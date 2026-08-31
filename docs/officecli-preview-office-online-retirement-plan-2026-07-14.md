# OfficeCLI HTML 预览闭环与 Office Online 完整退役计划

> 日期：2026-07-14
> 状态：Step 1–9 全部完成；OfficeCLI 预览已闭环，production Office Online 服务已删除并通过删除后验收
> 当前代码基线：`git HEAD = 33fbecd9d8021685aa2471114113b1edcc740b98`
> 生产项目：Railway `dd959a13-19f9-497a-9704-42c310eae230`，environment=`production`
> 适用范围：Agent Detail 对话侧 Current Workspace、ChatArtifact Office 文件预览、OfficeCLI runtime、ONLYOFFICE 源码/配置/生产服务退役
> 交付纪律：一次完成测试、实现、兼容数据清理、观测、三服务部署、生产验收和线上服务删除；不保留默认关闭的半成品或死代码

说明：本文中的 **Office Online** 指 Hive 当前通过 `onlyoffice-documentserver` 提供的浏览器在线编辑能力，具体实现是 ONLYOFFICE DocumentServer，不是 Microsoft Office Online Server。

---

## 0. 最终决策

在以下产品前提下，本轮不再保留 Office Online：

- 用户需要在 Hive 中查看 DOCX、XLSX、PPTX 的内容；
- Agent 继续通过 OfficeCLI 创建、读取、查询、修改、校验和渲染 Office 文件；
- 本轮不提供人类浏览器内 WYSIWYG 写回、多人实时协作、评论、审阅或共同编辑；
- Office 二进制文件继续是可下载、可外发、可被 Agent 修改的兼容交付格式；
- 文件内容真相仍是 Agent workspace 或 ChatArtifact delivery-time snapshot，HTML 只是可重建的派生预览，不是第二事实源。

最终产品形态：

1. 删除 Agent Detail 中独立的 Office Online Tag/标签页；对话侧 Current Workspace 通过 OfficeCLI `view <file> html --json` 预览 DOCX、XLSX、PPTX。
2. ChatArtifact 中的 DOCX、XLSX、PPTX 使用相同渲染服务预览 delivery-time snapshot；只有无快照字段的 legacy row 才允许读取当前 workspace 文件。
3. HTML 通过带鉴权的 Blob 请求取得，只在隔离 iframe 中渲染，不进入主页面 DOM。
4. HTML 渲染失败时使用可观测的 OfficeCLI text fallback；再次失败时保留原文件下载和明确错误。
5. 删除前端 ONLYOFFICE host、后端 editor/callback/force-save/token 路径、配置、Docker 资源、legacy active editor metadata。
6. 部署并验收 `backend`、`backend-api`、`frontend` 后，最后删除 Railway `onlyoffice-documentserver`。

本轮不是“先删除服务再看看能不能预览”。必须先让新消费链在生产闭环，再执行不可逆的服务删除。

---

## 1. 当前事实基线

### 1.1 OfficeCLI 生产能力

2026-07-14 已通过 Railway SSH 核实：

```text
backend /usr/local/bin/officecli
officecli version: 1.0.88
```

生产二进制的真实命令契约：

```bash
officecli view <file> <mode> [options]
```

支持的 `mode` 包含：

```text
text, annotated, outline, stats, issues, html, svg, screenshot, forms
```

正确的 HTML 命令是：

```bash
officecli view workspace/report.docx html --json
```

成功返回的 JSON 结构为：

```json
{
  "success": true,
  "data": "<!DOCTYPE html>..."
}
```

### 1.2 当前适配器存在真实命令断点

当前调用链：

```text
office_document_view
  -> OfficeDocumentService.run_view()
  -> OfficeCLIAdapter.run("view", path, options={"mode": "html"})
  -> officecli view <file> --json --mode html
```

生产二进制不接受上述参数顺序，实测返回：

```text
Unrecognized command or argument 'html'.
Usage:
  officecli view <file> <mode> [options]
```

相关当前文件：

- `backend/app/services/officecli_adapter.py`
- `backend/app/services/office_document_service.py`
- `backend/app/tools/handlers/office.py`
- `backend/tests/services/test_officecli_adapter.py`

现有单元测试把错误的 `--mode` 形式写进了断言，因此测试通过不代表真实 OfficeCLI 契约成立。

### 1.3 当前用户消费只依赖 ONLYOFFICE

当前 Agent Office Workbench：

```text
OfficeWorkbenchSection
  -> officeApi.getEditorConfig(..., "edit")
  -> GET /agents/{agent_id}/office/editor-config
  -> ONLYOFFICE DocsAPI.DocEditor
  -> callback / force-save / signed download
```

当前前端事实：

- `OfficeWorkbenchSection.tsx` 固定请求 `mode=edit`；
- `OnlyOfficeHost` 动态加载 `/web-apps/apps/api/documents/api.js`；
- `ArtifactSurface.getArtifactOpenMode()` 把 `previewKind=office` 归为 download；
- DOCX、XLSX、PPTX ChatArtifact 当前不能进入 inspector preview；
- 前端已有 `getBlob()` 和 authenticated resource helper，可直接复用，不需要新增 query-token 下载旁路。

### 1.4 当前后端 ONLYOFFICE 路径

`backend/app/api/office.py` 当前包含：

- `GET /editor-config`
- `GET /download`
- `POST /force-save`
- `POST /callback`
- ONLYOFFICE JWT、document key、callback URL、download URL、command URL helpers

`backend/app/services/office_document_service.py` 当前包含：

- `active_editor_session` manifest 字段；
- `set_active_editor_session()`；
- `clear_active_editor_session()`；
- `get_active_editor_session()`；
- `run_apply(..., require_no_active_editor=True)` 写入阻断。

这些路径在 Office Online 退役后必须一起删除，不能只删 Railway 服务后让前端长期显示“ONLYOFFICE 未配置”。

### 1.5 当前生产拓扑

2026-07-14 Railway production 已核实：

