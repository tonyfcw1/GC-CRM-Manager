# CRM Support Case UI

本地 B/S 应用。FastAPI 提供 Dataverse API、Azure CLI 身份验证和 SQLite 批量任务，前端使用 Vite、Vue 3、Element Plus、SortableJS 与 pinyin-pro。

## 目录

- `app.py`：FastAPI 应用与 HTTP 接口
- `dataverse_client.py`：Azure CLI 令牌、Dataverse HTTP 客户端与日期规范化
- `dataverse_gateway.py`：客户/商机来源查询、权利聚合、历史案例查询和案例创建
- `forum_gateway.py`：使用本次请求提供的 GCDN Cookie 上传图片并创建论坛主题（服务端不保存 Cookie）
- `batch_jobs.py`：SQLite 批量任务与后台执行
- `launcher.py`：Windows 本地启动器
- `frontend/`：Vue 前端源码
- `static/`：Vite 生产构建，由 FastAPI 提供
- `data/`：运行时生成，不进入发布包

## 后端启动

在项目根目录（包含 `crm_support_ui` 文件夹的目录）执行：

```powershell
python -m pip install -r requirements.txt
$env:CRM_AZ_PATH='C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd'
python -m uvicorn crm_support_ui.app:app --host 127.0.0.1 --port 8765
```

也可以直接运行 `start_crm_ui.cmd`。

## 前端开发

```powershell
cd crm_support_ui\frontend
npm.cmd ci
npm.cmd run dev
```

Vite 默认运行在 `http://127.0.0.1:5173`，并将 `/api` 代理到 `http://127.0.0.1:8765`。

## 构建

```powershell
cd crm_support_ui\frontend
npm.cmd ci
npm.cmd run build
```

构建结果写入 `crm_support_ui/static`。

前端测试：

```powershell
cd crm_support_ui\frontend
npm.cmd test
```

## 测试

```powershell
python -m unittest tests.test_crm_support_ui
```

工作区开发布局下使用：

```powershell
python -m unittest work.test_crm_support_ui
```

历史案例接口为 `GET /api/incidents?limit=500`，只返回当前 Azure CLI 用户创建的技术支持案例。

## 配置

- `CRM_ENVIRONMENT`：Dataverse 环境 URL
- `CRM_TENANT_ID`：Microsoft Entra 租户 ID
- `CRM_AZ_PATH`：Azure CLI 的完整路径
- `CRM_BATCH_DATABASE`：批量任务 SQLite 文件路径

论坛发帖接口为 `POST /api/forum-post`，接受单条请求中的 `cookie`、`title`、`content` 和可选 `rewardprice` 字段。未提供 `rewardprice` 时使用论坛悬赏表单针对当前账号返回的默认值。带图片时使用 `POST /api/forum-post-with-images` 的 `multipart/form-data`，文本字段名称相同，图片字段名称为 `attachments`，可重复提交多张文件。接口不会读取浏览器 Cookie，也不会把 Cookie 写入服务端存储；前端可选择将 Cookie 保存在当前浏览器的 `localStorage`，仅用于下次自动回填。

前端使用求助中心的悬赏表单，提交地址和表单正文都会明确指定 `special=3`，并固定提交“未处理”主题分类（`typeid=286`）。如果论坛没有返回悬赏金额字段或悬赏类型不正确，接口会停止发布并返回错误，避免创建成普通帖子。

前端单条录入勾选论坛选项后会显示独立的论坛主题和内容输入框，需要单独填写，不会自动带入 CRM 主题、说明、客户/商机名称、日期或 CRM 链接。可以在内容框中直接按 `Ctrl+V` 粘贴图片，也可以点击“添加图片”选择文件；支持 PNG、JPEG、GIF、WEBP、BMP，最多 10 张、单张不超过 10 MB、总大小不超过 50 MB。图片缩略图只保存在当前页面内存中，发送时由本机服务先上传并绑定到主题。“单独发帖”按钮只调用论坛接口，不创建 CRM 案例；发帖失败时当前表单和图片会保留，成功后只清空当前表单。Cookie 输入旁的“记住 Cookie”选项控制浏览器本地保存，旁边的清除操作可以立即删除已保存值；已保存的 Cookie 不会因成功发帖而清除。批量录入暂不自动发帖。

未配置时使用当前葡萄城 CRM 环境、租户和项目内 `data/batch_jobs.db`。

## 发布包规则

发布包包含 Python 源码、前端源码、生产静态文件、依赖锁文件、测试和文档，不包含：

- `node_modules`
- `__pycache__` / `.pyc`
- SQLite 运行数据
- 日志
- Azure CLI 登录数据或令牌
