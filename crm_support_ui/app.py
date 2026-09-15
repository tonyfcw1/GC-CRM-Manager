from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from fastapi import File, Form, FastAPI, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .dataverse_client import (
    DEFAULT_ENVIRONMENT,
    DEFAULT_TENANT_ID,
    DataverseClient,
    DataverseError,
    acquire_azure_cli_token,
)

from .dataverse_gateway import DataverseGateway
from .batch_jobs import BatchJobManager, BatchJobStore, parse_excel_tsv
from .forum_gateway import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_ATTACHMENT_COUNT,
    MAX_ATTACHMENT_SIZE,
    MAX_TOTAL_ATTACHMENT_SIZE,
    ForumAttachment,
    ForumPostError,
    create_forum_post as send_forum_post,
    create_forum_post_with_attachments as send_forum_post_with_attachments,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"


class IncidentInput(BaseModel):
    source_entity: str
    source_id: str
    subject: str
    description: str = ""
    actual_end: str


class BatchItemInput(IncidentInput):
    client_key: str
    source_name: str = ""


class BatchInput(BaseModel):
    items: list[BatchItemInput]


class PasteInput(BaseModel):
    text: str


class ForumPostInput(BaseModel):
    cookie: str
    title: str
    content: str
    rewardprice: str | None = None


def build_default_gateway() -> DataverseGateway:
    environment = os.environ.get("CRM_ENVIRONMENT", DEFAULT_ENVIRONMENT).rstrip("/")
    tenant_id = os.environ.get("CRM_TENANT_ID", DEFAULT_TENANT_ID)
    az_path = os.environ.get("CRM_AZ_PATH") or None

    def client_factory() -> DataverseClient:
        token = acquire_azure_cli_token(tenant_id, environment, az_path)
        return DataverseClient(environment, token)

    return DataverseGateway(client_factory)


def _api_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


async def _read_forum_attachments(
    uploads: list[UploadFile] | None,
) -> list[ForumAttachment]:
    values = uploads or []
    if len(values) > MAX_ATTACHMENT_COUNT:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_ATTACHMENT_COUNT} 张图片")
    attachments: list[ForumAttachment] = []
    total_size = 0
    try:
        for upload in values:
            # Read no more than the smaller per-file and remaining aggregate
            # budget. This keeps an oversized multipart request from being
            # copied into memory beyond the endpoint's declared limits.
            remaining_size = MAX_TOTAL_ATTACHMENT_SIZE - total_size
            if remaining_size <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"图片总大小不能超过 {MAX_TOTAL_ATTACHMENT_SIZE // (1024 * 1024)} MB",
                )
            declared_size = getattr(upload, "size", None)
            if isinstance(declared_size, int) and declared_size >= 0:
                if declared_size > MAX_ATTACHMENT_SIZE:
                    raise HTTPException(
                        status_code=400,
                        detail=f"单张图片不能超过 {MAX_ATTACHMENT_SIZE // (1024 * 1024)} MB",
                    )
                if declared_size > remaining_size:
                    raise HTTPException(
                        status_code=400,
                        detail=f"图片总大小不能超过 {MAX_TOTAL_ATTACHMENT_SIZE // (1024 * 1024)} MB",
                    )
            content = await upload.read(min(MAX_ATTACHMENT_SIZE + 1, remaining_size + 1))
            if len(content) > MAX_ATTACHMENT_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=f"单张图片不能超过 {MAX_ATTACHMENT_SIZE // (1024 * 1024)} MB",
                )
            total_size += len(content)
            if total_size > MAX_TOTAL_ATTACHMENT_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=f"图片总大小不能超过 {MAX_TOTAL_ATTACHMENT_SIZE // (1024 * 1024)} MB",
                )
            attachments.append(
                ForumAttachment(
                    filename=upload.filename or "",
                    content=content,
                    content_type=upload.content_type or "",
                )
            )
    finally:
        for upload in values:
            try:
                await upload.close()
            except Exception:
                pass
    return attachments