| 服务 | 当前状态 | 说明 |
| --- | --- | --- |
| `backend` | `SUCCESS` | 最新部署时间 `2026-07-14T02:00:05Z` |
| `backend-api` | `SUCCESS` | 最新部署时间 `2026-07-14T02:00:08Z` |
| `frontend` | `SUCCESS` | 最新部署时间 `2026-07-14T02:00:12Z` |
| `onlyoffice-documentserver` | `SUCCESS` | image=`onlyoffice/documentserver:9.3`，1 replica，无 Railway volume |

`onlyoffice-documentserver` 当前仍是活跃生产服务，不能在新预览路径上线前删除。

---

## 2. 当前原子化状态与目标状态

| 原子 | 当前事实 | 当前状态 | 本轮闭环目标 |
| --- | --- | --- | --- |
| 输入 Input | Current Workspace 输入 workspace path；ChatArtifact 有 path/artifact id | 局部闭环 | workspace path 与 artifact id 都进入明确 preview API |
| 权威 Authority | Office editor-config 有 workspace authority；artifact 有独立 snapshot authority | 局部闭环 | workspace preview 复用 `authorize_workspace_path`；artifact preview 复用 `ChatArtifact` resource authority，禁止 path 冒充 artifact |
| 执行 Execution | OfficeCLI 有 HTML 能力，但 Hive 参数错误；UI 只执行 ONLYOFFICE | 断点 | 唯一渲染入口为受限 `OfficeCLIAdapter.run_view()`；UI 不再调用 ONLYOFFICE |
| 证据 Evidence | 无 OfficeCLI HTML render receipt/cache manifest；ONLYOFFICE callback 是旧证据 | 断点 | source hash、renderer version、preview mode、cache hit、duration、output bytes、error type 进入结构化日志/span 与 preview manifest |
| 恢复 Recovery | ONLYOFFICE callback/session 有旧恢复；HTML preview 无 retry/cache/fallback | 断点 | source hash 校验、原子缓存、单次 source-changed retry、text fallback、可重试 typed error |
| 消费 Consumption | 独立 Office 标签只消费 ONLYOFFICE；Office artifact 只下载 | 断点 | 删除独立标签；Current Workspace 与 Artifact inspector 都消费 OfficeCLI HTML/文本降级产物 |
| 验收 Acceptance | mock 单测未覆盖真实二进制契约；生产有 ONLYOFFICE 服务 | 断点 | 真实参数契约测试、权限测试、UI sandbox 测试、全量回归、三格式生产 smoke、删除后健康检查 |

当前总状态：

- OfficeCLI 用户预览：**断点**。
- ONLYOFFICE 在线编辑：**局部闭环但与当前产品需求不再匹配**。
- 本轮目标：OfficeCLI 预览达到 **闭环**，Office Online 达到 **完整退役**。

---

## 3. 目标架构

```mermaid
flowchart LR
    U["用户打开 Office 文件"]
    W["Current Workspace path"]
    A["ChatArtifact artifact_id"]
    AUTH["Workspace / Artifact Authority"]
    P["Office Preview API"]
    S["OfficeDocumentService"]
    C["OfficeCLIAdapter.run_view"]
    H["HTML primary"]
    T["Text fallback"]
    M["Derived preview cache + manifest"]
    B["Authenticated Blob"]
    I["sandbox iframe"]
    D["原文件下载"]

    U --> W
    U --> A
    W --> AUTH
    A --> AUTH
    AUTH --> P
    P --> S
    S --> C
    C --> H
    C -. "HTML infrastructure failure" .-> T
    H --> M
    T --> M
    M --> B
    B --> I
    I --> D
```

架构不变量：

1. DOCX/XLSX/PPTX 或 ChatArtifact snapshot 是内容事实源。
2. HTML 是 source hash 和 renderer version 可重建的派生物。
3. workspace preview 与 artifact preview 使用不同权威入口，不通过客户端 path 猜测 snapshot。
4. OfficeCLI 只通过命令白名单和 mode 白名单运行，不开放 shell 或任意位置参数。
5. HTML 不注入 React 主 DOM，不使用 `dangerouslySetInnerHTML`。
6. iframe 不获得 same-origin、forms、popup、top-navigation、download 权限。
7. 预览失败不影响 Agent 创建、读取、修改、校验或下载原文件。
8. Office Online 删除后不存在 callback、force-save、active editor session 或隐藏的外部文档服务依赖。

---

## 4. 目标接口与数据契约

### 4.1 OfficeCLI adapter 契约

禁止给通用 `run()` 增加任意 `positional_args`，避免把受限 adapter 重新变成命令拼接器。新增显式入口：

```python
def run_view(
    self,
    path: str | Path,
    *,
    mode: Literal[
        "outline",
        "text",
        "annotated",
        "stats",
        "issues",
        "html",
        "svg",
        "screenshot",
        "forms",
    ],
    options: dict[str, Any] | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    ...
```

命令必须精确组装为：

```text
[officecli, view, <path>, <mode>, --json, ...validated options]
```

约束：

- `mode` 必须在固定 allowlist；
- `options` 继续由 `_option_args()` 生成，不允许覆盖 command/path/mode；
- 环境继续强制 `OFFICECLI_SKIP_UPDATE=1`；
- binary SHA256 校验、timeout、invalid JSON、non-zero exit 继续保留；
- 真实 preview 不使用 `watch`，不在生产启动本地 HTTP server。

### 4.2 Preview service 契约

在 `OfficeDocumentService` 中增加单一 preview core：

```python
@dataclass(frozen=True)
class OfficePreviewResult:
    html: str
    preview_mode: Literal["html", "text_fallback"]
    source_sha256: str
    renderer_version: str
    cache_hit: bool
    output_bytes: int
```

行为：

1. 只接受 `.docx`、`.xlsx`、`.pptx`。
2. 渲染前计算 source SHA256。
3. 使用 `officecli view <file> html --json`。
4. 校验 `success is true`、`data` 为字符串、存在合法 HTML head、输出未超过资源上限。
5. 给 HTML 注入严格 CSP meta；若找不到预期 head 则 fail closed，不直接返回未知 HTML。
6. 渲染后再次计算 source SHA256；发生变化时丢弃结果并重试一次，再变化则返回 `office_preview_source_changed`。
7. HTML renderer infrastructure failure 时调用 `view text`，将纯文本进行 HTML escape 后包装为平台自有 `<pre>` 页面。
8. fallback 必须返回 `preview_mode=text_fallback` 并写 metric/span，不得静默伪装成完整 HTML。
9. HTML 和 text 都失败时返回 typed error，前端保留下载按钮。

