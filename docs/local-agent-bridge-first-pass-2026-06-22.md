# Local Agent Bridge First Pass

状态：设计稿，先确认文档再实现
日期：2026-06-22
范围：任意本地 agent runtime 与 Hive agent 的双向通信、单向文件上传、认证、会话回放；Claude Code / Codex 只是首批适配示例

## 0. 结论

第一版不要追完整 ACP / A2A JSON-RPC task server，也不要把 MCP 当唯一目标。

最快、最稳的落地方案是做一个 **Hive Local Agent Bridge**：

```text
本地 agent runtime
  - Claude Code
  - Codex
  - Claude Desktop
  - Cursor / Windsurf / custom agent
  -> Local Bridge
  -> Hive Gateway + ChatSession + Artifact
  -> Hive Agent / 本地 Agent
```

MCP 只作为本地客户端接入方式之一：

```text
任意支持 MCP 或可执行 CLI 的本地 agent client
  -> local MCP server wrapper
  -> Hive Local Bridge core
```

也就是说，产品语义是“本地 agent bridge”，底层第一版复用 Hive 现有 Gateway / ChatSession / Artifact 主链；MCP 是兼容层，不是主架构。

### 0.1 P0 硬指标

第一版只有两个不可降级硬指标：

1. **单向文件上传**：本地 bridge 可以把本地文件上传到 Hive；Hive 必须保存原文件、生成可引用 artifact，并在 ChatSession transcript 中展示。第一版不要求 Hive 把文件主动推送下载到本地。
2. **相互通信并可调用本地 agent 干活**：Hive 云端可以向本地 bridge 投递一项工作，任意已适配的本地 agent runtime 可以处理并把结果回传；本地 bridge 也可以主动向 Hive agent 发消息。这里的“通信”不是简单 ping，而是 `request -> local work -> result` 的闭环。

任何会拖慢这两个指标的能力都不进入 P0：完整 A2A JSON-RPC task server、双向文件同步、push 通知、本地公网入站、复杂 OAuth delegation 都后置。

## 1. 为什么不第一版做完整 ACP / A2A

### 1.1 当前 Hive 已有可用主链

当前代码里已经有：

- `backend/app/api/gateway.py`
  - `GET /api/v1/gateway/poll`
  - `POST /api/v1/gateway/report`
  - `POST /api/v1/gateway/send-message`
  - `POST /api/v1/gateway/heartbeat`
  - `GET /api/v1/gateway/setup-guide/{agent_id}`
- `backend/app/api/chat_sessions.py`
  - `POST /api/v1/agents/{agent_id}/sessions`
  - `POST /api/v1/agents/{agent_id}/sessions/{session_id}/runs`
  - `GET /api/v1/agents/{agent_id}/sessions/{session_id}/transcript`
- `backend/app/api/upload.py`
  - `POST /api/chat/upload`
  - 保存到 `workspace/uploads`
  - 文档文件可经 `DocumentConversionService` 转成 `workspace/.hive/document_conversions/{source_sha256}/content.md`
- `backend/app/services/interoperability.py`
  - A2A-style Agent Card 已有
  - `json_rpc_tasks` 仍标记 `not_exposed`
  - OAuth delegation / MCP resource-server flow 仍标记 `not_exposed`

这说明第一版最短路径是把本地桥接接到这些已存在的端点，而不是先补完整标准协议。

### 1.2 完整 A2A / ACP 会扩大第一版风险

完整 A2A / ACP 意味着新增或补齐：

- public task JSON-RPC endpoint
- task lifecycle contract
- streaming / SSE task events
- file parts and artifact references
- OAuth delegation or equivalent scoped delegation
- external agent trust model
- cross-owner collaboration policy
- replay / idempotency / cancel / retry semantics

这些能力长期需要，但不该阻塞“本地 agent runtime 与 Hive 互通”第一版。

### 1.3 直接采用成熟模式，不重新发明轮子

P0 采用三类成熟模式组合：

```text
Agent Skill pattern:
  用 Skill 承载安装手册、操作步骤、验证 checklist、故障处理。
  对应支持 Skill / instruction package 的本地 agent 的自然入口。

Local MCP STDIO pattern:
  本地 agent 通过 stdio MCP 调用 hive_status / hive_poll_inbox / hive_upload_file 等工具。
  MCP server 不自己发明身份，读取 CLI/token store 里的 bridge token。

CLI device-flow auth pattern:
  类似 GitHub CLI 的 `gh auth login`。
  CLI 打开浏览器，用户在 Hive Web 登录、选择/确认 agent；CLI 轮询直到拿到 scoped token。
```

这比自研桌面 app / deep link 更贴近当前用户习惯：

- 用户极大概率已经在某个本地 agent runtime 里。
- 用户自然会对本地 agent 说“帮我装这个 skill 并连到 Hive”。
- 本地 agent 可以按 Skill 自动执行 CLI/MCP 安装步骤。
- 浏览器授权页负责用户登录、租户、agent、权限确认。

参考模式：

- MCP 官方 authorization 规范：HTTP remote MCP 走 OAuth 2.1；local STDIO MCP 不走这套远程授权，而是从本地环境/凭据获取 credentials。
- GitHub CLI：`gh auth login` 默认 browser flow，完成后把 token 存系统 credential store；device flow 不要求本地开 callback server。
- Claude Code Skills：Skill 是可复用 instructions/scripts/resources，适合承载安装和操作 playbook。

### 1.4 MCP 不是主协议，但适合作为本地入口

MCP 很适合让 Claude Code / Claude Desktop 看到一组工具：

```text
hive_poll_inbox
hive_report_result
hive_send_message
hive_upload_file
hive_create_session
hive_start_run
hive_read_transcript
```

但 MCP 本身是 tool/resource/prompt protocol，不是 Hive 的 durable conversation source of truth。所有 MCP tool 调用产生的消息、文件、结果必须仍然落回 Hive 的 Gateway / ChatSession / Artifact。

## 2. Product Shape

### 2.1 用户看到的东西

用户在 Hive 中看到一个连接能力：

```text
Local Agent Bridge
  - Claude Code
  - Codex
  - Claude Desktop
  - Cursor / Windsurf
  - 自定义本地 agent runtime
```

每个 bridge 绑定到一个 Hive agent：

```text
Hive Agent
  -> Connections
     -> Local Bridge
        status: online / offline / needs_auth / revoked
        last_seen_at
        device_name
        capabilities:
          - receive_message
          - send_message
          - upload_file
          - read_transcript
```

### 2.2 对本地用户的体验

这个语境下，最符合用户心智的本地入口只有三个：

```text
Skill  -> 给本地 agent runtime 的安装和操作手册
MCP    -> 本地 agent 实际调用 Hive 的工具通道
CLI    -> 认证、配置、daemon、排障辅助
```

P0 主路径应该是用户对本地 agent 说一句：

```text
帮我安装 Hive Bridge skill，并把你连接到 Hive 里的这个 agent。
```

然后本地 agent 按 Skill 指南完成：

```text
install hive-bridge CLI/MCP
-> run hive-bridge login
-> open Hive browser auth/pairing
-> store bridge token locally
-> add MCP server config
-> verify hive_status
```

这里 Skill 是 onboarding/manual，不是 runtime tunnel；MCP 是 runtime tunnel；CLI 是本地 helper。不要把用户默认导向单独下载安装桌面 app。

本地模型能做：

- 查看 Hive 发来的任务
- 回复 Hive
- 主动给 Hive agent 或 human target 发消息
- 上传当前工作目录里的文件到 Hive
- 读取某个 session transcript

### 2.3 绑定对象

第一版的绑定对象不是“Claude Code 账号”或“Codex 账号”，也不是“某个 MCP server 配置本身”。

绑定对象是：

```text
Hive user + Hive tenant + Hive agent + local bridge device
```

Claude Code / Claude Desktop / Codex / 其他本地 agent runtime 只是本地 bridge 的调用方：

```text
Local agent runtime
  -> stdio MCP subprocess: hive-bridge mcp
  -> local bridge connection token
  -> Hive Gateway
```

因此后端认证的主体是 `local_agent_bridge_connection`，不是某个具体本地 agent 产品账号。前端展示的是“某用户通过某个本地 bridge connection 连接了 Local Bridge”，并可显示 `client_kind=claude_code|codex|claude_desktop|cursor|custom_agent`。

## 3. Architecture

