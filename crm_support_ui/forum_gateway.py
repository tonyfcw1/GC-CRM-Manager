from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from html import unescape
from http.cookies import SimpleCookie
from typing import Any, Iterable
from urllib.parse import parse_qs, parse_qsl, quote_plus, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://gcdn.grapecity.com.cn"
EDIT_URL = (
    f"{BASE_URL}/forum.php?mod=post&action=newthread&fid=230&special=3"
)
UNPROCESSED_TYPEID = "286"
MAX_TITLE_LENGTH = 80
MAX_COOKIE_LENGTH = 16_000
MAX_CONTENT_LENGTH = 200_000
MAX_ATTACHMENT_COUNT = 10
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_SIZE = 50 * 1024 * 1024

_ALLOWED_IMAGE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
    }
)
_IMAGE_TYPE_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_CANONICAL_EXTENSION_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


class ForumPostError(RuntimeError):
    """A safe-to-display error from the GCDN forum posting flow."""


@dataclass(frozen=True)
class ForumAttachment:
    """An image kept in memory for one forum-post request."""

    filename: str
    content: bytes
    content_type: str = ""


def _load_cookie_jar(raw_cookie: str) -> requests.cookies.RequestsCookieJar:
    cookie = str(raw_cookie or "").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    if not cookie:
        raise ForumPostError("论坛 Cookie 不能为空")
    if len(cookie) > MAX_COOKIE_LENGTH:
        raise ForumPostError("论坛 Cookie 过长，请确认粘贴的是 Cookie 请求头内容")
    if "\r" in cookie or "\n" in cookie:
        raise ForumPostError("论坛 Cookie 不能包含换行")

    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
    except (TypeError, ValueError) as exc:
        raise ForumPostError("论坛 Cookie 格式无法解析") from exc
    if not parsed:
        raise ForumPostError("论坛 Cookie 格式无法解析")

    # Ignore attributes such as Path/Expires pasted from a browser export and
    # let requests merge any refreshed cookies returned by the GET request.
    jar = requests.cookies.RequestsCookieJar()
    for morsel in parsed.values():
        jar.set(
            morsel.key,
            morsel.value,
            domain="gcdn.grapecity.com.cn",
            path="/",
        )
    return jar


def _decode_page(response: requests.Response) -> str:
    content = bytes(response.content)
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig")

    candidates: list[str] = []
    declared = str(getattr(response, "encoding", "") or "").strip()
    if declared and declared.lower() not in {"iso-8859-1", "latin-1", "ascii"}:
        candidates.append(declared)

    head = content[:8192].decode("ascii", errors="ignore")
    meta_match = re.search(
        r"<meta[^>]+charset\s*=\s*[\"']?\s*([a-z0-9._-]+)",
        head,
        flags=re.IGNORECASE,
    )
    if not meta_match:
        meta_match = re.search(
            r"<meta[^>]+content\s*=\s*[\"'][^\"']*charset\s*=\s*([a-z0-9._-]+)",
            head,
            flags=re.IGNORECASE,
        )
    if meta_match:
        candidates.append(meta_match.group(1))

    # Prefer UTF-8 for modern responses, then fall back to the forum's legacy
    # GBK encoding when no declaration is available.
    candidates.extend(("utf-8", "gbk"))
    seen: set[str] = set()
    for encoding in candidates:
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def _selected_option_value(element) -> str:
    option = element.find("option", selected=True) or element.find("option")
    return option.get("value", "") if option else ""


def _form_fields(form) -> list[tuple[str, str]]:
    """Extract values that a normal browser submit would send."""

    fields: list[tuple[str, str]] = []
    for element in form.select("input[name], textarea[name], select[name], button[name]"):
        name = element.get("name")
        if not name:
            continue
        if element.has_attr("disabled"):
            continue
        tag = element.name.lower()
        if tag == "input":
            input_type = (element.get("type") or "text").lower()
            if input_type in {"checkbox", "radio"} and not element.has_attr("checked"):
                continue
            if input_type == "file":
                continue
            value = element.get("value", "")
        elif tag == "select":
            value = _selected_option_value(element)
        elif tag == "textarea":
            value = element.text or ""
        else:
            value = element.get("value", "")
        fields.append((str(name), str(value)))
    return fields