资源约束：

- 继续使用 `OFFICECLI_TIMEOUT_SECONDS`；
- 新增 `OFFICECLI_PREVIEW_MAX_BYTES`，默认值在实现时根据真实 DOCX/XLSX/PPTX fixture 测量后固化；
- 超限返回 `office_preview_too_large`，不截断 HTML；
- API 中通过 threadpool 调用同步 OfficeCLI，不阻塞 FastAPI event loop；
- 日志不记录文档内容或完整 HTML。

### 4.3 派生缓存契约

复用现有 `.office_meta` sidecar，不新增数据库事实源：

```text
.office_meta/<document-digest>/preview/current.html
.office_meta/<document-digest>/preview/manifest.json
.office_meta/artifacts/<artifact-id>/preview/current.html
.office_meta/artifacts/<artifact-id>/preview/manifest.json
```

`manifest.json` 至少包含：

```json
{
  "source_sha256": "...",
  "renderer_version": "1.0.88",
  "preview_mode": "html",
  "preview_sha256": "...",
  "output_bytes": 12345,
  "generated_at": "..."
}
```

缓存规则：

- source hash 与 renderer version 同时匹配才可命中；
- 每个 workspace document / artifact 只保留 current preview，原子替换，不累积历史版本；
- ChatArtifact snapshot 被 cleanup 时同步清理对应 artifact preview sidecar；
- 缓存损坏只导致重建，不影响源文件；
- preview manifest 是机械证据，不是内容真相。

### 4.4 HTTP API 契约

Workspace preview：

```http
GET /api/agents/{agent_id}/office/preview?path=workspace/report.docx
Authorization: Bearer <access-token>
```

Artifact snapshot preview：

```http
GET /api/agents/{agent_id}/office/artifacts/{artifact_id}/preview
Authorization: Bearer <access-token>
```

响应：

```http
Content-Type: text/html; charset=utf-8
Cache-Control: private, no-store
Content-Disposition: inline
X-Content-Type-Options: nosniff
X-Office-Preview-Mode: html | text_fallback
X-Office-Source-SHA256: <hash>
X-Office-Renderer-Version: <version>
Content-Security-Policy: sandbox allow-scripts; default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; font-src data:
```

权威要求：

- workspace route 复用当前 `_authorize_office_request_path(..., action="read")` / `authorize_workspace_path()`；
- artifact route 按 `ChatArtifact.id + agent_id` 加载，复用 `authorize_resource_action(resource_kind="chat_artifact", action="read")`；
- artifact route 必须渲染 `snapshot_storage_path`，仅 legacy row 才允许当前 workspace fallback，并在响应/manifest 标记；
- 不接收客户端提交的 snapshot path；
- 不在 URL 放 access token、ONLYOFFICE token 或长期签名；
- 404、403、unsupported type、source changed、timeout、too large、renderer unavailable 使用可区分错误码。

### 4.5 前端消费契约

前端复用：

- `frontend/src/api/core/request.ts::getBlob()`；
- `frontend/src/utils/authenticatedResource.ts` 的 Blob 生命周期模式；
- React Query 的请求取消和 loading/error 状态；
- 当前 Artifact delivery-time snapshot 标识。

Current Workspace / Artifact inspector 行为：

1. 对话侧 Current Workspace 的“打开”请求 workspace preview Blob；有 artifact id 时请求 artifact snapshot preview。
2. 删除 Agent Detail 独立 Office Tag/标签页、Office Workbench、“保存”按钮、ONLYOFFICE disabled notice、DocsAPI loader 和 `OnlyOfficeHost`。
3. Agent 的 `office_document_create` / apply / query / validate 等能力继续保留，不依赖独立人类编辑标签。
4. Blob 转 object URL 后交给 iframe。
5. path 改变、重新打开、组件卸载时 revoke 旧 URL。
6. text fallback 显示明确降级提示。
7. 错误态同时提供重试和下载原文件。

iframe 必须为：

```tsx
<iframe
  src={previewUrl}
  sandbox="allow-scripts"
  referrerPolicy="no-referrer"
  title={documentName}
/>
```

禁止增加：

- `allow-same-origin`
- `allow-forms`
- `allow-popups`
- `allow-top-navigation`
- `allow-downloads`

Artifact 行为：

- `previewKind=office` 进入 `inspector_preview`，不再直接下载；
- 有 `artifact.id` 时必须请求 artifact snapshot preview；
- legacy 无 artifact id 时才请求 workspace preview；
- inspector 继续显示 snapshot/current-file 状态；
- 下载按钮继续下载原始 DOCX/XLSX/PPTX，不下载 HTML preview。

### 4.6 Evidence 与 observability 契约

每次 cache miss render 记录：

- tenant id、agent id、authority source；
- workspace/artifact source kind；
- source hash、renderer version、preview mode；
- cache hit/miss；
- render duration、output bytes；
- fallback reason 或 typed failure；
- invocation/span reference。

指标：

- `office_preview_requests_total{source_kind,preview_mode,status}`
- `office_preview_render_seconds{format,preview_mode}`
- `office_preview_cache_hits_total{source_kind}`
- `office_preview_failures_total{error_code,format}`
- `office_preview_output_bytes{format,preview_mode}`

不得记录：

- HTML body；
- 文档正文；
- JWT、Authorization header；
- 原 ONLYOFFICE secret；
- 用户提交文件中的任意敏感内容。

---

## 5. 单轮完整修改面

### 5.1 Backend production code