```text
┌─────────────────────────────────────────────────────────┐
│ Local Machine                                           │
│                                                         │
│  Claude / Claude Code / Codex / local agent             │
│        │                                                │
│        │ MCP stdio or local CLI/API                     │
│        v                                                │
│  Hive Local Bridge                                      │
│    - auth/token store                                   │
│    - inbox poller                                       │
│    - file uploader                                      │
│    - MCP server wrapper                                 │
│    - local execution adapter                            │
│        │                                                │
└────────┼────────────────────────────────────────────────┘
         │ HTTPS
         v
┌─────────────────────────────────────────────────────────┐
│ Hive Backend                                            │
│                                                         │
│  Gateway API                                            │
│    - poll / report / send-message / heartbeat           │
│                                                         │
│  ChatSession / Transcript                               │
│    - replayable session surface                         │
│                                                         │
│  Upload / DocumentConversion / ChatArtifact             │
│    - file source and user-visible artifact references   │
│                                                         │
│  Native Agent Runtime                                   │
│    - RuntimeTask / web chat / tools / governance        │
└─────────────────────────────────────────────────────────┘
```

## 4. First-Pass Scope

第一版必须一次做完这些闭环，不做半成品：

1. 本地 bridge 可注册并认证。
2. Hive 能给本地 bridge 投递消息。
3. 本地 bridge 能 poll 消息并返回结果。
4. 本地 bridge 能主动向 Hive agent / human target 发消息。
5. 本地 bridge 能上传单个本地文件，Hive 能保存原文件、生成 artifact，并展示在 transcript 中。
6. Hive 能向本地 bridge 投递一项工作，本地 bridge 能把任务交给本地 Claude / Claude Code / Codex / local agent 处理，并通过 report 回传结果。
7. 所有 bridge 交互都能在 ChatSession transcript 里回放。
8. token 可撤销，bridge offline/last_seen 可见。
9. 权限按 agent / tenant / user scope 收窄，不暴露全租户万能 token。
10. 关键路径有后端测试和本地 bridge smoke test。

## 5. Backend Design

### 5.1 Reuse Gateway as v1 Transport

第一版继续用 Gateway 作为本地 agent 的传输层。

现有 `gateway_messages` 生命周期保持：

```text
pending -> delivered -> completed
```

新增文件和元数据支持，但不要改变基础状态机。

### 5.2 Binding and Trust Model

P0 不能只复用 `Agent.api_key_hash`。现有 OpenClaw API key 只证明“请求方持有某个 agent 的共享 key”，不能证明：

- 这个本地 bridge 是哪个登录用户绑定的。
- 这台本地设备是谁的。
- token 是否只允许连接某一个 agent。
- 管理员能否撤销某一台设备，而不是重置整个 agent key。

所以 Local Bridge 必须使用独立 connection 表和 pairing flow。旧 `X-Api-Key` 可以继续服务 legacy OpenClaw，但 P0 Local Bridge 不把它作为主认证模型。

后端信任规则：

1. Web 端 pairing 只接受当前登录用户的 JWT。
2. `pairing/start` 必须调用 `check_agent_access(db, current_user, agent_id)`，且 `access_level == "manage"` 才能创建 bridge connection。
3. `tenant_id`、`agent_id`、`user_id` 都由后端从当前用户和 agent 推导，不能从请求 body 信任。
4. Bridge runtime 请求只看 bearer token 对应的 `local_agent_bridge_connections` 行，不信任 header/body 里的 tenant/user/agent。
5. 每个 bridge token 只能访问自己 connection 绑定的 `tenant_id + agent_id`。
6. Gateway poll/report/send-message/upload 必须 pin RLS tenant context 到 connection.tenant_id。
7. Bridge token 原文只返回一次；数据库只存 hash。
8. Revoked / expired connection fail-closed。

最小权限策略：

```text
create pairing: current user must have manage access to the agent
poll/report/send-message/upload: bearer token must resolve to active connection for that agent
list/revoke connections: current user must have manage access; org_admin can revoke tenant-local connections
```

这保证的是“这个本地 agent 的能力来自某个登录用户批准的、本 tenant、本 agent 的 connection token”。系统无法通过网络魔法证明物理机器身份；它通过 user-approved pairing、token secrecy、device metadata、revocation 和 tenant-bound enforcement 来建立可审计绑定。

### 5.3 Data Model Changes

#### `gateway_messages`

新增：

```text
attachments_json jsonb not null default '[]'
client_message_id text null
metadata_json jsonb not null default '{}'
```

语义：

- `attachments_json`：消息关联的文件 / artifact refs；P0 只要求 local -> Hive 上传文件，Hive -> local 文件下载后置。
- `client_message_id`：本地 bridge 幂等键，防止重试重复发消息。
- `metadata_json`：bridge device、source app、capability hints、local correlation id。

`attachments_json` item：

```json
{
  "type": "file",
  "name": "report.pdf",
  "mime_type": "application/pdf",
  "size": 123456,
  "sha256": "...",
  "workspace_path": "workspace/uploads/report.pdf",
  "markdown_path": "workspace/.hive/document_conversions/<sha>/content.md",
  "source": "local_bridge_upload"
}
```

#### `local_agent_bridge_connections`

正式 connection 表是 P0 必须项，不能后置：

```text
id uuid primary key
tenant_id uuid null -- null until browser activation approves a target agent
agent_id uuid null -- null until browser activation approves a target agent
user_id uuid null -- null until browser activation approves a logged-in user
device_name text not null
client_kind text not null
device_fingerprint text not null
token_hash text not null
scopes jsonb not null default '[]'
status text not null -- active | revoked
last_seen_at timestamptz null
last_seen_ip text null
last_seen_user_agent text null
created_at timestamptz not null
revoked_at timestamptz null
expires_at timestamptz null
metadata_json jsonb not null default '{}'
```

第一版 token 格式：

```text
hb_<random>
```

只保存 hash，不保存明文。

#### `local_agent_bridge_pairing_sessions`

```text
id uuid primary key
tenant_id uuid not null
agent_id uuid not null
user_id uuid not null
pairing_code_hash text null
device_code_hash text null
status text not null -- pending | claimed | approved | exchanged | expired | cancelled
device_name text null
client_kind text null
device_fingerprint text null
requested_scopes jsonb not null default '[]'
approval_mode text not null default 'device_flow' -- device_flow | web_started | manual_code
preapproved_at timestamptz null
approved_connection_id uuid null
expires_at timestamptz not null
created_at timestamptz not null
claimed_at timestamptz null
approved_at timestamptz null
exchanged_at timestamptz null
metadata_json jsonb not null default '{}'
```

Device code、user code、pairing code 都只存 hash。过期 session 不能 approve / exchange。`exchange` 只有在 `tenant_id + agent_id + user_id` 都已经由 Web activation 写入后才允许成功。

### 5.4 API Changes

#### Pairing

```text
POST /api/v1/agents/{agent_id}/local-bridge/pairing/start
GET  /api/v1/agents/{agent_id}/local-bridge/pairing/{pairing_id}
POST /api/v1/local-bridge/pairing/init
POST /api/v1/local-bridge/pairing/claim
POST /api/v1/agents/{agent_id}/local-bridge/pairing/{pairing_id}/approve
POST /api/v1/local-bridge/pairing/{pairing_id}/exchange
GET  /api/v1/agents/{agent_id}/local-bridge/connections
POST /api/v1/agents/{agent_id}/local-bridge/connections/{connection_id}/revoke
```

认证方式：

```text
start/get/approve/list/revoke:
  Web JWT required
  check_agent_access required
  manage access required for start/approve/revoke

init/claim/exchange:
  device_code or pairing_code required
  no Hive user JWT required
  rate-limited by pairing_id, IP, and code failures
  cannot change tenant_id/agent_id/user_id
```

本地 agent runtime 主路径使用 device-flow style pairing。`hive-bridge login` 先创建本地 pairing request：

```text
POST /api/v1/local-bridge/pairing/init
```

返回：

```json
{
  "pairing_id": "...",
  "device_code": "...",
  "user_code": "ABCD-1234",
  "verification_uri": "https://hive.example.com/local-bridge/activate",
  "verification_uri_complete": "https://hive.example.com/local-bridge/activate?user_code=ABCD-1234",
  "expires_at": "...",
  "interval_seconds": 5
}
```

CLI 自动打开 `verification_uri_complete`。用户在浏览器登录 Hive、选择 tenant/agent、确认授权后，后端把 `tenant_id + agent_id + user_id` 写入 pairing session。

本地 bridge 执行 `claim`，只提交设备信息，不能提交 tenant/user/agent：

```json
{
  "pairing_id": "...",
  "device_code": "...",
  "device_name": "Rocky MacBook Pro",
  "client_kind": "claude_code",
  "device_fingerprint": "sha256:..."
}
```

Web UI 轮询 pairing session，显示 claimed device：