def _replace_field(fields: list[tuple[str, str]], name: str, value: str) -> None:
    replaced = False
    result: list[tuple[str, str]] = []
    for key, old_value in fields:
        if key == name:
            if not replaced:
                result.append((name, value))
                replaced = True
            continue
        result.append((key, old_value))
    if not replaced:
        result.append((name, value))
    fields[:] = result


def _field_value(fields: Iterable[tuple[str, str]], name: str) -> str:
    for key, value in fields:
        if key == name:
            return value
    return ""


def _replace_query_fields(url: str, values: dict[str, str]) -> str:
    parsed = urlparse(url)
    fields = parse_qsl(parsed.query, keep_blank_values=True)
    for name, value in values.items():
        _replace_field(fields, name, value)
    return parsed._replace(query=urlencode(fields)).geturl()


def _normalize_rewardprice(value: str) -> str:
    try:
        normalized = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ForumPostError("悬赏金币必须是整数") from exc
    if normalized < 1:
        raise ForumPostError("悬赏金币必须大于 0")
    return str(normalized)


def _detected_image_type(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"BM"):
        return "image/bmp"
    return ""


def _safe_attachment_filename(filename: str, content_type: str) -> str:
    value = str(filename or "").strip()
    # A browser can hand us a path-like name. Only the final component belongs
    # in the multipart header, and control characters are never valid there.
    value = re.split(r"[\\/]", value)[-1]
    value = "".join(character for character in value if ord(character) >= 32 and ord(character) != 127)
    extension = _CANONICAL_EXTENSION_BY_TYPE.get(content_type, ".png")
    if not value:
        value = f"pasted-image{extension}"
    else:
        stem, dot, suffix = value.rpartition(".")
        if not dot or suffix.lower() not in {
            candidate_suffix[1:]
            for candidate_suffix, mime in _IMAGE_TYPE_BY_EXTENSION.items()
            if mime == content_type
        }:
            value = f"{stem if dot else value}{extension}"
    if len(value) > 255:
        stem, dot, suffix = value.rpartition(".")
        if dot:
            value = f"{stem[: 254 - len(suffix)]}.{suffix}"
        else:
            value = value[:255]
    return value


def _normalize_attachments(
    attachments: Iterable[ForumAttachment] | None,
) -> list[ForumAttachment]:
    if attachments is None:
        return []
    try:
        values = list(attachments)
    except TypeError as exc:
        raise ForumPostError("论坛图片附件格式无效") from exc
    if len(values) > MAX_ATTACHMENT_COUNT:
        raise ForumPostError(f"一次最多上传 {MAX_ATTACHMENT_COUNT} 张图片")

    normalized: list[ForumAttachment] = []
    total_size = 0
    for index, attachment in enumerate(values, start=1):
        if not isinstance(attachment, ForumAttachment):
            raise ForumPostError("论坛图片附件格式无效")
        try:
            content = bytes(attachment.content)
        except (TypeError, ValueError) as exc:
            raise ForumPostError(f"第 {index} 张图片内容无效") from exc
        if not content:
            raise ForumPostError(f"第 {index} 张图片为空")
        if len(content) > MAX_ATTACHMENT_SIZE:
            raise ForumPostError(
                f"第 {index} 张图片不能超过 {MAX_ATTACHMENT_SIZE // (1024 * 1024)} MB"
            )

        declared_type = str(attachment.content_type or "").split(";", 1)[0].strip().lower()
        filename = str(attachment.filename or "").strip()
        detected_type = _detected_image_type(content)
        # Do not trust the browser supplied MIME type or filename. A file can
        # be renamed to .png (or sent with image/png) while containing
        # arbitrary bytes. The upload endpoint only receives data after a
        # supported image signature has been detected.
        if detected_type not in _ALLOWED_IMAGE_TYPES:
            raise ForumPostError(
                f"第 {index} 张文件不是有效的 PNG、JPEG、GIF、WEBP 或 BMP 图片"
            )
        if declared_type in _ALLOWED_IMAGE_TYPES and declared_type != detected_type:
            raise ForumPostError(f"第 {index} 张图片格式与文件内容不一致")
        declared_type = detected_type

        total_size += len(content)
        if total_size > MAX_TOTAL_ATTACHMENT_SIZE:
            raise ForumPostError(
                f"图片总大小不能超过 {MAX_TOTAL_ATTACHMENT_SIZE // (1024 * 1024)} MB"
            )
        normalized.append(
            ForumAttachment(
                filename=_safe_attachment_filename(filename, declared_type),
                content=content,
                content_type=declared_type,
            )
        )
    return normalized