| 文件 | 精确修改 |
| --- | --- |
| `backend/app/services/officecli_adapter.py` | 增加受限 `run_view()`；修正 mode 位置参数；保留通用命令白名单、SHA256、timeout、JSON error contract |
| `backend/app/services/office_document_service.py` | 增加 preview result、HTML primary、text fallback、CSP hardening、source hash、原子 cache；删除 active editor session 模型和写入阻断 |
| `backend/app/api/office.py` | 保留 create；新增 workspace/artifact preview；删除 ONLYOFFICE callback/editor-config/force-save/download token/command helpers |
| `backend/app/api/files.py` | 抽取并复用 artifact snapshot target resolution，避免 Office artifact preview 复制 authority 或退回客户端 path |
| `backend/app/services/chat_artifact_delivery.py` | artifact snapshot cleanup 时同步删除其派生 Office preview sidecar |
| `backend/app/tools/handlers/office.py` | 保留 core OfficeCLI tools；删除 ONLYOFFICE active-session 描述和参数；`office_document_view` 使用真实 adapter contract |
| `backend/app/config.py` | 删除 `ONLYOFFICE_*` settings；新增明确的 Office preview resource limit setting |
| `backend/app/scripts/retire_onlyoffice_metadata.py` | 新增 idempotent dry-run/apply 脚本，清理 legacy manifest 的 `active_editor_session` |

`backend/app/api/office.py` 中计划删除的 ONLYOFFICE-only 符号：

- `OnlyOfficeCallback`
- `OfficeForceSaveIn`
- `_onlyoffice_secret`
- `_onlyoffice_command_secret`
- `_document_type_for_suffix`
- `_token_expiry`
- `make_document_token`
- `_verify_document_token`
- `_document_key`
- `_editor_user_identity`
- `_download_url`
- `_callback_url`
- `_document_command_url`
- `record_office_callback_event`
- `_authorize_office_token_payload`
- `get_editor_config`
- `download_document`
- `force_save_document`
- `_rewrite_to_internal_docs_url`
- `onlyoffice_callback`

删除前必须由测试证明这些符号没有非 ONLYOFFICE consumer；通用 workspace authority helper 与 `create_office_document` 保留。

### 5.2 Backend tests

| 文件 | Red/Green 目标 |
| --- | --- |
| `backend/tests/services/test_officecli_adapter.py` | Red 固定 `view <file> html --json`；拒绝未知 mode；保留 non-zero/invalid JSON/SHA256 tests |
| `backend/tests/services/test_office_document_service.py` | HTML success、text fallback、CSP、cache hit/stale、source changed、timeout、too-large、atomic write、legacy session field ignored/removed |
| `backend/tests/api/test_office_preview.py` | 新增 workspace + artifact preview 的 auth、RLS/resource authority、snapshot truth、headers、errors、fallback tests |
| `backend/tests/api/test_office_editor.py` | 删除 ONLYOFFICE editor/callback tests；仍有价值的 authority/token assertions 迁入 preview tests，不丢失安全覆盖 |
| `backend/tests/api/test_office_end_to_end.py` | 改为 create -> Agent edit -> preview -> original download/readback 闭环，不再依赖 callback |
| `backend/tests/api/test_resource_owned_surfaces.py` | 增加 foreign workspace/artifact preview deny 与 manager operator-view assertions |
| `backend/tests/tools/test_office_tools.py` | 确认 OfficeCLI create/view/query/apply/validate/dump 仍是 `agent_base`，Office Online 删除不削弱 Agent 能力 |
| `backend/tests/integration/test_officecli_binary_contract.py` | 使用 release/production 同版本二进制做真实 HTML/text contract smoke；release gate 不允许静默 skip |

### 5.3 Frontend production code

| 文件 | 精确修改 |
| --- | --- |
| `frontend/src/api/domains/office.ts` | 删除 editor config/force-save types 与 calls；增加 workspace/artifact preview Blob calls |
| `frontend/src/api/domains/files.ts` | 如 artifact preview 放在 files authority router，增加对应 `getBlob` adapter；不新增 query token |
| `frontend/src/pages/AgentDetail.tsx`、`agentDetailPolicy.ts` | 删除 Office Online Tag、独立标签页和 lazy section consumer |
| `frontend/src/pages/agent-detail/OfficeWorkbenchSection.tsx/.css` | 完整删除，不保留无入口 Workbench scaffold |
| `frontend/src/pages/agent-detail/ArtifactSurface.tsx` | `office` 进入 inspector；iframe preview；snapshot/fallback/degraded label；原文件下载保持不变 |
| `frontend/src/pages/agent-detail/AgentChatSection.tsx` | Office artifact 使用 preview Blob endpoint，不把原始 Office binary 当 iframe URL |
| `frontend/src/i18n/en.json` | 删除 ONLYOFFICE 文案；增加 preview/degraded/retry/download 文案 |
| `frontend/src/i18n/zh.json` | 同步中文文案 |

### 5.4 Frontend tests

| 文件 | Red/Green 目标 |
| --- | --- |
| `frontend/src/api/domains/office.test.ts` | preview URL、artifact id、URL encoding、getBlob auth adapter |
| `frontend/src/pages/agent-detail/AgentDetailSections.test.tsx` | Office 标签/路由不存在；Current Workspace 的 `onOpenDocument` 接入统一 preview consumer |
| `frontend/src/pages/agent-detail/ArtifactSurface.test.tsx` | office 进入 inspector；artifact snapshot 优先；iframe 无 same-origin；原文件下载不变 |
| `frontend/src/pages/agent-detail/AgentDetailSections.test.tsx` | 把当前 `office -> download` 断言改成 `office -> inspector_preview`，覆盖跨组件入口 |

### 5.5 Config、infra 与源码清理

| 文件/目录 | 修改 |
| --- | --- |
| `.env.example` | 删除所有 `ONLYOFFICE_*` 与 `ONLYOFFICE_PORT`；保留 OfficeCLI settings，新增 preview size setting |
| `docker-compose.yml` | 删除 `onlyoffice-documentserver` service、JWT env、backend ONLYOFFICE env、`onlyoffice_data`、`onlyoffice_logs` |
| `deploy/onlyoffice-documentserver/` | 删除 Dockerfile 和 Railway entrypoint；不保留未消费 scaffold |
| `backend/Dockerfile` | OfficeCLI 安装与 SHA256 校验继续保留，不引入 Office server |