```json
{
  "status": "claimed",
  "device_name": "Rocky MacBook Pro",
  "client_kind": "claude_code",
  "device_fingerprint": "sha256:...",
  "requested_scopes": ["message:read", "message:write", "file:upload", "session:read"]
}
```

用户在浏览器 approve 后，本地 bridge 使用 `device_code` exchange 获取一次性 token：

```json
{
  "bridge_token": "hb_...",
  "agent_id": "...",
  "base_url": "https://...",
  "scopes": ["message:read", "message:write", "file:upload", "session:read"]
}
```

如果 CLI 无法自动打开浏览器，才显示 `user_code` 作为 fallback。用户仍然不是复制长期密钥；`device_code/user_code` 都短期、一次性，只能完成当前 pairing。真正 token 只返回给本地 `hive-bridge`，并且可撤销。

#### Gateway Auth

现有：

```text
X-Api-Key: <openclaw key>
```

新增：

```text
Authorization: Bearer hb_<token>
```

认证解析顺序：

1. `Authorization: Bearer hb_...` -> local bridge connection
2. `X-Api-Key` -> legacy OpenClaw path

两者都必须 pin tenant RLS context。

Local Bridge runtime path 必须返回一个统一 auth context：

```text
BridgeAuthContext {
  tenant_id
  agent_id
  user_id
  connection_id
  device_name
  client_kind
  scopes
}
```

后续 Gateway / upload / transcript read 都只能使用这个 context，不允许 body 中覆盖。

#### Backend Identity Resolution

后端必须把 Web 管理面和 Bridge runtime 面分成两套身份来源：

```text
Web setup/admin request
  Authorization: Bearer <user_jwt>
  X-Tenant-Id: <frontend selected tenant>
  -> get_current_user()
  -> check_agent_access(current_user, agent_id)
  -> derive tenant_id from Agent.tenant_id
  -> derive user_id from current_user.id

Bridge runtime request
  Authorization: Bearer hb_<bridge_token>
  -> get_bridge_auth_context()
  -> derive tenant_id/user_id/agent_id/connection_id from local_agent_bridge_connections
  -> ignore X-Tenant-Id, body.tenant_id, body.user_id, body.agent_id
```

具体约束：

1. `pairing/start` path 里的 `agent_id` 是唯一 agent 输入；body 里不允许传 `tenant_id` / `user_id`。
2. `pairing/start` 读取 agent 后，以 `agent.tenant_id` 写入 pairing session。
3. `pairing/start` 以 `current_user.id` 写入 pairing session 的 `user_id`。
4. `pairing/claim` 只能更新设备信息，不能改变 session 的 tenant / agent / user。
5. `pairing/approve` 必须再次校验当前登录用户仍对该 agent 有 `manage` 权限。
6. `pairing/exchange` 只能给 `approved` session 发 token，且 token 行继承 pairing session 的 tenant / agent / user。
7. `poll/report/send-message/upload/read-transcript` 必须先解析 `BridgeAuthContext`，再用 context 内的 `agent_id` 和 `tenant_id` 查询；请求 body 里的目标 agent 只能作为业务 target，不能作为认证 target。
8. `connection_id` 必须写入 gateway message / transcript / artifact metadata，便于审计“哪个用户的哪台本地 bridge 产生了这条记录”。
9. `device_code` 只用于 claim/exchange 当前 pairing，不能用于任何 Gateway runtime API。
10. `device_code`、`user_code` 和 `pairing_code` 都必须一次性使用；exchange 成功后 pairing session 进入 `exchanged`，再次 exchange 返回 `409`。

这意味着即使本地 MCP client 传了伪造的 `tenant_id`、`user_id` 或另一个 `agent_id`，后端也不会采用。最终绑定关系只来自服务器端 pairing session 和 connection row。

#### Backend Request Mapping

P0 推荐新增一个独立 dependency，而不是把逻辑塞进每个 endpoint：

```text
get_bridge_auth_context(request, db) -> BridgeAuthContext
```

它负责：

- 验证 `hb_` token hash。
- 校验 connection `status == active`。
- 校验 `expires_at` 未过期。
- 设置 tenant RLS context 为 `connection.tenant_id`。
- 加载 bound agent，并确认 agent 仍存在、仍在同 tenant。
- 校验 scope，例如 `file:upload`、`message:read`、`message:write`。
- 更新 `last_seen_at`、`last_seen_ip`、`last_seen_user_agent`。

所有 Local Bridge runtime endpoint 都必须依赖这个 context：

```text
GET  /api/v1/gateway/poll                      -> requires message:read
POST /api/v1/gateway/report                    -> requires message:write
POST /api/v1/gateway/send-message              -> requires message:write
POST /api/v1/gateway/heartbeat                 -> requires heartbeat
POST /api/v1/agents/{agent_id}/local-bridge/files -> requires file:upload and path agent_id == context.agent_id
GET  /api/v1/agents/{agent_id}/sessions/{session_id}/transcript -> requires session:read and path agent_id == context.agent_id
```

如果 path `agent_id` 与 `context.agent_id` 不一致，直接 `403`。不要用 token 所属 agent 自动改写 path，因为这会隐藏前端或客户端 bug。

#### Gateway Payload

`GatewayMessageOut` 增加：

```json
{
  "attachments": [],
  "metadata": {}
}
```

`GatewayReportRequest` 增加：

```json
{
  "message_id": "...",
  "result": "...",
  "attachments": [],
  "client_message_id": "..."
}
```

`GatewaySendMessageRequest` 增加：

```json
{
  "target": "...",
  "content": "...",
  "channel": "agent",
  "attachments": [],
  "client_message_id": "..."
}
```

#### Cloud-to-Local Work Request Contract

P0 的“云端调用本地 agent 干活”不要新建完整 task server。最快稳定做法是复用 `gateway_messages`：

```text
Hive cloud
  -> create gateway_messages row
     agent_id = bound local bridge agent
     status = pending
     content = human-readable instruction
     metadata_json.kind = "work_request"
  -> local bridge poll
  -> local execution adapter runs the work
  -> gateway/report writes result + attachments
  -> ChatSession transcript records request/result
```

`metadata_json` 对 work request 的最小结构：

```json
{
  "kind": "work_request",
  "work_id": "uuid",
  "source": "cloud_agent|human|workflow",
  "requested_capabilities": ["code_edit", "file_upload"],
  "input_artifacts": [],
  "reply_required": true,
  "timeout_seconds": 1800
}
```

Local Bridge 收到 `kind == "work_request"` 后有两种 P0 执行入口：

```text
hive-bridge run
  -> daemon/poller mode
  -> dispatch to configured local execution adapter
  -> report result automatically

hive-bridge mcp
  -> local agent interactive mode
  -> MCP client calls hive_poll_inbox
  -> local model decides work
  -> MCP client calls hive_report_result
```

因此本地 agent 的绑定方式不是“把某个本地 agent 产品账号直接绑定到 Hive”，而是：

```text
local agent runtime
  -> calls hive-bridge mcp or local adapter script
  -> hive-bridge uses bridge token
  -> Hive authenticates local_agent_bridge_connection
```

如果用户需要云端自动把任务交给本地 agent，必须运行 `hive-bridge run` 或同等 daemon。只配置 `hive-bridge mcp` 时，本地 agent 仍可处理云端任务，但触发点是本地 MCP client 主动 poll；Hive 不能直接 push 进任意本地 agent 进程。

### 5.5 File Upload

P0 只实现 **local -> Hive 单向上传**。这足以满足任意本地 agent runtime 把工作产物、证据文件、报告、截图、表格上传回 Hive 的核心场景。

复用：

```text
POST /api/chat/upload
```

但新增 bridge-friendly endpoint 更清晰：

```text
POST /api/v1/agents/{agent_id}/local-bridge/files
```

内部复用 upload service，不新造文件解析逻辑。

返回：

```json
{
  "name": "report.pdf",
  "size": 123456,
  "workspace_path": "workspace/uploads/report.pdf",
  "conversion": {
    "status": "converted",
    "markdown_path": "workspace/.hive/document_conversions/<sha>/content.md",
    "metadata_path": "workspace/.hive/document_conversions/<sha>/metadata.json",
    "source_sha256": "..."
  }
}
```

规则：

- 文档统一转 Markdown artifact。
- 图片保留 `image_data_url` 或 image artifact ref。
- 不把本地绝对路径写入 transcript。
- 不允许 `../`、绝对路径、symlink escape。
- 每次上传先按单文件实现；多文件批量、目录上传、Hive -> local 下载不进入 P0。

### 5.6 Transcript Contract

每个 bridge 消息必须绑定 ChatSession。

最低要求：