def create_app(
    gateway: DataverseGateway | None = None,
    batch_manager: BatchJobManager | None = None,
) -> FastAPI:
    production_gateway = gateway is None
    service = gateway or build_default_gateway()
    manager = batch_manager
    if manager is None and production_gateway:
        database_path = Path(
            os.environ.get(
                "CRM_BATCH_DATABASE",
                Path(__file__).resolve().parent / "data" / "batch_jobs.db",
            )
        )
        manager = BatchJobManager(BatchJobStore(database_path), service)

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        try:
            yield
        finally:
            if manager is not None:
                manager.close()

    application = FastAPI(
        title="CRM \u6280\u672f\u652f\u6301\u5f55\u5165",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @application.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @application.get("/api/sources")
    def list_sources(scope: str = Query("related", pattern="^(related|owned|all)$")) -> dict:
        try:
            return service.list_sources(scope)
        except (DataverseError, requests.RequestException, OSError, ValueError) as exc:
            raise _api_error(exc) from exc

    @application.post("/api/incidents", status_code=201)
    def create_incident(values: IncidentInput) -> dict:
        try:
            return service.create_incident(**values.model_dump())
        except (DataverseError, requests.RequestException, OSError, ValueError) as exc:
            raise _api_error(exc) from exc

    @application.post("/api/forum-post", status_code=201)
    def create_forum_post(values: ForumPostInput, response: Response) -> dict:
        """Submit a GCDN topic using a cookie supplied for this request only."""

        response.headers["Cache-Control"] = "no-store"
        if not values.cookie.strip():
            raise HTTPException(status_code=400, detail="论坛 Cookie 不能为空")
        if not values.title.strip():
            raise HTTPException(status_code=400, detail="论坛帖子标题不能为空")
        if len(values.title.strip()) > MAX_TITLE_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"论坛帖子标题不能超过 {MAX_TITLE_LENGTH} 个字符",
            )
        if not values.content.strip():
            raise HTTPException(status_code=400, detail="论坛帖子内容不能为空")
        if len(values.content) > MAX_CONTENT_LENGTH:
            raise HTTPException(status_code=400, detail="论坛帖子内容过长")
        try:
            return send_forum_post(
                cookie=values.cookie,
                title=values.title,
                content=values.content,
                rewardprice=values.rewardprice,
            )
        except ForumPostError as exc:
            raise _api_error(exc) from exc

    @application.post("/api/forum-post-with-images", status_code=201)
    async def create_forum_post_with_images(
        response: Response,
        cookie: str = Form(""),
        title: str = Form(""),
        content: str = Form(""),
        rewardprice: str | None = Form(None),
        attachments: list[UploadFile] | None = File(None),
    ) -> dict:
        """Submit a topic after uploading images from the same authenticated session."""

        response.headers["Cache-Control"] = "no-store"
        if not cookie.strip():
            raise HTTPException(status_code=400, detail="论坛 Cookie 不能为空")
        if not title.strip():
            raise HTTPException(status_code=400, detail="论坛帖子标题不能为空")
        if len(title.strip()) > MAX_TITLE_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"论坛帖子标题不能超过 {MAX_TITLE_LENGTH} 个字符",
            )
        if not content.strip():
            raise HTTPException(status_code=400, detail="论坛帖子内容不能为空")
        if len(content) > MAX_CONTENT_LENGTH:
            raise HTTPException(status_code=400, detail="论坛帖子内容过长")
        uploaded = await _read_forum_attachments(attachments)
        try:
            # The forum gateway uses the synchronous requests client. Keep
            # that network work off FastAPI's event loop while the multipart
            # files are already held in memory for this request.
            return await run_in_threadpool(
                send_forum_post_with_attachments,
                cookie=cookie,
                title=title,
                content=content,
                rewardprice=rewardprice,
                attachments=uploaded,
            )
        except ForumPostError as exc:
            raise _api_error(exc) from exc

    @application.get("/api/incidents")
    def list_incidents(limit: int = Query(500, ge=1, le=500)) -> dict:
        try:
            return service.list_incidents(limit)
        except (DataverseError, requests.RequestException, OSError, ValueError) as exc:
            raise _api_error(exc) from exc

    @application.post("/api/parse-paste")
    def parse_paste(values: PasteInput) -> dict:
        if len(values.text) > 2_000_000:
            raise HTTPException(status_code=413, detail="Pasted content is too large")
        rows = parse_excel_tsv(values.text)
        return {
            "rows": rows,
            "row_count": len(rows),
            "column_count": max((len(row) for row in rows), default=0),
        }

    @application.post("/api/batches", status_code=202)
    def create_batch(values: BatchInput) -> dict:
        if manager is None:
            raise HTTPException(status_code=503, detail="Batch processing is unavailable")
        if not 1 <= len(values.items) <= 200:
            raise HTTPException(status_code=400, detail="A batch must contain 1 to 200 items")
        try:
            return manager.create_job([item.model_dump() for item in values.items])
        except (DataverseError, OSError, ValueError) as exc:
            raise _api_error(exc) from exc

    @application.get("/api/batches/{job_id}")
    def get_batch(job_id: str) -> dict:
        if manager is None:
            raise HTTPException(status_code=503, detail="Batch processing is unavailable")
        try:
            return manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Batch job was not found") from exc

    @application.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return application


app = create_app()