### 5.6 Canonical docs 校正

| 文档 | 修改 |
| --- | --- |
| `docs/hive-living-object-native-surface-architecture-2026-07-10.md` | 将“ONLYOFFICE 保留策略”更新为本决策；OfficeCLI 是 Office 兼容渲染/操作层，浏览器预览不依赖外部编辑器 |
| `docs/hive-sota-master-goal.md` | Office target 从 ONLYOFFICE workbench 改为 OfficeCLI preview + original artifact interoperability |
| `docs/runtime-pool-isolation-plan-2026-07-02.md` | 添加 superseded note，说明生产拓扑删除 `onlyoffice-documentserver` 后的新事实 |
| 本文 | 实施后回填实际 commit、测试输出、deployment ids、production smoke 与删除 receipt |

历史审计和历史计划保留当时事实，不做无意义全仓重写；当前 canonical truth surface 必须同步。

---

## 6. Legacy data、清理与回滚

### 6.1 Legacy `.office_meta` 清理

现有 manifest 可能包含：

```json
{
  "active_editor_session": {
    "session_id": "...",
    "user_id": "...",
    "started_at": "..."
  }
}
```

新增脚本必须：

1. 默认 dry-run；
2. 只扫描 agent data root 下 `.office_meta/**/manifest.json`；
3. 只删除 `active_editor_session`，不修改 Office 文件、revision、operations；
4. JSON 无效时报告并跳过，不覆盖；
5. 使用同目录临时文件 + `os.replace()` 原子写；
6. 输出扫描数、需修改数、已修改数、错误数，不输出 session/user 值；
7. 重复 apply 结果为零修改；
8. `--apply --confirm` 才执行写入。

预期命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m app.scripts.retire_onlyoffice_metadata
python -m app.scripts.retire_onlyoffice_metadata --apply --confirm
```

### 6.2 回滚边界

删除 Railway 服务前的代码回滚：

- 回滚到上一部署 artifact；
- ONLYOFFICE 服务仍在，可恢复旧 editor path；
- preview cache 是派生物，可直接丢弃；
- legacy session metadata 即使已清理，旧编辑器重新打开即可建立新 session，不影响 Office 文件内容。

删除 Railway 服务后的回滚：

- 需要从 `onlyoffice/documentserver:9.3` 重新创建服务；
- 从受控 secret/config 系统恢复 JWT 与 URL 配置；
- 重新部署包含旧 editor/callback 代码的三个 Hive 服务；
- 原服务当前无 Railway volume，因此没有 DocumentServer volume 数据迁移，但服务配置、域名和 secrets 仍需重建。

不得把 secret 值写入本文、终端日志或 Git。

---

## 7. TDD 与验收矩阵

### 7.1 Red

必须先让以下测试针对当前实现失败：

1. adapter 期望 positional mode，当前 `--mode` 失败；
2. workspace preview route 当前 404；
3. artifact snapshot preview route 当前 404；
4. `previewKind=office` 当前返回 download；
5. Agent Detail 当前仍存在 Office Online 标签并加载 DocsAPI；
6. iframe sandbox/CSP 当前不存在；
7. legacy active editor session 当前仍阻断 Agent apply；
8. config/compose/production code 当前仍含 ONLYOFFICE references。

Red 定向命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_officecli_adapter.py \
  tests/services/test_office_document_service.py \
  tests/api/test_office_preview.py \
  tests/api/test_resource_owned_surfaces.py \
  tests/tools/test_office_tools.py -q

cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/api/domains/office.test.ts \
  src/pages/agent-detail/ArtifactSurface.test.tsx \
  src/pages/agent-detail/AgentDetailSections.test.tsx
```

### 7.2 Green + Refactor

定向回归：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_officecli_adapter.py \
  tests/services/test_office_document_service.py \
  tests/api/test_office_preview.py \
  tests/api/test_office_end_to_end.py \
  tests/api/test_resource_owned_surfaces.py \
  tests/tools/test_office_tools.py \
  tests/integration/test_officecli_binary_contract.py -q
ruff check app/ tests/

cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/api/domains/office.test.ts \
  src/pages/agent-detail/ArtifactSurface.test.tsx \
  src/pages/agent-detail/AgentDetailSections.test.tsx
npm run build
```

全量门：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q
ruff check app/ tests/

cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- --run
npm run build
```

必须记录实际零失败结果，不预填通过数量。

### 7.3 安全验收

- foreign tenant/user 无法预览 workspace 或 artifact；
- artifact id 不能被 path 替换或跨 agent 读取；
- HTML 不进入 `dangerouslySetInnerHTML`；
- iframe 没有 `allow-same-origin`；
- CSP 阻断 network、form、popup、top navigation；
- 文档中的 `<script>`、event handler、external image/font URL 不能获得主应用 token 或同源存储；
- preview URL 不含 JWT/token；
- Blob URL 在替换和卸载时被 revoke；
- source changed、timeout、too-large、missing binary 都是 typed error/fallback；
- preview failure 不阻断原文件下载和 Agent OfficeCLI tools。

### 7.4 三格式功能验收

DOCX：

- 标题、段落、表格、图片、分页可见；
- CJK/英文文本可见；
- HTML failure 时 text fallback 可见。

XLSX：

- sheet/cell/formula display 可见；
- 大表资源限制和错误可观测；
- 原 XLSX 可下载。

PPTX：

- 多 slide、图片、文本、基本布局可见；
- artifact snapshot 与当前 workspace 文件变化后仍显示 delivery snapshot；
- 原 PPTX 可下载。

### 7.5 源码退役验收

生产代码与 infra 预期无匹配：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
rg -n "ONLYOFFICE|OnlyOfficeHost|DocsAPI|onlyoffice-documentserver|onlyoffice_not_configured" \
  backend/app frontend/src .env.example docker-compose.yml deploy