- Hive -> local bridge 的投递写入 session。
- local bridge -> Hive 的 report 写入 session。
- local bridge 主动 send-message 也写入 session。
- local -> Hive upload 的 attachments 以 `ChatArtifact` 或 transcript event part 出现。
- `GatewayMessage.result` 只能是状态摘要，不是唯一 completion surface。

这延续“ChatSession 是 primary replay surface”的现有原则。

## 6. Local Bridge Design

### 6.0 Local Entry Shape

P0 本地交付形态是 **Skill + MCP + CLI**，三者都要有明确职责：

```text
Hive Bridge Skill:
  给任意本地 agent runtime 的安装和操作手册。
  负责告诉本地 agent 如何安装 MCP、如何登录 Hive、如何验证连接、如何上传文件、如何处理 Hive inbox。

Hive Bridge MCP:
  本地 agent 调用 Hive 的 runtime tool surface。
  暴露 hive_status / hive_poll_inbox / hive_report_result / hive_send_message / hive_upload_file 等工具。

hive-bridge CLI:
  本地 helper。
  负责 login、token store、MCP server stdio、可选 daemon run、status、logout、diagnostics。
```

P0 不是“只交付一个 Skill”，也不是“只交付一个 MCP”。正确交付是：

```text
Skill 让本地 agent 知道怎么装和怎么用
MCP 让本地 agent 真正能调 Hive
CLI 负责认证和本地进程能力
```

更准确地说，P0 的协议层必须 agent-agnostic：

```text
Hive backend API
  <- same for every local agent

hive-bridge CLI/MCP
  <- same binary and same tool schema

Agent adapter
  <- only handles local install/config differences
```

首批 adapter 可以覆盖 Claude Code 和 Codex，但不允许在后端、token、message schema、artifact schema 里写死任何一个产品。

### 6.1 Package Shape

```text
local_bridge/
  pyproject.toml
  hive_bridge/
    __init__.py
    cli.py
    config.py
    client.py
    auth.py
    poller.py
    files.py
    mcp_server.py
    local_store.py
```

### 6.2 Install and Distribution

P0 目标是先把链路调通并上线可用，不在第一版承担完整独立安装包、签名、公证、多平台发行成本。

安装策略分两档：

```text
P0 / first usable release:
  simplest working local package
  acceptable channels: npm, pipx, brew tap, or downloadable script/binary
  optimized for getting Claude Code / Codex / generic MCP users connected quickly
  user can ask local agent to run the install steps from the Skill

Skill entry:
  Hive Bridge Skill package
  includes install instructions and verification checklist
  tells local agent runtimes how to install or download hive-bridge

Runtime:
  hive-bridge local executable
  provides `hive-bridge mcp` stdio server
  stores token in Keychain or 0600 config

P1 / production packaging:
  standalone native binary or OS installer
  no Node.js required
  no Python required
  no compiler required
  no package manager required
  signed/notarized macOS package
  broader Windows/Linux packaging
```

P0 安装方式优先级：

```text
1. Use the fastest package path that works in our current engineering stack.
2. Prefer one command that local agent can execute:
   npm install -g @hive/bridge
   or pipx install hive-bridge
   or brew install hive-bridge
   or curl/download a single binary.
3. Skill must detect what is available locally and choose the simplest path.
4. If none exists, Skill should point to manual download instructions.
```

P0 package registration decision:

```text
Do not make public package registration a P0 blocker.

Recommended P0 path:
  1. Build hive-bridge inside this monorepo as local_bridge/.
  2. Support local dev install first:
     pipx install -e ./local_bridge
     or python -m pip install -e ./local_bridge
  3. Support internal/alpha install from Git:
     pipx install "git+https://github.com/<org>/<repo>.git#subdirectory=local_bridge"
  4. For users without repo access, publish a GitHub Release artifact or internal download URL.
  5. Register npm/PyPI/brew only after CLI commands, auth flow, and MCP tool schema are stable.

Why:
  - P0 needs protocol validation and real local-agent linking, not package ecosystem polish.
  - Public registry names are hard to rename after adoption.
  - A Git or Release artifact is enough for Skill-driven installation during alpha.
  - P1 can add standalone signed installers and public registry distribution.
```

如果要提前占包名，可以单独 reserve namespace，但不要把它放进 P0 critical path。P0 的验收标准是 `hive-bridge login/mcp/run` 能被本地 agent 安装并跑通，不是用户能从公开 registry 搜到包。

P0 Skill should use this install ladder:

```bash
# 1. Local development / internal dogfood from checked-out repo
cd /path/to/hiveclaw-main
python3 -m pip install -e ./local_bridge
hive-bridge status

# 2. Alpha users with repository access
python3 -m pip install --user "git+https://github.com/<org>/<repo>.git#subdirectory=local_bridge"
hive-bridge status

# 3. Cleaner isolated install when pipx is available
pipx install "git+https://github.com/<org>/<repo>.git#subdirectory=local_bridge"
hive-bridge status

# 4. Later public package path, only after post-alpha stabilization
pipx install hive-bridge
# or
npm install -g @hive/bridge
```

The Skill must not require the user to understand these choices. It should try the easiest available path, verify `hive-bridge status`, then continue to `hive-bridge login`.

P1 平台目标：

```text
macOS arm64 + x64:
  signed + notarized .pkg or .dmg

Linux x64:
  standalone tar.gz and optional .deb/.rpm

Windows:
  signed installer or zip
```

包内必须包含：

```text
hive-bridge
  login
  mcp
  run
  service install/start/status/stop
  status
  logout
```

`hive-bridge mcp` 仍是本地 stdio MCP server；P0 的重点只是确保这个命令能被装到用户机器并被本地 agent 启动。

面向用户的实际说法：

```text
对本地 agent 说：
"帮我安装 Hive Bridge skill，并连接到 Hive。"
```

Skill 内的安装手册必须让本地 agent 做这些动作：

- 检测当前环境：Claude Code / Codex / Claude Desktop / Cursor / Windsurf / shell / unknown。
- 检测可用安装方式：npm / pipx / brew / downloadable binary。
- 选择当前机器上最容易成功的方式。
- 安装 `hive-bridge`。
- 执行 `hive-bridge login --base-url <Hive URL>`。
- 引导用户完成浏览器登录和 agent 选择。
- 执行当前 runtime 对应的 MCP 配置命令，例如 `claude mcp add hive-local -- hive-bridge mcp`，或写入等价 MCP config。
- 调用 `hive_status` 验证连接。
- 可选启动 `hive-bridge run` 作为无人值守 daemon。

完整桌面 app、签名安装包、browser extension 可以后置，不是 P0 主路径；P0 的硬要求是有一个可执行的 `hive-bridge` 本地包能把链路跑通。

### 6.3 Skill, MCP, and CLI Entrypoints

Skill 用户入口：

```text
本地 agent runtime:
  "帮我安装 Hive Bridge skill，并连接到 Hive。"
```

MCP runtime 入口：

```bash
claude mcp add hive-local -- hive-bridge mcp
```

不同 runtime 的 MCP 配置方式由 adapter 决定：

```text
claude_code:
  install command: claude mcp add hive-local -- hive-bridge mcp

codex:
  install command/config: use Codex-supported MCP config surface

claude_desktop:
  install command/config: edit Claude Desktop MCP config

custom_agent:
  install command/config: expose stdio command ["hive-bridge", "mcp"]
```

CLI helper 入口：

```bash
hive-bridge login --base-url https://try.hive.ai
hive-bridge status
hive-bridge run
hive-bridge mcp
hive-bridge logout
```

`hive-bridge run` 是云端调用本地 agent 的 P0 daemon 入口。它不暴露公网端口，只长轮询 Gateway，并把 `work_request` 交给本地 adapter。

本地 adapter 第一版使用通用脚本契约，避免把实现绑死到某个厂商 CLI：

```text
stdin:  JSON work request, transcript context, artifact refs
stdout: JSON result, summary, attachments
exit 0: success
exit non-zero: failure result, stderr captured as error metadata
```

示例配置形态：

```json
{
  "execution_adapter": {
    "type": "command",
    "command": ["/usr/local/bin/hive-local-worker"]
  }
}
```

后续可以加 Claude Code / Codex / Claude Desktop 专用 adapter，但 P0 不把 vendor-specific CLI 作为协议基础。

### 6.4 Adapter Registry

Adapter 只解决本地 agent runtime 的安装和配置差异，不改变 Hive 协议。

```text
adapter_id
display_name
detect()
install_bridge()
configure_mcp()
verify_mcp()
supports_daemon_hint
notes
```

P0 adapter：