def _query_value(url: str, name: str) -> str:
    for key, values in parse_qs(urlparse(url).query, keep_blank_values=True).items():
        if key.lower() == name.lower() and values:
            return str(values[0]).strip()
    return ""


def _script_value(text: str, names: Iterable[str], pattern: str) -> str:
    joined_names = "|".join(re.escape(name) for name in names)
    match = re.search(
        # Discuz exposes these values both as plain assignments (`hash: "…"`)
        # and as quoted object keys (`"hash":"…"`).
        rf"(?:\b(?:{joined_names})\b|['\"](?:{joined_names})['\"])\s*(?:[:=])\s*['\"]?{pattern}",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _upload_url_candidates(html: str, soup: BeautifulSoup) -> list[str]:
    candidates: list[str] = []
    normalized_html = unescape(html).replace("\\/", "/").replace("\\u0026", "&")
    for element in soup.select("[href], [src], [action], [data-upload-url], [data-url]"):
        for attribute in ("href", "src", "action", "data-upload-url", "data-url"):
            value = element.get(attribute)
            if value:
                candidates.append(unescape(str(value)).replace("\\/", "/"))
    # Discuz normally embeds the SWFUpload URL in JavaScript. Keep the match
    # bounded by a quote/whitespace so trailing script syntax is discarded.
    candidates.extend(
        match.group(0).rstrip("),;]")
        for match in re.finditer(
            r"(?:https?:)?//[^\"'<>\s]+|[^\"'<>\s]*misc\.php\?[^\"'<>\s]+",
            normalized_html,
            flags=re.IGNORECASE,
        )
    )
    return candidates


def _extract_upload_context(
    html: str,
    fields: Iterable[tuple[str, str]],
    page_url: str,
    cookie_jar: requests.cookies.RequestsCookieJar,
) -> tuple[str, str, str]:
    """Return the same-domain upload URL, uid and hash exposed by the form."""

    soup = BeautifulSoup(html, "html.parser")
    normalized_html = unescape(html).replace("\\/", "/").replace("\\u0026", "&")
    upload_url = ""
    uid = _field_value(fields, "uid").strip()
    upload_hash = _field_value(fields, "hash").strip()
    fid = _field_value(fields, "fid").strip() or "230"

    for candidate in _upload_url_candidates(html, soup):
        parsed = urlparse(urljoin(page_url, candidate))
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_keys = {key.lower() for key in query}
        if parsed.netloc.lower() != urlparse(BASE_URL).netloc.lower():
            continue
        if "swfupload" not in parsed.query.lower() or "operation" not in query_keys:
            continue
        if _query_value(parsed.geturl(), "operation").lower() != "upload":
            continue
        upload_url = parsed.geturl()
        uid = uid or _query_value(upload_url, "uid")
        upload_hash = upload_hash or _query_value(upload_url, "hash")
        fid = _query_value(upload_url, "fid") or fid
        break

    uid = uid or _script_value(normalized_html, ("discuz_uid", "uid"), r"(\d+)")
    upload_hash = upload_hash or _script_value(
        normalized_html,
        ("uploadhash", "hash"),
        r"([A-Za-z0-9_-]{8,128})",
    )
    if not uid:
        try:
            uid = str(cookie_jar.get("uid") or "").strip()
        except requests.cookies.CookieConflictError:
            uid = ""
    if not upload_hash:
        try:
            upload_hash = str(cookie_jar.get("hash") or "").strip()
        except requests.cookies.CookieConflictError:
            upload_hash = ""

    if not re.fullmatch(r"\d+", uid or "") or not upload_hash:
        raise ForumPostError("没有取得图片上传凭据，请刷新论坛登录状态后重试")
    if not upload_url:
        upload_url = (
            f"{BASE_URL}/misc.php?mod=swfupload&action=swfupload"
            f"&operation=upload&fid={quote_plus(fid)}"
        )
    if not upload_url.startswith(f"{BASE_URL}/"):
        raise ForumPostError("论坛图片上传地址不在 GCDN 域名内")
    return _replace_query_fields(
        upload_url,
        {"uid": uid, "hash": upload_hash, "fid": fid},
    ), uid, upload_hash


def _positive_attachment_id(value: Any) -> str:
    candidate = str(value or "").strip()
    return candidate if re.fullmatch(r"[1-9]\d*", candidate) else ""


def _find_attachment_id(value: Any) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in {"aid", "attachid", "attachmentid"}:
                found = _positive_attachment_id(nested)
                if found:
                    return found
            found = _find_attachment_id(nested)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _find_attachment_id(nested)
            if found:
                return found
    return ""


def _extract_attachment_id(response: requests.Response) -> str:
    text = _decode_page(response).strip()
    direct = _positive_attachment_id(text)
    if direct:
        return direct
    # The normal desktop endpoint returns only the aid. The compact/mobile
    # variants return one of Discuz's pipe-delimited DISCUZUPLOAD records.
    parts = text.split("|")
    if parts and parts[0].strip().upper() == "DISCUZUPLOAD":
        if len(parts) > 2 and parts[1].strip() == "0":
            direct = _positive_attachment_id(parts[2])
            if direct:
                return direct
        if len(parts) > 3 and parts[2].strip() == "0":
            direct = _positive_attachment_id(parts[3])
            if direct:
                return direct
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    found = _find_attachment_id(parsed)
    if found:
        return found
    soup = BeautifulSoup(text, "html.parser")
    for element in soup.find_all(True):
        for attribute in ("data-aid", "data-attachid", "data-attachmentid"):
            found = _positive_attachment_id(element.get(attribute))
            if found:
                return found
    match = re.search(
        r"(?:aid|attachid|attachmentid)\s*[\"']?\s*[:=]\s*[\"']?(\d+)",
        text,
        flags=re.IGNORECASE,
    )
    return _positive_attachment_id(match.group(1)) if match else ""


def _upload_attachment(
    session: requests.Session,
    upload_url: str,
    uid: str,
    upload_hash: str,
    attachment: ForumAttachment,
    referer: str,
) -> str:
    fid = _query_value(upload_url, "fid") or "230"
    # Discuz reads these two values from the query string while constructing
    # the uploaded file metadata. Sending them only as multipart fields can
    # make an otherwise valid image be treated as an arbitrary attachment.
    request_url = _replace_query_fields(
        upload_url,
        {
            "filetype": attachment.content_type,
            "type": "image",
        },
    )
    data = {
        "uid": uid,
        "hash": upload_hash,
        "fid": fid,
        "uploadsubmit": "true",
        "filetype": attachment.content_type,
        "filesize": str(len(attachment.content)),
    }
    try:
        response = session.post(
            request_url,
            data=data,
            files={
                "Filedata": (
                    attachment.filename,
                    attachment.content,
                    attachment.content_type,
                )
            },
            headers={
                "Referer": referer,
                "Origin": BASE_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ForumPostError(f"论坛图片上传失败：{exc}") from exc
    attachment_id = _extract_attachment_id(response)
    if not attachment_id:
        summary = _response_summary(response)
        raise ForumPostError(f"论坛未返回有效图片编号：{summary or '服务器返回了未知响应'}")
    return attachment_id


def _append_attachment_fields(fields: list[tuple[str, str]], attachment_ids: Iterable[str]) -> None:
    for attachment_id in attachment_ids:
        fields.extend(
            (
                (f"attachnew[{attachment_id}][description]", ""),
                (f"attachnew[{attachment_id}][readperm]", ""),
                (f"attachnew[{attachment_id}][price]", "0"),
            )
        )


def _append_attachment_tags(content: str, attachment_ids: Iterable[str]) -> str:
    tags = [f"[attachimg]{attachment_id}[/attachimg]" for attachment_id in attachment_ids]
    return "\n".join((content, *tags)) if tags else content


def _best_effort_delete_attachment(
    session: requests.Session,
    attachment_id: str,
    formhash: str,
    referer: str,
) -> None:
    """Remove an uploaded-but-unbound file after a failed posting attempt."""

    try:
        cleanup_url = (
            f"{BASE_URL}/forum.php?mod=ajax&action=deleteattach&inajax=yes"
            f"&formhash={quote_plus(formhash)}&tid=0&pid=0"
            f"&aids[]={quote_plus(attachment_id)}"
        )
        session.get(
            cleanup_url,
            headers={"Referer": referer, "Origin": BASE_URL},
            timeout=10,
            allow_redirects=True,
        )
    except (requests.RequestException, TypeError, AttributeError):
        # Cleanup is deliberately best effort. The original posting error is
        # more useful than a secondary failure from an optional endpoint.
        return


def _extract_formhash(html: str, fields: Iterable[tuple[str, str]]) -> str:
    formhash = _field_value(fields, "formhash").strip()
    if formhash:
        return formhash

    # Some forum responses leave the hidden input empty and expose the token
    # only in a same-domain logout/link URL. Parse attributes so an unrelated
    # external URL or page text cannot supply the token accidentally.
    soup = BeautifulSoup(html, "html.parser")
    expected_host = urlparse(BASE_URL).netloc.lower()
    for element in soup.select("[href], [action]"):
        for attribute in ("href", "action"):
            raw_url = element.get(attribute)
            if not raw_url:
                continue
            parsed_url = urlparse(urljoin(BASE_URL, unescape(str(raw_url))))
            if parsed_url.netloc.lower() != expected_host:
                continue
            query = parse_qs(parsed_url.query, keep_blank_values=True)
            for key, values in query.items():
                if key.lower() != "formhash" or not values:
                    continue
                candidate = values[0].strip()
                if re.fullmatch(r"[0-9a-z]+", candidate, flags=re.IGNORECASE):
                    return candidate

    # A token assigned by page JavaScript is the remaining supported form.
    # Restrict this fallback to script bodies so query parameters from an
    # unrelated external link cannot be mistaken for the forum token.
    script_text = "\n".join(script.get_text() for script in soup.find_all("script"))
    patterns = (
        r"\bFORMHASH\b\s*[=:]\s*['\"]([0-9a-z]+)['\"]",
        r"\bformhash\b\s*[=:]\s*['\"]([0-9a-z]+)['\"]",
        r"\bformhash\b\s*[=:]\s*([0-9a-z]+)(?:[^0-9a-z]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, script_text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _encode_gbk_form(fields: Iterable[tuple[str, str]]) -> bytes:
    pairs: list[str] = []
    for key, value in fields:
        try:
            key_bytes = str(key).encode("gbk")
            value_bytes = str(value).encode("gbk")
        except UnicodeEncodeError as exc:
            raise ForumPostError("标题或内容包含论坛 GBK 不支持的字符") from exc
        pairs.append(
            f"{quote_plus(key_bytes, safe='')}={quote_plus(value_bytes, safe='')}"
        )
    return "&".join(pairs).encode("ascii")


def _response_summary(response: requests.Response) -> str:
    text = _decode_page(response)
    compact = " ".join(BeautifulSoup(text, "html.parser").stripped_strings)
    return compact[:500] + ("..." if len(compact) > 500 else "")


def create_forum_post(
    *,
    cookie: str,
    title: str,
    content: str,
    rewardprice: str | None = None,
    attachments: Iterable[ForumAttachment] | None = None,
) -> dict[str, Any]:
    """Create one topic using a caller-provided, already-authenticated cookie.

    The cookie is held only by this call and is never returned or logged.
    """

    normalized_title = str(title or "").strip()
    normalized_content = str(content or "").replace("\r\n", "\n").strip()
    if not normalized_title:
        raise ForumPostError("论坛帖子标题不能为空")
    if len(normalized_title) > MAX_TITLE_LENGTH:
        raise ForumPostError(f"论坛帖子标题不能超过 {MAX_TITLE_LENGTH} 个字符")
    if not normalized_content:
        raise ForumPostError("论坛帖子内容不能为空")
    if len(normalized_content) > MAX_CONTENT_LENGTH:
        raise ForumPostError("论坛帖子内容过长")
    normalized_attachments = _normalize_attachments(attachments)
    requested_reward = "" if rewardprice is None else str(rewardprice).strip()
    normalized_reward = (
        _normalize_rewardprice(requested_reward) if requested_reward else ""
    )

    cookie_jar = _load_cookie_jar(cookie)
    session = requests.Session()
    session.cookies.update(cookie_jar)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    uploaded_ids: list[str] = []
    formhash = ""
    topic_attempted = False
    try:
        page = session.get(EDIT_URL, timeout=20)
        page.raise_for_status()
        html = _decode_page(page)
        soup = BeautifulSoup(html, "html.parser")
        form = soup.select_one("#postform")
        if form is None:
            raise ForumPostError(
                "没有找到论坛发帖表单；Cookie 可能已失效，或论坛要求验证码/访问验证"
            )

        fields = _form_fields(form)
        formhash = _extract_formhash(html, fields)
        if not formhash:
            raise ForumPostError("没有取得 formhash，请刷新论坛登录状态后重试")

        form_special = _field_value(fields, "special").strip()
        form_reward = _field_value(fields, "rewardprice").strip()
        if form_special != "3" or not form_reward:
            raise ForumPostError(
                "论坛没有返回悬赏发帖表单；请确认当前账号和版块允许发布悬赏主题"
            )
        if not normalized_reward:
            normalized_reward = _normalize_rewardprice(form_reward)

        post_content = normalized_content
        if normalized_attachments:
            upload_url, uid, upload_hash = _extract_upload_context(
                html,
                fields,
                str(page.url),
                session.cookies,
            )
            for attachment in normalized_attachments:
                uploaded_ids.append(
                    _upload_attachment(
                        session,
                        upload_url,
                        uid,
                        upload_hash,
                        attachment,
                        EDIT_URL,
                    )
                )
            post_content = _append_attachment_tags(post_content, uploaded_ids)
            if len(post_content) > MAX_CONTENT_LENGTH:
                raise ForumPostError("图片标签加入后，论坛帖子内容过长")
            _append_attachment_fields(fields, uploaded_ids)

        _replace_field(fields, "formhash", formhash)
        _replace_field(fields, "subject", normalized_title)
        _replace_field(fields, "message", post_content)
        _replace_field(fields, "rewardprice", normalized_reward)
        _replace_field(fields, "special", "3")
        _replace_field(fields, "typeid", UNPROCESSED_TYPEID)
        _replace_field(fields, "wysiwyg", _field_value(fields, "wysiwyg") or "0")
        _replace_field(fields, "posttime", _field_value(fields, "posttime") or str(int(time.time())))
        _replace_field(fields, "topicsubmit", "true")

        action = urljoin(EDIT_URL, form.get("action") or "")
        if not action.startswith(f"{BASE_URL}/"):
            raise ForumPostError("论坛发帖表单地址不在 GCDN 域名内")
        action = _replace_query_fields(
            action,
            {"special": "3", "topicsubmit": "yes"},
        )
        topic_attempted = True
        response = session.post(
            action,
            data=_encode_gbk_form(fields),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": EDIT_URL,
                "Origin": BASE_URL,
            },
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        response_text = _decode_page(response)
        response_url = str(response.url)
        published = bool(
            re.search(
                r"(?:showtopic|viewthread|mod=redirect|(?:tid|ptid)=\d+)",
                response_url,
                re.IGNORECASE,
            )
            or re.search(r"发表成功|发布成功|主题已发布", response_text)
        )
        if not published:
            summary = _response_summary(response)
            raise ForumPostError(f"论坛未确认发布成功：{summary or '服务器返回了未知页面'}")
    except ForumPostError:
        if not topic_attempted:
            for attachment_id in reversed(uploaded_ids):
                _best_effort_delete_attachment(session, attachment_id, formhash, EDIT_URL)
        raise
    except requests.RequestException as exc:
        if not topic_attempted:
            for attachment_id in reversed(uploaded_ids):
                _best_effort_delete_attachment(session, attachment_id, formhash, EDIT_URL)
        raise ForumPostError(f"论坛网络请求失败：{exc}") from exc
    finally:
        session.close()

    return {
        "url": response_url,
        "title": normalized_title,
        "rewardprice": normalized_reward,
        "attachment_count": len(uploaded_ids),
    }


def create_forum_post_with_attachments(
    *,
    cookie: str,
    title: str,
    content: str,
    attachments: Iterable[ForumAttachment],
    rewardprice: str | None = None,
) -> dict[str, Any]:
    """Explicit multipart-facing wrapper retained as a small public seam."""

    return create_forum_post(
        cookie=cookie,
        title=title,
        content=content,
        rewardprice=rewardprice,
        attachments=attachments,
    )