```

预期：无匹配。历史 docs 不作为此命令的清理目标；canonical docs 通过人工复核确认已更新。

---

## 8. 完成定义

只有同时满足以下条件才能进入最终删除步骤：

- OfficeCLI 命令参数与 production 1.0.88 契约一致；
- 对话侧 Current Workspace 可查看 DOCX/XLSX/PPTX，Agent Detail 不再存在 Office Online 标签；
- ChatArtifact inspector 可查看 delivery-time Office snapshot；
- workspace 与 artifact 两条 authority 均有 deny tests；
- HTML iframe sandbox、CSP、Blob URL cleanup 均有测试；
- HTML/text fallback、timeout、too-large、source changed 均有 typed evidence；
- Agent 的 Office create/view/query/apply/validate/dump 不依赖 Office Online；
- legacy active editor metadata dry-run/apply 已完成并记录计数；
- ONLYOFFICE production code/config/compose/deploy scaffold 已删除；
- backend 定向、backend 全量、ruff、frontend 定向、frontend 全量、frontend build 全部零失败；
- `backend`、`backend-api`、`frontend` 均部署相同的新代码且状态 `SUCCESS`；
- production 三格式 smoke 通过；
- 删除 `ONLYOFFICE_*` 变量后 preview 仍通过；
- 已准备删除 receipt、回滚说明和明确人工确认。

---

## 9. 单轮完整施工顺序

以下是一次性交付顺序，不是 MVP 或分期 roadmap。

### Step 1：写 Red tests

先修改/新增第 7 节测试，运行并记录正确失败原因。不得先改实现再补测试。

### Step 2：修复真实 OfficeCLI view contract

实现受限 `OfficeCLIAdapter.run_view()`，让 `OfficeDocumentService.run_view()` 使用位置 mode；保留所有现有 sandbox/allowlist/SHA256/timeout/JSON error contract。

### Step 3：实现 preview core、cache、fallback 与 evidence

完成 source hash、renderer version、CSP hardening、原子 cache、source-changed retry、text fallback、resource cap、structured evidence；同步移除 active editor session 写阻断。

### Step 4：接通 workspace 与 artifact authority API

增加两条 preview route；workspace 走 workspace authority，artifact 走 delivery snapshot authority。复用现有 authenticated Blob，不增加 query token 或第二认证系统。

### Step 5：删除独立 Office 标签，接通 Current Workspace 与 Artifact inspector

删除 Office Online Tag/标签页与 Workbench；Current Workspace 和 Artifact `office` 统一进入 inspector preview；实现 sandbox iframe、URL revoke、degraded/error/retry/original-download；删除 DocsAPI 和在线保存 UI。

### Step 6：完整退役 ONLYOFFICE 源码、配置与 legacy metadata

删除后端 editor/callback/force-save/token、前端 OnlyOfficeHost、config、compose service/volumes、deploy scaffold；运行 metadata dry-run 和 apply；更新 canonical docs。

### Step 7：全量验证并形成可部署 HEAD

运行第 7 节所有定向/全量命令，检查 diff 无旁支修改。当前已有的 `AGENTS.md`、`CLAUDE.md`、`backend/tests/architecture/test_model_agency_no_semantic_truncation.py` 用户改动不得被覆盖或混入 Office 修改。由于生产部署使用 `git archive HEAD`，部署前必须确保本轮 Office 修改已进入目标 HEAD。

### Step 8：部署三个 Hive 服务并完成删除前生产验收

必须部署 `backend`、`backend-api`、`frontend`，不能只部署两个：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
tmp_root=$(mktemp -d /tmp/hiveclaw-railway-upload.XXXXXX)
mkdir -p "$tmp_root/backend-root" "$tmp_root/frontend-root"
git archive --format=tar HEAD backend | tar -xf - -C "$tmp_root/backend-root"
git archive --format=tar HEAD frontend | tar -xf - -C "$tmp_root/frontend-root"

cd "$tmp_root/backend-root"
railway up --service backend --environment production --project "$PROJECT_ID" --detach -m "deploy OfficeCLI preview and retire Office Online code"

cd "$tmp_root/backend-root/backend"
railway up --service backend-api --environment production --project "$PROJECT_ID" --detach -m "deploy OfficeCLI preview and retire Office Online code"

cd "$tmp_root/frontend-root"
railway up --service frontend --environment production --project "$PROJECT_ID" --detach -m "deploy OfficeCLI preview and retire Office Online code"
```

轮询并验收：

```bash
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
railway deployment list --service backend --environment production --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service backend-api --environment production --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service frontend --environment production --project "$PROJECT_ID" --limit 1 --json

curl -fsS https://backend-production-326d.up.railway.app/api/health
curl -I -fsS https://frontend-production-0346.up.railway.app/
```

删除前必须完成：

1. production DOCX/XLSX/PPTX Current Workspace preview；
2. production Office ChatArtifact snapshot preview；
3. foreign authority deny；
4. text fallback 可观测；
5. original file download；
6. backend/frontend logs 无 preview uncaught exception；
7. 新代码运行期间不再请求 `onlyoffice-documentserver`。

然后清理 Hive 服务中不再使用的变量。变量列表 JSON 含原始值，命令只允许通过 `jq` 输出 key，禁止保存完整 JSON：

```bash
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
railway variable list --service backend --environment production --project "$PROJECT_ID" --json \
  | jq -r 'keys[] | select(startswith("ONLYOFFICE_"))'
railway variable list --service backend-api --environment production --project "$PROJECT_ID" --json \
  | jq -r 'keys[] | select(startswith("ONLYOFFICE_"))'
```

只对实际存在的 key 执行：

```bash
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
railway variable delete ONLYOFFICE_DOCS_URL --service backend --environment production --project "$PROJECT_ID" --json
railway variable delete ONLYOFFICE_INTERNAL_DOCS_URL --service backend --environment production --project "$PROJECT_ID" --json
railway variable delete ONLYOFFICE_JWT_SECRET --service backend --environment production --project "$PROJECT_ID" --json
railway variable delete ONLYOFFICE_DOWNLOAD_TOKEN_EXPIRE_SECONDS --service backend --environment production --project "$PROJECT_ID" --json
```