```text
generic_mcp_stdio:
  适用于任何能配置 stdio MCP server 的本地 agent。
  command = ["hive-bridge", "mcp"]

claude_code:
  在 generic_mcp_stdio 基础上提供 Claude Code 的安装命令和验证步骤。

codex:
  在 generic_mcp_stdio 基础上提供 Codex 的 MCP 配置说明。
```

所有 adapter 最终都必须落到同一组 MCP tools 和同一套 bridge token。`client_kind` 只是观测和 UI 标签，不参与授权决策。

### 6.5 Repository and Deployment Topology

P0 不需要单开一个云端 MCP server，也不需要单独部署一个 remote MCP 服务。

正确拓扑：

```text
Local agent runtime
  -> local stdio MCP server: hive-bridge mcp
  -> HTTPS
  -> existing Hive backend
     - pairing/init / activate / exchange
     - gateway poll/report/send-message/heartbeat
     - local-bridge file upload
     - transcript/session APIs
```

这里的 MCP server 是本机进程，不是云端服务：

```text
hive-bridge mcp
  runs on user's machine
  stdio transport
  launched by Claude Code / Codex / other local agent runtime
  reads token from local token store
  calls Hive backend over HTTPS
```

所以 P0 部署只有两类：

```text
Hive cloud backend/frontend:
  跟当前 Hive backend/frontend 一起部署。

hive-bridge local package:
  发布为 CLI/MCP 本地包。
  npm / brew / pipx / binary 都可以，P0 选择最快可落地的一条。
  这是分发 artifact，不是云端部署服务。
```

仓库策略：

```text
P0:
  放在当前 hiveclaw-main monorepo。
  原因是 backend API、frontend Local Agent Link、local bridge schemas/tests 需要一起演进。

Recommended layout:
  backend/
    app/api/local_bridge.py
    app/models/local_bridge.py
    tests/api/test_local_bridge_pairing.py
    tests/api/test_gateway_bridge_auth.py

  frontend/
    Local Agent Link card under existing ChannelConfig / AgentSettingsSection

  local_bridge/
    pyproject.toml or package.json
    hive_bridge/
      cli.py
      mcp_server.py
      auth.py
      client.py
      files.py
      adapters/
      skills/hive-bridge/SKILL.md
```

什么时候再单开仓库：

```text
Split later only when:
  - CLI/MCP package has independent release cadence.
  - external contributors need a small public repo.
  - package signing/release automation becomes noisy for hiveclaw-main.
  - stable API contracts exist and cross-repo versioning is manageable.

Do not split for P0:
  - 会增加版本漂移。
  - 会让 backend/frontend/local tests 很难一次闭环。
  - 会把 Local Agent Link 的 UI/backend/CLI contract 拆散。
```

未来可选 remote MCP：

```text
Remote MCP over HTTP/SSE:
  Not P0.
  Only consider if we need marketplace-style cloud MCP access,
  browser-only agents,
  or third-party remote clients that cannot run local stdio.

Even then:
  remote MCP should be a facade over the same Hive backend auth/session/artifact contracts,
  not a second source of truth.
```

结论：**当前仓库做 P0；不单独部署云端 MCP server；本地 `hive-bridge mcp` 是随 CLI 发布的 stdio MCP server。**

### 6.6 Network Model: No Local Inbound Server

P0 绝对不要求用户本地起公网服务器、配置反向代理、暴露 localhost、开端口、配置 ngrok/Tailscale/Cloudflare Tunnel。

网络模型只有一种：**本地主动出站连接 Hive**。

```text
Local agent runtime
  -> starts local stdio process: hive-bridge mcp
  -> hive-bridge reads local token
  -> hive-bridge makes outbound HTTPS requests to Hive
  -> Hive backend stores messages/files/results
```

云端调用本地 agent 干活不是“云端 HTTP 反连本地”，而是：

```text
Hive cloud writes pending work_request
  -> local hive-bridge polls /gateway/poll over outbound HTTPS
  -> local agent processes work
  -> local hive-bridge reports result over outbound HTTPS
```

这和 GitHub CLI、Slack desktop polling、很多 CI runner 的控制面模式接近：用户机器只发起出站请求，不接受入站请求。

P0 禁止项：

```text
No local public URL.
No reverse proxy.
No inbound webhook to user machine.
No required local HTTP callback server.
No port forwarding.
No ngrok-style setup.
No user-managed TLS cert.
```

`hive-bridge mcp` 里的 “server” 只是 MCP 术语：它是本机 stdio 子进程，不监听网络端口。

`hive-bridge run` 是可选常驻 poller：

```text
hive-bridge run
  -> outbound HTTPS polling
  -> no listening port
  -> no reverse proxy
```

如果未来要更实时，可以升级为 outbound long-poll / SSE / WebSocket：

```text
local hive-bridge -> opens outbound WebSocket to Hive
Hive -> sends events over that already-open outbound connection
```

即使这样，本地仍不需要入站服务。

### 6.7 Local Process Lifecycle

这里要区分两个“本地进程”。它们都来自同一个 `hive-bridge` 本地包，但启动方式不同。

```text
Process A: hive-bridge mcp
  类型：本地 stdio MCP 子进程
  谁启动：Claude Code / Codex / other local agent runtime
  什么时候启动：本地 agent 启动 MCP tools 时
  是否监听端口：否
  作用：让本地 agent 主动调用 Hive tools

Process B: hive-bridge run
  类型：本地常驻 runner / poller
  谁启动：hive-bridge CLI 或用户级后台服务
  什么时候启动：用户/本地 agent 按 Skill 开启无人值守连接时
  是否监听端口：否
  作用：持续出站 poll Hive，把云端 pending work_request 拉到本地执行
```

`hive-bridge mcp` 部署在哪里：

```text
不是云端部署。
不是 Hive backend 里的 service。
不是 remote MCP server。

它安装在用户本机：
  standalone binary / OS installer 安装 hive-bridge
  本地 agent MCP config 指向 command: ["hive-bridge", "mcp"]
  本地 agent 需要工具时启动这个 stdio 子进程
```

`hive-bridge run` 怎么起：

P0 需要提供 CLI 命令，让本地 agent 按 Skill 自动执行，不要求用户理解服务管理。

```bash
hive-bridge run
```

为了支持“云端可以调用本地 agent 干活儿”，还需要提供用户级后台服务安装命令：

```bash
hive-bridge service install
hive-bridge service start
hive-bridge service status
hive-bridge service stop
```

服务实现要求：

```text
macOS:
  user LaunchAgent
  runs hive-bridge run
  starts after user login

Linux:
  user systemd unit where available
  fallback to foreground run

Windows:
  scheduled task or user service later
```

P0 可以先实现 macOS + foreground fallback，但 Skill 必须把 capability 讲清楚：

```text
如果只配置 MCP:
  本地 agent 打开时可以主动收/发消息、上传文件、处理 inbox。
  Hive 云端可排队 work_request，但本地 agent 不在线时不会立即执行。

如果启动 hive-bridge run/service:
  本机有一个常驻出站 poller。
  Hive 云端可以投递 work_request。
  runner 拉取任务并交给 configured local execution adapter。
```

本地 runner 如何“调用本地 agent”：

```text
Option 1: interactive MCP mode
  local agent 通过 hive_poll_inbox 主动拿任务。
  适合用户正在用 Claude Code / Codex / other agent。

Option 2: command adapter mode
  hive-bridge run 调用一个本地命令 adapter。
  adapter stdin 接收 work_request JSON。
  adapter stdout 返回 result JSON。
  适合支持 headless/CLI 调用的本地 agent。
```

不能承诺所有本地 agent 都支持无人值守 headless 执行。P0 的通用保证是：

```text
所有适配 agent 都可以通过 MCP 主动通信和上传文件。
支持 command adapter 或本地服务模式的 agent 才能无人值守执行云端 work_request。
```

### 6.8 End-to-End User Flow

P0 按这条主流程走。

```text
1. 我们发布 hive-bridge 本地包
   npm / brew / pipx / binary 都可以，P0 选最快能上线的一条。
   里面包含：
   - hive-bridge login
   - hive-bridge mcp
   - hive-bridge run
   - hive-bridge service install/start/status/stop

2. 用户对本地 agent 说
   "帮我安装 Hive Bridge skill，并连接到 Hive。"

3. 本地 agent 按 Skill 做
   - 安装 hive-bridge
   - 配置 MCP：command = ["hive-bridge", "mcp"]
   - 执行 hive-bridge login

4. hive-bridge login 做 device-flow
   - 调 Hive backend 创建 pairing
   - 打开 Hive Web activation page
   - 用户登录 Hive
   - 选择目标 agent
   - 在该 agent 的 Local Agent Link card 点 Approve

5. CLI 拿到 hb_token
   - 存 Keychain 或 0600 config
   - 本地 agent 调 hive_status 验证连接

6. 本地 agent 主动通信
   - 本地 agent 启动 hive-bridge mcp
   - 通过 MCP 调 hive_poll_inbox / hive_send_message / hive_upload_file / hive_report_result

7. 如果要云端无人值守派活
   - 本地 agent 按 Skill 执行 hive-bridge service install/start
   - 本机有常驻 runner
   - runner 主动出站 poll Hive
   - Hive 写 pending work_request
   - runner 拉取任务并交给本地 adapter
   - runner report 回 Hive
```

### 6.9 What We Need To Build

P0 需要做这些，不多也不少。

Backend:

```text
1. local_agent_bridge_pairing_sessions 表
2. local_agent_bridge_connections 表
3. POST /local-bridge/pairing/init
4. Web activation / approve / reject / exchange APIs
5. Bridge bearer-token auth dependency: get_bridge_auth_context
6. Gateway poll/report/send-message 支持 Bearer hb_<token>
7. local -> Hive single-file upload endpoint
8. ChatSession transcript attachment/result 写入
9. tests/api/test_local_bridge_pairing.py
10. tests/api/test_gateway_bridge_auth.py
11. tests/api/test_upload_bridge_files.py
```

Frontend:

```text
1. AgentDetail -> Settings -> Channel / 消息渠道新增 Local Agent Link card
2. Activation page for user_code
3. Pending request UI: device/client/scopes + Approve/Reject
4. Connected list: device/client/user/last_seen/scopes + Revoke
5. Setup instruction modal: copy local-agent instruction
6. i18n en/zh
```

Local package:

```text
1. hive-bridge local executable package
2. hive-bridge login
3. hive-bridge mcp stdio server
4. hive-bridge run foreground poller
5. hive-bridge service install/start/status/stop
6. token store: Keychain or 0600 config
7. HTTPS client for pairing/gateway/upload/transcript
8. generic_mcp_stdio adapter
9. command execution adapter contract
10. Hive Bridge Skill package
11. local_bridge tests
12. P0 local package release path: local editable install + Git install + optional Release artifact
13. Public npm/PyPI/brew registration is P1 or post-alpha, not a P0 blocker
```

Release:

```text
P0 在当前 hiveclaw-main monorepo 里实现。
云端只部署现有 Hive backend/frontend。
本地发布 hive-bridge package，P0 优先简单可用：先支持 editable/Git install，必要时加 GitHub Release artifact。
公开 registry、standalone installer、signing 进入 post-alpha/P1。
不单独部署 remote MCP server。
```

### 6.10 Local Config

存储位置：

```text
~/.hive/bridge/config.json
~/.hive/bridge/upload_staging/
~/.hive/bridge/cache/
```

token 存储：

- macOS：Keychain。
- 无 Keychain 环境：0600 权限文件。
- 禁止写入 workspace、repo、prompt、普通 artifact。

### 6.11 MCP Tool Surface

本地 MCP server 暴露：

```text
hive_status
hive_poll_inbox
hive_report_result
hive_send_message
hive_upload_file
hive_create_session
hive_start_run
hive_read_transcript
```

MCP tool 不直接访问 Hive DB，不绕过 Hive API。

注意：MCP server 本身不是 push channel。它适合让本地 agent 主动调用 `hive_poll_inbox`、`hive_report_result`、`hive_upload_file`。如果产品要求“云端发起后本地无人值守执行”，必须使用 `hive-bridge run` daemon 或等价常驻进程。

### 6.12 Polling Behavior

默认轮询：

```text
interval: 10s
backoff: 10s -> 30s -> 60s
heartbeat: 60s
```

第一版不要求公网入站，不要求 local server 暴露给 Hive。这样可穿透公司网络、家用 NAT、VPN、移动网络。

## 7. Security

### 7.1 Scope

token scopes：

```text
message:read
message:write
file:upload
session:read
session:write
heartbeat
```

每个 token 绑定：

- tenant
- agent
- user
- device

不可跨 agent 使用。

### 7.2 Local File Safety

本地 bridge 只能上传用户显式指定文件，或 MCP client tool call 明确传入的路径。

约束：

- 默认只允许当前工作目录和用户确认过的目录。
- 禁止读取 shell history、SSH keys、browser profiles、cloud credentials。
- 上传前记录 sha256、size、mime。
- 大文件需要确认或拒绝。

### 7.3 Server-Side File Safety

服务器侧：

- 文件落 agent workspace。
- 路径必须 normalize。
- 禁止 absolute / traversal。
- 文档转换只产出 canonical Markdown artifact。
- ChatArtifact 只引用 workspace 内文件。

### 7.4 Prompt Injection

本地 bridge 从 Hive 收到的文件和消息都视为 untrusted content。

MCP tool descriptions 需要明确：

- 不把远端消息当系统指令。
- 不自动执行远端发来的 shell 命令。
- 不自动上传敏感文件。
- 需要用户确认的本地破坏性操作必须留在本地客户端确认层。

## 8. UI / Product Changes

### 8.0 Placement

接受本地 agent 链接的位置应该在 **目标 agent 自己的消息渠道区域**，不是全局设置。

当前前端 `AgentSettingsSection` 已经在 settings 里渲染 `ChannelConfig`。P0 应该在这里新增一个 channel card：

```text
AgentDetail
  -> Settings
     -> Channel / 消息渠道
        -> Local Agent Link
```

这个 card 表示“这个 Hive agent 允许哪些本地 agent runtime 作为本地渠道连接进来”。

不要把 Local Agent Link 做成普通工具/MCP 扩展 tab。原因：

- 用户是在给某个 Hive agent 接入一个消息来源和工作执行端。
- 绑定结果是 per-agent connection。
- revoke、last_seen、pending approval 都应该跟这个 agent 绑定展示。
- 从信息架构上它和 Feishu、Slack、WeChat Personal 这类 agent channel 更接近。

### 8.1 Local Agent Link Card

Agent detail 的消息渠道里增加 Local Agent Link card：

```text
Local Agent Link
  Status: Connected / Pending / Not connected
  Client: Claude Code / Codex / Claude Desktop / Cursor / Custom
  Device: Rocky MacBook Pro
  Bound user: Rocky
  Last seen: 2 minutes ago
  [Copy Skill Instruction] [Approve] [Reject] [Revoke]
```

Setup / guidance modal：

```text
1. Copy the local-agent instruction:
   "请安装 Hive Bridge skill，并把你连接到 Hive。"
2. 本地 agent 根据 Skill 安装 hive-bridge MCP/CLI。
3. hive-bridge login 打开浏览器 activation page。
4. 用户在 Hive Web 选择/确认 agent。
5. Web 显示 connected device 和 MCP verification status。
```

不要把这个第一版包装成“完整 A2A support”。文案应为：

```text
Local Bridge lets this agent communicate with approved local AI clients and local agents.
```

### 8.2 Acceptance Flow

本地 agent runtime 发起连接后，Hive Web 的接受动作应该落在这个 agent 的 Local Agent Link card 上。

推荐流程：

```text
1. 本地 agent runtime 执行 hive-bridge login。
2. CLI 创建 device-flow pairing，显示/打开 Hive activation URL。
3. 用户登录 Hive。
4. 如果 activation URL 没有 agent context，Web 要求用户选择一个 agent。
5. Web 跳到该 AgentDetail -> Settings -> Channel -> Local Agent Link。
6. Local Agent Link card 显示 pending request:
   - client_kind
   - device_name
   - device_fingerprint
   - requested_scopes
   - requested_at
7. 用户点击 Approve。
8. 后端把 pairing session 绑定到该 agent，并签发 scoped bridge token。
9. card 状态变成 Connected。
```

如果用户是先从某个 agent 的 Local Agent Link card 复制指令，则指令里可以带 agent hint：

```text
请安装 Hive Bridge skill，并连接到 Hive agent: <agent_name>。
Hive URL: <base_url>
Agent ID: <agent_id>
```

但后端仍不能信任本地传回来的 `agent_id`。最终绑定必须由 Web 登录用户在该 agent 的 Local Agent Link card 里 approve。

### 8.3 Frontend Identity Rules

前端不负责证明用户身份，只负责发起和展示服务器确认过的绑定。

规则：