如果 `backend-api` 也存在相同 key，使用同样命令并将 `--service` 改为 `backend-api`。变量删除可能触发部署；等待最终 deployment `SUCCESS` 后再次确认预览不依赖这些变量。

### Step 9：最终删除 Office Online 线上服务并核验退役结果

⚠️ **HIGH-RISK OPERATION**

后果：Railway `onlyoffice-documentserver` 的服务配置、deployment、公开 URL 将从 production environment 删除。当前核对无 Railway volume，但服务本身仍不可原地恢复；回滚需要重新创建 image service 并从安全配置源恢复 secrets。

执行前硬门：

- Step 1 至 Step 8 全部有实际证据；
- 三个 Hive 服务均 `SUCCESS`；
- `ONLYOFFICE_*` 已从 Hive 服务清理；
- production preview 在变量缺失状态下通过；
- 重新读取 service list，确认目标名称和 ID；
- 获得用户针对删除 production service 的明确确认。

先重新确认目标：

```bash
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
railway service list --environment production --project "$PROJECT_ID" --json
```

确认唯一目标为 `onlyoffice-documentserver` 后，执行最终删除：

```bash
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
railway service delete \
  --service onlyoffice-documentserver \
  --environment production \
  --project "$PROJECT_ID" \
  --yes \
  --json
```

删除 receipt 必须保存到本轮执行证据，随后只读核验：

```bash
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
railway service list --environment production --project "$PROJECT_ID" --json
railway deployment list --service backend --environment production --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service backend-api --environment production --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service frontend --environment production --project "$PROJECT_ID" --limit 1 --json

curl -fsS https://backend-production-326d.up.railway.app/api/health
curl -I -fsS https://frontend-production-0346.up.railway.app/
```

最终完成证据必须同时表明：service list 不再包含 `onlyoffice-documentserver`、三个 Hive 服务仍为 `SUCCESS`、健康检查成功、production DOCX/XLSX/PPTX 预览仍可用。

---

## 10. 实施证据回填（2026-07-14）

### 10.1 Step 1–7 已完成并提交

最终可部署提交链：

```text
ab6c19c42df045a62be8e1e6f534328a8479a26f Replace Office Online with OfficeCLI previews
09fcca1aa1e49ace9db335e1216845418b0ce27b Fix OfficeCLI structured text previews
c215de324d68e45c723fdb7fc541e3edda109310 Align DOCX text coverage with OfficeCLI
29172588ba164ae340f51abc0580ea502d11f446 Resolve verified OfficeCLI binary from PATH
33fbecd9d8021685aa2471114113b1edcc740b98 Commit Office authority before create response
```

生产 smoke 反向发现并补齐了四个 mock 未覆盖的真实断点：OfficeCLI 三格式结构化 text payload、DOCX 嵌套节点 coverage、带 SHA 校验的 PATH binary 解析，以及 create 成功响应前 authority commit 的立即预览竞态。

仓库当前策略在 `.gitignore` 中将 `docs/` 定义为本地内部文档，因此本文与 canonical docs 保留在共享 workspace，不强行加入 Git；所有生产代码与测试均已进入上述提交。

- Red：后端新增契约/preview/metadata/retirement 测试先得到 `12 failed, 5 passed`；前端先得到 `5 failed, 106 passed`，失败点分别对应错误 CLI 参数、缺失 preview API、Office artifact 仍下载、Office 标签仍存在与 iframe 隔离缺失。
- OfficeCLI adapter：`view <file> <mode> --json` 使用固定 mode allowlist；保留 binary hash、timeout、JSON/non-zero contract；missing binary 与 timeout 均映射为 typed error。
- Preview core：HTML primary、escaped text fallback、CSP、source hash 二次核验、一次 source-changed retry、renderer-version/hash cache、25 MiB 无截断上限、typed failures 已接入。
- Authority：workspace preview 使用 workspace resource authority；artifact preview 使用 `ChatArtifact.id + agent_id` 和 resource authority，优先 delivery snapshot；声明过但丢失的 snapshot 不再旁路到当前 workspace。
- Evidence：preview manifest、结构化日志、Prometheus 指标与持久化 `office_preview` invocation span 均不记录正文；响应携带 source hash、renderer version、mode 与 trace id。
- Consumption：Agent Detail Office Online Tag/标签页及 Workbench 已删除；对话侧 Current Workspace 与 ChatArtifact inspector 使用 authenticated Blob + `sandbox="allow-scripts"` iframe；错误态保留 retry 与原文件下载；Blob URL 变更/卸载时 revoke。
- Retirement：editor-config/callback/force-save/token/session gate、前端 DocsAPI、`ONLYOFFICE_*` settings、Compose service/volumes、Railway deploy scaffold 已删除；legacy metadata 脚本默认 dry-run，只有 `--apply --confirm` 写入。
- Recovery：ChatArtifact GC 同步清理 orphan Office preview 派生缓存；缓存损坏只重建，不改源文件。

### 10.2 本地验收结果

```text
cd backend && source .venv/bin/activate && ruff check app tests
All checks passed!

cd backend && source .venv/bin/activate && pytest tests -q
6915 passed, 2 skipped in 238.77s

cd frontend && npm test -- --run
115 test files passed; 668 tests passed

cd frontend && npm run build
TypeScript + Vite build passed
AgentDetail: 290078 / 380000 bytes; gzip 81994 / 115000 bytes
Shared vendor: 591449 / 620000 bytes; gzip 186474 / 200000 bytes
```

本机的两个 skip 包含无 release binary 的真实 OfficeCLI integration gate；该 gate 已在 production 容器中实跑并通过。

### 10.3 Step 8：production 部署与删除前验收已完成

最终 production deployment：

| 服务 | Deployment ID | 状态 |
| --- | --- | --- |
| `backend` | `0e1f43f4-ec0f-4d33-87d4-bb88831772a6` | `SUCCESS` |
| `backend-api` | `1d560bc3-bd18-4d45-8714-1648b900274b` | `SUCCESS` |
| `frontend` | `b4dbeba0-589e-4997-b95d-524bb94d5487` | `SUCCESS` |

健康面：