1. Local Agent Link card 只在当前用户对 agent 有 `manage` 权限时显示 Setup / Approve / Reject / Revoke。
2. Setup modal 默认给用户一段发给本地 agent 的自然语言指令，而不是让用户手动安装桌面 app。
3. Activation page 接收 `user_code`，用户登录后选择目标 agent，或跳回 agent 的 Local Agent Link card。
4. 前端可以继续通过现有 request layer 发送 JWT 和 `X-Tenant-Id`，但后端必须以 `agent.tenant_id` 和 `check_agent_access` 为准。
5. Local Agent Link card / activation page 显示本地 bridge `claim` 上来的 `device_name`、`client_kind`、`device_fingerprint`、`requested_scopes`。
6. Approve 按钮在目标 agent 的 Local Agent Link card 中出现；P0 不允许管理员代替另一个普通用户批准本地设备。
7. Connections list 必须展示 `bound user`、`device_name`、`client_kind`、`last_seen_at`、`status`、`scopes`、`created_at`。
8. UI 只能在 `connection.user_id == currentUser.id` 时标记为 `Your device`；否则显示实际绑定用户。
9. Token 原文永不在 connections list 展示；只在 `exchange` 时返回给本地 bridge。
10. Revoke 后立即让 React Query / local state 失效重拉，并显示该 connection `revoked`。

前端 API surface：

```text
agentApi.startLocalBridgePairing(agentId)
agentApi.getLocalBridgePairing(agentId, pairingId)
agentApi.approveLocalBridgePairing(agentId, pairingId)
agentApi.activateLocalBridgePairing(userCode, agentId)
agentApi.listLocalBridgeConnections(agentId)
agentApi.revokeLocalBridgeConnection(agentId, connectionId)
```

所有新增 UI 文案必须同步 `frontend/src/i18n/en.json` 和 `frontend/src/i18n/zh.json`。

### 8.4 Local Client Binding Flow

本地 agent runtime 不直接拿 Hive 用户 JWT。它们通过 `hive-bridge` 共享同一个本地 bridge token。

本地 agent 主流程：

```text
1. 用户对本地 agent 说："帮我安装 Hive Bridge skill，并把你连接到 Hive。"
2. Skill 指导本地 agent 安装 `hive-bridge` CLI/MCP。
3. 本地 agent 执行 `hive-bridge login --base-url ...`。
4. CLI 创建 device-flow pairing，并打开 Hive activation URL。
5. 用户在 Hive Web 登录、选择/确认目标 agent、点击 approve。
6. 本地 CLI exchange 一次性 bridge token，并存入 Keychain 或 0600 config。
7. 本地 agent 执行其 runtime 对应的 MCP 配置，例如 `claude mcp add hive-local -- hive-bridge mcp` 或等价配置。
8. 本地 agent 调用 `hive_status` 验证连接。
9. 需要无人值守云端派活时，本地 agent 或用户启动 `hive-bridge run`。
```

`device_fingerprint` 不应该上传原始硬件序列号。建议是本地首次启动生成随机 device id，存本机安全存储后取 hash；它用于同一台本地 bridge 的稳定识别和审计，不用于强硬件认证。

用户不需要自己理解这些命令；这些命令由本地 agent 按 Skill 执行。排障时可以显式展示：

```bash
hive-bridge login --base-url https://...
claude mcp add hive-local -- hive-bridge mcp
```

如果 token 丢失、过期或被撤销，MCP tools 返回 `auth_required`，提示本地 agent 重新执行 `hive-bridge login`。MCP tool 不应要求任意本地 agent 输入 Hive JWT。

## 9. Tests

### 9.1 Backend Red-Green Tests

新增 / 扩展：

```text
backend/tests/api/test_local_bridge_pairing.py
backend/tests/api/test_gateway_bridge_auth.py
backend/tests/api/test_gateway_bridge_attachments.py
backend/tests/api/test_gateway_agent_transcript.py
backend/tests/api/test_upload_bridge_files.py
```

覆盖：

1. revoked token cannot poll.
2. token cannot access another agent.
3. user with only `use` access cannot create pairing.
4. pairing/start ignores body tenant/user and derives tenant/user server-side.
5. claim cannot change pairing tenant/agent/user.
6. exchange before approve returns 403.
7. approve rechecks current manage access.
8. bridge token ignores `X-Tenant-Id` and body `agent_id`.
9. path agent_id mismatch returns 403.
10. device-flow pairing works when initiated by local `hive-bridge login`.
11. device_code and user_code are hashed, short-lived, and one-time.
12. reused device_code cannot exchange a second token.
13. fallback pairing_code path requires explicit approve.
14. poll returns attachments.
15. report with attachment writes ChatMessage + ChatArtifact / transcript part.
16. send-message with client_message_id is idempotent.
17. upload rejects path traversal filename.
18. document upload returns markdown artifact path.
19. heartbeat updates last_seen.

### 9.2 Local Bridge Tests

```text
local_bridge/tests/test_client.py
local_bridge/tests/test_files.py
local_bridge/tests/test_mcp_server.py
local_bridge/tests/test_token_store.py
```

覆盖：

- token read/write permissions。
- poll/report happy path。
- upload file multipart request。
- MCP tool schema loads。
- network failure backoff。

### 9.3 Verification Commands

后端：

```bash
cd backend
source .venv/bin/activate
pytest tests/api/test_gateway_agent_transcript.py tests/api/test_chat_sessions_permissions.py tests/services/test_web_chat_runtime.py -q
```

新增测试后：

```bash
cd backend
source .venv/bin/activate
pytest tests/api/test_local_bridge_pairing.py tests/api/test_gateway_bridge_auth.py tests/api/test_gateway_bridge_attachments.py tests/api/test_upload_bridge_files.py -q
```

本地 bridge：

```bash
cd local_bridge
pytest -q
```

Manual smoke：

```bash
hive-bridge login --base-url http://localhost:8008
hive-bridge status
hive-bridge run
```

Gateway smoke：

```bash
curl -s "$HIVE_BASE/api/v1/gateway/poll" \
  -H "Authorization: Bearer $HIVE_BRIDGE_TOKEN"

curl -s -X POST "$HIVE_BASE/api/v1/gateway/send-message" \
  -H "Authorization: Bearer $HIVE_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target":"目标Agent名","content":"来自 Local Bridge 的测试消息","channel":"agent"}'
```

## 10. Development Cadence / Rollout Plan

开发节奏按 **vertical slice** 走，不按“后端先全写完、前端再全写完、本地包最后接”走。每个 slice 都必须有测试、可运行命令和一个真实验收动作。

### Slice 0: Contract freeze

目标：先冻结 P0 的协议和 UI 入口，避免后面返工。

产出：

- `local_agent_bridge_pairing_sessions` / `local_agent_bridge_connections` schema 草案。
- Bridge token auth contract：`Authorization: Bearer hb_<token>`。
- MCP tool contract：`hive_status` / `hive_poll_inbox` / `hive_send_message` / `hive_upload_file` / `hive_report_result`。
- Frontend placement contract：AgentDetail -> Settings -> Channel / 消息渠道 -> `Local Agent Link` card。
- Local package command contract：`hive-bridge login/mcp/run/status/logout/service`。

验收：

```bash
rg -n "local_agent_bridge|hive-bridge|Local Agent Link|hive_upload_file" docs/local-agent-bridge-first-pass-2026-06-22.md
```

### Slice 1: Pairing loop, no file/message yet

目标：先证明“这个本地 bridge 绑定到正确的 Hive tenant/user/agent”，这是所有安全边界的基础。

后端：

- Add migration for bridge pairing + connection tables.
- Add `POST /api/v1/local-bridge/pairing/init`。
- Add activation approve/reject/exchange APIs。
- Add `get_bridge_auth_context` dependency。
- Tests first: `tests/api/test_local_bridge_pairing.py`。

前端：

- Add `Local Agent Link` card skeleton。
- Add activation page and pending approval card。

本地包：

- Add `local_bridge/` package scaffold。
- Implement `hive-bridge login` and `hive-bridge status`。
- Store token in Keychain if available, otherwise `0600` config。

验收：

```bash
cd backend
source .venv/bin/activate
pytest tests/api/test_local_bridge_pairing.py -q

cd ../local_bridge
pytest -q

hive-bridge login --base-url http://localhost:8008
hive-bridge status
```

### Slice 2: Active communication through MCP

目标：满足“相互通信”的主动部分：本地 agent 能通过 MCP 和 Hive 发/收消息。

后端：

- Gateway poll/send-message/report accepts `Bearer hb_<token>`。
- Runtime identity only comes from connection row, never request body。
- Tests first: `tests/api/test_gateway_bridge_auth.py`。

本地包：

- Implement bridge HTTP client。
- Implement `hive-bridge mcp` stdio server。
- Expose MCP tools: `hive_status` / `hive_poll_inbox` / `hive_send_message` / `hive_report_result`。
- Add generic MCP stdio adapter docs in Hive Bridge Skill。