- backend `/api/health`：`status=ok`、version `1.7.0`；RLS runtime role=`app_rls`、`superuser=false`、`bypassrls=false`、enforcement=`strict`。
- frontend：HTTP/2 `200`。
- production OfficeCLI：version `1.0.88`；默认 adapter 的 binary SHA 校验、PATH 解析和 HTML preview 均成功。

真实二进制合同：

```json
{
  "status": "ok",
  "version": "1.0.88",
  "formats": {
    "docx": {"html": true, "text": true, "csp": true, "service_preview_mode": "html", "output_bytes": 36620},
    "xlsx": {"html": true, "text": true, "csp": true, "service_preview_mode": "html", "output_bytes": 7540},
    "pptx": {"html": true, "text": true, "csp": true, "service_preview_mode": "html", "output_bytes": 19239}
  }
}
```

公开 API 的普通用户鉴权 smoke：

- 创建后不等待，DOCX/XLSX/PPTX 三格式 Current Workspace preview 均 `200`、`text/html`、mode=`html`、renderer=`1.0.88`，CSP header/meta 与 trace id 均存在。
- 输出大小：DOCX `35178` bytes、XLSX `6854` bytes、PPTX `19007` bytes。
- smoke 产生的 3 个文件、3 个 sidecar、3 个 authority row 已精确清理。
- ChatArtifact 普通 owner 路径第一个 delivery snapshot 候选即返回 `200`，`X-Office-Artifact-Source=delivery_snapshot`、mode=`html`、renderer=`1.0.88`、CSP/trace 正常，输出 `178592` bytes。
- foreign workspace 与 foreign artifact 均为 `403 workspace_resource_forbidden`；原文件下载 SHA256 与源/快照一致。
- 人工触发 HTML renderer failure 后得到 `text_fallback`；持久化 span 为 `status=ok`、`authority_source=synthetic_release_smoke`、`fallback_reason=OfficeCLIExecutionError`、renderer=`1.0.88`、output=`900` bytes。

生产前端分包读回：

```text
/assets/index-BpKqlPTk.js
/assets/AgentDetail-CqKdUYmP.js
```

- 包含 workspace preview endpoint、artifact preview endpoint 和 `sandbox="allow-scripts"` iframe。
- 不含 `onlyoffice`、`OfficeWorkbenchSection`、`DocsAPI`、`editor-config`、旧中英文 Office Online Tab 文案。
- 浏览器自动化 skill 的 setup plugin 两次失败并报 `Cannot redefine property: process`；因此没有伪造浏览器点击证据，改用生产分包读回、普通用户鉴权 API、delivery snapshot 与完整前端测试/build 共同验收消费链。

legacy metadata：

```text
dry-run: {"scanned":21,"needs_update":20,"updated":0,"errors":0}
apply:   {"scanned":21,"needs_update":20,"updated":20,"errors":0}
repeat:  {"scanned":21,"needs_update":0,"updated":0,"errors":0}
```

配置退役：

- 从 `backend` 与 `backend-api` 各删除 `ONLYOFFICE_DOCS_URL`、`ONLYOFFICE_INTERNAL_DOCS_URL`、`ONLYOFFICE_JWT_SECRET`、`ONLYOFFICE_DOWNLOAD_TOKEN_EXPIRE_SECONDS`；frontend 原本为 0。
- 配置面删除未自动替换旧实例，实际进程仍短暂读到 4 个 key；因此重新部署完整三服务。
- 最终 `backend`、`backend-api` 运行实例的 `ONLYOFFICE_*` key count 均为 `0`，变量缺失状态下再次完成三格式 contract、默认 adapter、Current Workspace 与 ChatArtifact preview。
- backend 新部署日志：Office preview failure=`0`、OnlyOffice=`0`、documentserver=`0`、Traceback=`0`、uncaught=`0`。

### 10.4 Step 9：production Office Online 服务已删除并验收

2026-07-14 删除前重新读取的唯一目标：

```text
service name: onlyoffice-documentserver
service id: e75cccb9-f46d-4f99-bd26-0d772438e7a4
deployment id: c281e493-2205-47e9-9c53-8c1a633666bc
status: SUCCESS
image: onlyoffice/documentserver:9.3
Railway volume mounts: []
```

用户已明确回复确认删除 production `onlyoffice-documentserver`。2026-07-14T06:00:29Z 前完成删除，Railway receipt：

```json
{
  "environmentName": "production",
  "id": "e75cccb9-f46d-4f99-bd26-0d772438e7a4",
  "name": "onlyoffice-documentserver",
  "unlinked": false
}
```

删除后最终验收：

- production service list 中 `onlyoffice-documentserver` count=`0`。
- `backend` deployment `0e1f43f4-ec0f-4d33-87d4-bb88831772a6`、`backend-api` deployment `1d560bc3-bd18-4d45-8714-1648b900274b`、`frontend` deployment `b4dbeba0-589e-4997-b95d-524bb94d5487` 均保持 `SUCCESS`。
- backend `/api/health` 为 `status=ok`、version=`1.7.0`，RLS role=`app_rls`、strict，所有 daemon healthy；frontend HTTP/2 `200`。
- 三个 Hive 服务配置面均无 `ONLYOFFICE_*`；backend 与 backend-api 进程环境 key count 均为 `0`。
- production OfficeCLI 1.0.88 的 DOCX/XLSX/PPTX HTML/text/CSP contract 再次全部 `ok`。
- 删除服务后，普通用户创建后立即预览三格式均为 `200`、mode=`html`、renderer=`1.0.88`、CSP/trace 正常；输出分别为 DOCX `35182`、XLSX `6862`、PPTX `19019` bytes；3 个测试文件、sidecar、authority row 均已清理。
- ChatArtifact owner delivery snapshot preview 为 `200`、source=`delivery_snapshot`、mode=`html`、renderer=`1.0.88`、CSP/trace 正常，输出 `178592` bytes。
- 删除后日志 audit：Office preview failure=`0`、OnlyOffice=`0`、documentserver=`0`、Traceback=`0`、uncaught=`0`。

最终状态：OfficeCLI 只读预览闭环，Office Online production 依赖完整退役。