验收：

```bash
cd backend
source .venv/bin/activate
pytest tests/api/test_gateway_bridge_auth.py -q

cd ../local_bridge
pytest tests/test_mcp_server.py tests/test_client.py -q

hive-bridge mcp
```

Manual smoke:

```bash
curl -s "$HIVE_BASE/api/v1/gateway/poll" \
  -H "Authorization: Bearer $HIVE_BRIDGE_TOKEN"
```

### Slice 3: Local -> Hive single-file upload

目标：满足第一个硬指标：本地 agent 可以上传一个本地文件，Hive 保存原文件、生成 artifact，并写入 transcript。

后端：

- Add bridge upload endpoint。
- Reuse existing upload / document conversion path where possible。
- Add transcript attachment writing。
- Tests first: `tests/api/test_upload_bridge_files.py`。

本地包：

- Implement `hive_upload_file` MCP tool。
- Implement CLI smoke command if needed: `hive-bridge upload <path>`。
- Add file path validation, sha256, mime detection, retry behavior。

验收：

```bash
cd backend
source .venv/bin/activate
pytest tests/api/test_upload_bridge_files.py -q

cd ../local_bridge
pytest tests/test_files.py -q

hive-bridge upload ./sample.pdf
```

Manual smoke:

```text
Upload one PDF from local agent.
Confirm Hive transcript shows the file artifact.
Confirm canonical Markdown artifact exists when conversion is supported.
```

### Slice 4: Cloud -> local work_request

目标：满足第二个硬指标的无人值守部分：Hive cloud 可以投递 work_request，本地 runner 出站 poll，交给本地 adapter 干活并 report 回 Hive。

后端：

- Add `gateway_messages.metadata_json.kind = "work_request"` handling。
- Add completed/result transcript write path。
- Add idempotency around report result。

本地包：

- Implement `hive-bridge run` foreground poller。
- Implement command adapter contract。
- Implement first generic command adapter for smoke tests。
- Implement service command stubs with macOS user LaunchAgent first if feasible; otherwise foreground fallback must be explicit。

验收：

```bash
cd backend
source .venv/bin/activate
pytest tests/api/test_gateway_bridge_auth.py tests/api/test_gateway_bridge_attachments.py -q

cd ../local_bridge
pytest tests/test_poller.py tests/test_client.py -q

hive-bridge run --base-url http://localhost:8008
```

Manual smoke:

```text
Create a pending work_request in Hive.
Run hive-bridge run locally.
Confirm runner receives the request.
Confirm local adapter returns a result.
Confirm Hive transcript shows the completed result.
```

### Slice 5: Skill-driven install and UX polish

目标：把“小白用户不会自己装”的问题交给 Skill + Local Agent Link UI，而不是要求用户理解命令细节。

产出：

- Hive Bridge Skill package with install ladder。
- Copyable instruction in Local Agent Link card。
- Runtime-specific examples for generic MCP stdio, Claude Code, Codex。
- i18n en/zh。
- Revoke / last_seen / status display。

验收：

```bash
cd frontend
npm run build

cd ../backend
source .venv/bin/activate
pytest tests/api/test_local_bridge_pairing.py tests/api/test_gateway_bridge_auth.py tests/api/test_upload_bridge_files.py -q
```

Manual smoke:

```text
Open one Hive agent.
Go to Settings -> Channel / 消息渠道 -> Local Agent Link.
Copy the setup instruction.
Ask a local agent to follow the Skill.
Approve the pending link.
Verify status turns connected.
Revoke it and verify local poll fails.
```

### Slice 6: Dogfood release

目标：不上公开 registry，先做可重复安装的 alpha 包。

产出：

- `local_bridge/pyproject.toml` with `hive-bridge` entrypoint。
- Editable install works。
- Git install works。
- Optional GitHub Release artifact / internal download URL。
- Final P0 smoke checklist recorded。

验收：

```bash
python3 -m pip install -e ./local_bridge
hive-bridge status

python3 -m pip install --user "git+https://github.com/<org>/<repo>.git#subdirectory=local_bridge"
hive-bridge status
```

Production smoke:

```text
1. Pair one local bridge to a test agent.
2. Send Hive -> local message.
3. Send local -> Hive message.
4. Send Hive -> local work_request, confirm local result is reported back.
5. Upload one PDF and confirm transcript artifact opens.
6. Revoke token and confirm poll fails.
```

### Merge Rule

不要把 Slice 1-4 全部分散在一个大 PR 里。推荐提交顺序：

```text
PR 1: backend pairing + minimal frontend approval + local login/status
PR 2: bridge gateway auth + MCP active communication
PR 3: file upload + transcript artifact
PR 4: work_request runner + command adapter
PR 5: Skill install playbook + UX polish + alpha release artifact
```

每个 PR 都必须能独立跑对应测试，并保留到最终 P0 smoke checklist。

## 11. Future Work

这些不是第一版阻塞项：

- Full A2A JSON-RPC task endpoint.
- OAuth delegation grant for cross-app agent delegation.
- Push notifications from Hive to local bridge.
- Hive -> local file download.
- Multi-file upload and directory upload.
- Browser extension.
- Windows packaging.
- Linux .deb/.rpm packaging beyond the P0 tarball.
- Standalone signed installers that require no Node.js/Python/Homebrew.
- Cross-owner collaboration group enforcement for external local agents.
- Local sandbox execution controlled by Hive.
- Rich MCP app UI.

但第一版必须保留扩展空间：

- bridge connection table 不应 hard-code any local agent product, including Claude Code or Codex。
- attachment schema 应接近 message parts，而不是只支持一种文件。
- transcript event metadata 应保留 protocol/client fields。
- interoperability profile 不要声称完整 A2A achieved。

## 12. Acceptance Criteria

第一版完成时必须满足：

1. 用户可以把任意已适配的本地 agent runtime 连接到某个 Hive agent；Claude Code / Codex 只是首批 adapter。
2. Hive 可以向本地 bridge 发起一项工作，本地 bridge 可以收到、交给本地 agent 处理并回传结果。
3. 本地 bridge 可以主动向 Hive agent 发消息。
4. 本地 bridge 可以上传单个文件到 Hive，且文件出现在 ChatSession transcript 中。
5. PDF/DOCX/XLSX 上传后有 canonical Markdown artifact。
6. bridge token 可以撤销，撤销后 poll/report/send-message 都失败。
7. 所有 bridge 结果可从 Hive UI 的会话历史恢复，不需要用户去 workspace 里找。
8. 后端和 local bridge 测试覆盖认证、幂等、附件、路径安全、transcript。
9. 文档和 UI 明确这是 Local Bridge first pass，不宣称完整 A2A / ACP / MCP resource-server support。
10. 每个 connection 都能在后端解析为唯一 `tenant_id + agent_id + user_id + connection_id`，且 runtime 请求不能通过 header/body 改写身份。
11. 前端 Connections list 能显示实际绑定用户和设备，用户可以撤销自己的设备；管理员可以按权限撤销 tenant 内连接，但不能替普通用户静默 approve 新设备。
12. 普通用户主流程不要求自己理解终端、MCP 配置或密钥；连接路径是 user tells local agent -> Skill playbook installs CLI/MCP -> browser device-flow approval -> MCP status verified。
13. P0 必须提供 Hive Bridge Skill、Hive Bridge MCP、`hive-bridge` CLI 三件套；只提供其中一个都不算达标。
14. CLI command 和 pairing code 可以被本地 agent 按 Skill 执行或展示，但不要求用户手工理解和配置。
15. AgentDetail 的消息渠道区域必须新增 `Local Agent Link` card；本地 agent 链接的 pending request、Approve、Reject、Revoke、last_seen 都在这个 card 里完成。
16. Activation page 不能在全局静默 approve；如果没有 agent context，必须让用户选择目标 agent，并回到该 agent 的 Local Agent Link card 完成接受。
17. P0 不能要求用户本地起公网服务器、配置反向代理、暴露端口、使用 ngrok/Tailscale/Cloudflare Tunnel；所有本地到 Hive 的通信必须是本地主动出站 HTTPS/long-poll/WebSocket。
18. P0 必须区分 `hive-bridge mcp` 和 `hive-bridge run/service`：前者是本地 agent 启动的 stdio 子进程，后者是可选常驻出站 poller；云端无人值守派活只能依赖 runner/service 或支持 headless command adapter 的本地 agent。
19. P0 只要求选择一条最简单可用的本地安装路径把链路跑通并上线；standalone binary / signed installer / no-Node-no-Python 的小白安装体验进入 P1。
