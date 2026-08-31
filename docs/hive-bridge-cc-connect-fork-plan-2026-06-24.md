# Hive Bridge cc-connect Fork Plan

状态：当前方案文档，先于实现
日期：2026-06-24
范围：Hive Local Agent 的本地 runner、IM 化对话、会话持久化、文件传输、流式事件、A2A 调用和用户安装路径

## 0. 结论

Local Agent 这条线不要继续从零自研完整本地 Agent runtime。正确路线是：

```text
Fork cc-connect -> Hive Bridge
保留 cc-connect core / session manager / agent adapters
深度定制为 Hive 专属本地 runner
Hive 主仓库继续做云端 IM / 权限 / A2A / Workspace / 前端体验
```

最终产品语义：

```text
Hive Cloud 是用户和云端 Agent 看到的 IM 与控制面。
Hive Bridge 是用户本机常驻 runner。
cc-connect core 是 Hive Bridge 内部的本地 Agent runtime 底座。
Codex / Claude Code / ACP / Gemini / Cursor 等本地 Agent 都通过 cc-connect adapter 接入。
```

这不是把原版 cc-connect 原样暴露给用户，也不是让用户自己配置 cc-connect。对用户来说，只有一个东西：

```text
Hive Bridge
```

cc-connect 是 Hive Bridge 内部的成熟 runtime 基座。

## 1. 为什么选择 fork，而不是直接用原版

直接用原版 cc-connect 可以很快做技术验证，但产品化会出现几个硬问题：

1. 原版 cc-connect 面向多个 IM 平台，用户需要理解项目、平台、config.toml、bridge token、web admin 等概念。
2. Hive 需要 tenant、owner、Local Agent 身份、A2A 调用、审计、Workspace 和云端 ChatSession，原版没有这些产品语义。
3. Hive 要把本地 Agent 显示成自己的 Agent，带本地标签、Chat 页面和 Workspace 页面，而不是一个外部 bot。
4. 小白用户的目标路径是“让 Codex/CC 帮我安装 Hive Bridge skill 并连接 Hive”，不是“手动安装并配置 cc-connect”。
5. 后续要做 npm/skill 分发、自动登录、服务化常驻、故障诊断和权限撤销，这些都应该用 Hive 的产品语言包装。

所以建议：

```text
不要裸用原版 cc-connect
不要把 cc-connect 大段代码塞进 hiveclaw-main
单独 fork 成 Hive Bridge runner 仓库
```

## 2. cc-connect 给我们的成熟能力

cc-connect 已经解决的核心问题，正好是 Hive Local Agent 现在缺的部分：

1. `core.Engine`
   - 负责把 IM message 路由到本地 Agent。
   - 管理 busy lock、队列、事件循环、最终回复和异常。

2. `core.SessionManager`
   - 保存 IM session 和本地 Agent session 的映射。
   - 保存 active session、session list、history、session name、agent session id。
   - 支持重启后恢复。

3. `Agent / AgentSession` 抽象
   - `StartSession`
   - `Send`
   - `Events`
   - `CurrentSessionID`
   - `Alive`
   - `Close`

4. 本地 Agent adapters
   - Claude Code
   - Codex
   - ACP
   - Gemini / Cursor / OpenCode / others

5. IM 级能力
   - streaming text
   - preview / update message
   - file / image
   - slash command
   - new / switch / list / delete sessions
   - cron / timer / relay 等可按需隐藏或后续开放

这些能力如果 Hive 自己重写，风险主要不在代码量，而在边界细节：resume、session 串线、concurrent message、runner 重启、外部 CLI session 过滤、流式 finalize、文件 staging、长任务中断等。cc-connect 已经踩过这批坑。

## 3. 总体架构

目标架构：

```text
Hive Web
  |
  | HTTPS / WSS
  v
Hive Backend
  - auth / tenant / owner
  - Local Agent identity
  - ChatSession / transcript
  - Workspace / artifacts
  - A2A local delegation
  - audit / revoke / presence
  |
  | outbound-only WSS from user's machine
  v
Hive Bridge local runner
  - hive login / token
  - HivePlatform
  - cc-connect Engine
  - cc-connect SessionManager
  - service daemon
  |
  | local process / stdio / CLI / ACP
  v
Local Agent
  - Codex
  - Claude Code
  - ACP-compatible agent
  - other supported local agents
```

本机不暴露公网服务。所有云端通信由 Hive Bridge 主动出站连接 Hive Backend。

## 4. 仓库拆分

### 4.1 Hive 主仓库

`hiveclaw-main` 保留云端能力：

```text
backend/
  local bridge auth
  local agent channel APIs
  chat session / transcript
  workspace / artifact
  A2A local delegation
  audit / revoke / tenant guards

frontend/
  Local Agent as real Agent with Local badge
  Local Agent Chat page
  Local Agent Workspace page
  login activation page
  online/offline state
```

Hive 主仓库不应该承载 cc-connect runtime 源码。

### 4.2 Hive Bridge 新仓库

建议新仓库：

```text
<legacy-owner>/hive-bridge
```

或者组织名确定后：

```text
<legacy-publisher>/hive-bridge
```

目录建议：

```text
hive-bridge/
  cmd/hive-bridge/
    main.go

  core/
    # fork from cc-connect core, keep upstream shape as much as possible

  agent/
    # fork from cc-connect agent adapters
    claudecode/
    codex/
    acp/
    gemini/
    ...

  platform/
    hive/
      platform.go
      client.go
      websocket.go
      protocol.go
      attachments.go
      presence.go

  hive/
    auth.go
    config.go
    keychain.go
    device_flow.go
    api_client.go
    workspace.go

  service/
    launchd.go
    systemd.go
    windows.go

  package/
    npm/
      package.json
      postinstall.js
      bin/

  skills/
    hive-bridge/
      SKILL.md

  docs/
    install.md
    troubleshooting.md
```

## 5. fork 的定制原则

### 5.1 保留 cc-connect core 的干净边界

不要把 Hive tenant/auth 逻辑写进 cc-connect `core`。

正确边界：

```text
core = runtime engine
platform/hive = Hive IM adapter
hive/* = Hive auth/client/config
cmd/hive-bridge = product CLI wrapper
```

### 5.2 HivePlatform 是内置默认平台

原版 cc-connect 通过 Feishu/Slack/Telegram 等 platform 接收消息。Hive fork 里新增：

```text
platform/hive
```

它实现 cc-connect 的 `Platform` 接口：

```text
Name() = "hive"
Start(handler)
Reply(ctx, replyCtx, content)
Send(ctx, replyCtx, content)
Stop()
```

它不是连接飞书，也不是本地 web UI。它连接 Hive Backend：

```text
wss://<hive-backend>/api/local-bridge/channel/ws
```

收到 Hive message 后转成 cc-connect `core.Message`，交给 Engine。

Engine 产生 reply/event 后，HivePlatform 再回传 Hive Backend。

### 5.3 Hive 是云端 IM 记录真相源

双真相源必须拆清：

```text
Hive Backend = 云端 IM transcript 真相源
Hive Bridge / cc-connect SessionManager = 本地 Agent resume 真相源
```

含义：

1. 用户在 Hive Web 看到的聊天记录，以 Hive `ChatSession` / Local Agent channel timeline 为准。
2. 本地 Agent 的真实 session id，以 Hive Bridge 内部 SessionManager 为准。
3. 两边必须保存映射：

```text
hive_session_id
hive_message_id
local_cc_session_id
local_agent_session_id
adapter_kind
```

不要让前端只依赖本地 runner 的 history；也不要让本地 Agent resume 只依赖 Hive DB。

## 6. 关键数据映射

### 6.1 Hive -> Hive Bridge message

Hive Backend 下发：

```json
{
  "type": "message",
  "session_id": "hive-local-session-id",
  "message_id": "hive-message-id",
  "target_local_agent_id": "hive-agent-id",
  "caller_agent_id": "optional-cloud-agent-id",
  "owner_user_id": "user-id",
  "content": "用户消息或云端 agent 指派任务",
  "attachments": [],
  "metadata": {
    "source": "web|a2a|automation",
    "requested_workspace_path": "workspace"
  }
}
```

HivePlatform 转成 cc-connect message：

```text
SessionKey = hive:<owner_user_id>:<target_local_agent_id>:<hive_session_id>
MessageID = hive_message_id
UserID = owner_user_id or caller_agent_id
UserName = Hive / caller agent name
Content = content
Files/Images = staged attachments
ReplyCtx = hive session/message envelope
```

### 6.2 Hive Bridge -> Hive event

本地 runner 回传：

```json
{
  "type": "event",
  "session_id": "hive-local-session-id",
  "message_id": "hive-message-id",
  "event_type": "text_delta",
  "payload": {
    "delta": "partial text",
    "full_text": "current accumulated text"
  },
  "local": {
    "adapter_kind": "codex",
    "local_agent_session_id": "codex-thread-id"
  }
}
```

最终结果：

```json
{
  "type": "result",
  "session_id": "hive-local-session-id",
  "message_id": "hive-message-id",
  "status": "completed",
  "output": "最终回复",
  "artifacts": [],
  "metadata": {
    "adapter_kind": "codex",
    "local_agent_session_id": "codex-thread-id",
    "cc_session_id": "s1"
  }
}
```

## 7. Hive Backend 需要配合的能力

现有 Local Agent Channel 已经有基础表和 API，但 fork cc-connect 后需要统一到更完整的 IM contract。

必须补齐：

1. 稳定 event cursor
   - `LocalAgentChannelEvent` 增加 per-session monotonic `seq`。
   - browser 和 runner 都用 `after_seq` replay。
   - 不再依赖 UUID 或 created_at 做可靠 cursor。

2. session list / new / switch / delete
   - Local Agent 页面要像普通 chat 一样支持多会话。
   - Hive session 需要和本地 cc-connect session 映射。

3. 字段语义拆分
   - `target_local_agent_id`
   - `caller_agent_id`
   - `owner_user_id`
   - 避免继续复用 `source_agent_id` 表达多个含义。

4. message lease / ack
   - pending -> claimed -> delivered -> running -> completed / failed。
   - runner 断线时未完成 message 可以重投或标记 stale。

5. ChatSession/T0 写入统一
   - Local Agent transcript 不能只存在 local event 表。
   - 用户可见 transcript 应进入统一 session/timeline truth surface。

6. attachments 双向处理
   - Hive -> local：Hive file/artifact 转成本地 staged file。
   - local -> Hive：本地生成文件上传为 Hive artifact，并在 Chat/Workspace 可见。

7. owner-only 和 A2A 权限
   - owner 可以直接和自己的 local agent 聊天。
   - owner 的云端 agent 可以通过受治理 A2A 调用该 local agent。
   - 非 owner 不可直接调用。

## 8. Hive Frontend 目标形态

Local Agent 不再是一个普通设置卡片，也不只是 `/local-agents` 控制台。

它应该像普通 agent：

```text
Digital Employees / Agent list
  - 普通 agent
  - 公共 agent
  - 本地 agent（Local badge）

Local Agent detail
  - Chat
  - Workspace
```

只保留两个页面：

1. Chat
   - session list / new chat / switch chat
   - user bubble
   - assistant bubble
   - streaming output
   - tool/progress events
   - file attachments
   - status: online / offline / reconnecting

2. Workspace
   - 用户级 shared local workspace
   - 上传、下载、预览
   - runner 生成 artifact 可见

不要露出 cc-connect、config.toml、bridge token 等底层概念。

## 9. CLI / Skill / npm 分发

第一版用户路径保持：

```text
用户在 Codex / Claude Code / 其他本地 Agent 中说：
"帮我安装 Hive Bridge skill，并连接到 Hive。"
```

Skill 安装：

```bash
npx skills add https://github.com/<legacy-owner>/hive-bridge-skill --skill hive-bridge
```

Skill 指导本地 Agent 执行：

```bash
npm install -g @<legacy-publisher>/hive-bridge
hive-bridge login
hive-bridge run
```

常驻：

```bash
hive-bridge service install
hive-bridge service start
hive-bridge service status
```

CLI 命令：

```bash
hive-bridge login
hive-bridge logout
hive-bridge status
hive-bridge run
hive-bridge doctor
hive-bridge service install
hive-bridge service start
hive-bridge service stop
hive-bridge service status
```

第一版可以 npm 分发。后续再做 brew / binary / signed installer。

## 10. 认证和登录状态

登录状态应该长期保持，类似 IM 登录态。

认证流程：

```text
hive-bridge login
  -> 创建 pairing/device flow
  -> 打开 Hive Web activation URL
  -> 用户在 Hive 登录
  -> Hive 自动 approve 当前 pairing
  -> CLI 拿到 long-lived bridge token
  -> token 存 Keychain 或 0600 config
  -> Hive Bridge run 使用 token 建立 WSS
```

登录态和在线态分开：

```text
登录态 = token 有效，未撤销
在线态 = runner 当前有活跃 WebSocket 或最近 presence 在有效窗口内
```

用户关机、休眠、断网时：

```text
登录态仍然存在
在线态变为 offline / stale
Hive 可以保存待投递消息，但不能假装已经执行
```

Web 端解绑：

```text
revoke connection
token 立即失效
runner 下次请求 fail-closed
Local Agent 显示 disconnected/revoked
```

## 11. 在线状态

不需要把 heartbeat 当业务逻辑，但需要 transport presence。

推荐：

```text
runner WSS connected -> online
runner WSS disconnected -> stale
超过 presence ttl -> offline
runner reconnect + ready -> online
```

ping/pong 只用于保持连接和检测死连接，不承担认证。

## 12. 文件传输

目标不是简单上传文件，而是 IM 附件语义。

### 12.1 Hive -> Local

用户或云端 agent 在 Hive Chat 里附加文件：

```text
Hive artifact/file
  -> Hive Bridge 下载或预签名拉取
  -> staging 到 local workspace
  -> 注入本地 Agent prompt
```

本地 Agent 看到的是本地文件路径，而不是 Hive URL。

### 12.2 Local -> Hive

本地 Agent 生成文件：

```text
local file
  -> hive-bridge upload
  -> Hive artifact
  -> Chat message attachment
  -> Workspace 可见
```

第一版必须支持：

```text
text
markdown
images
generic files
```

音频/视频可以继承 cc-connect 能力，但不作为第一验收硬指标。

## 13. A2A 调用语义

Local Agent 是 Hive 里的一个真实 agent，只是有 `local` 标签。

云端 agent 调用本地 agent 的规则：

```text
只有 owner 的云端 agent 可以调用 owner 的 local agent。
调用不限 owner 的某一个特定云端 agent。
只要调用链 principal 是 owner，就可以路由到该 owner 的 local agent。
```

调用路径：

```text
Cloud Agent
  -> delegate_to_agent(target = local_agent)
  -> Hive backend permission check
  -> Local Agent Channel session
  -> Hive Bridge WSS
  -> cc-connect Engine
  -> local Agent adapter
  -> result streamed back
```

A2A 结果要进入调用方可见 transcript，同时保留 local agent 自己的 Chat timeline。

## 14. 一次性验收标准

这条线的验收不能只看“消息入队”。必须按 IM 体验验收。

### 14.1 认证

- `hive-bridge login` 能打开 Hive Web 并自动完成绑定。
- 用户不需要复制 pairing code。
- token 持久化，重启后仍能登录。
- Web 端 revoke 后 runner 请求失败。

### 14.2 在线状态

- runner 启动后 Local Agent 显示 online。
- runner 停止后 Local Agent 显示 stale/offline。
- 电脑重启后 service 自动恢复连接。

### 14.3 Chat

- Web 可以直接给 Local Agent 发消息。
- 本地 Agent 能收到并执行。
- Web 能看到 streaming 输出。
- 最终结果进入持久 Chat history。
- 浏览器刷新后历史仍在。

### 14.4 Session

- 支持 new chat。
- 支持 session list。
- 支持 switch session。
- runner 重启后能 resume 对应 local agent session。
- 不同 Hive local sessions 不串上下文。

### 14.5 文件

- Hive Web 上传文件后，本地 Agent 能拿到本地 staged path。
- 本地 Agent 生成文件后，Hive Web 能看到 attachment/artifact。
- Workspace 页面可下载。

### 14.6 A2A

- owner 的云端 agent 可以派活给 owner 的 local agent。
- 非 owner 调用失败。
- 派活过程可见 queued/running/completed/failed。
- result 同步回调用方会话。

### 14.7 回归测试

必须补和 cc-connect 同类的回归测试：

- `/new` 后旧 session 不丢。
- `/switch` 后 session history 不丢。
- runner restart 后 session list 不丢。
- local agent session id 持久化。
- busy session 下第二条消息进入队列，不打断当前 turn。
- WebSocket reconnect 后按 `after_seq` replay missed events。
- result 写入 ChatSession 和 local timeline。

## 15. 不做什么

第一版不要做这些：

1. 不让用户手动配置 cc-connect。
2. 不暴露 cc-connect Web UI 作为 Hive 产品入口。
3. 不要求用户本地起公网服务。
4. 不把 Hive Backend 写成 cc-connect 的一个普通外部 adapter。
5. 不把 cc-connect core 直接搬进 hiveclaw-main。
6. 不把 MCP 放回第一版主链。
7. 不把本地 Agent 绑定到某个云端 agent 设置卡片里。

## 16. 实施顺序

这是构建顺序，不是产品 MVP。最终对用户交付时要闭环完整 IM 能力。

### 16.1 Fork 和瘦身

1. Fork cc-connect 到 Hive Bridge 仓库。
2. 保留 core、agent adapters、session manager、engine。
3. 隐藏或移除非 Hive 必需的 IM platform 默认入口。
4. 保留必要 build tags，方便后续按需恢复。

### 16.2 HivePlatform

1. 新增 `platform/hive`。
2. 实现 Hive WSS client。
3. 实现 message -> core.Message。
4. 实现 core reply/event -> Hive event/result。
5. 支持 attachment staging。

### 16.3 CLI 产品化

1. `hive-bridge login`
2. `hive-bridge run`
3. `hive-bridge status`
4. `hive-bridge doctor`
5. `hive-bridge service ...`

### 16.4 Hive Backend contract

1. session seq cursor。
2. session list/new/switch/delete。
3. local session mapping。
4. message lease/ack。
5. transcript writer 统一。

### 16.5 Frontend IM 化

1. Local Agent detail 复用普通 Agent Chat 体验。
2. Local badge。
3. Chat + Workspace 两个 tab。
4. session drawer。
5. streaming bubble。
6. file attachment UI。

### 16.6 发布

1. npm package：`@<legacy-publisher>/hive-bridge`。
2. skill repo：`<legacy-owner>/hive-bridge-skill`。
3. skill 内只写用户/agent 操作手册，不塞复杂实现。
4. npm 包内包含或下载平台对应 binary。

## 17. 上游同步策略

fork 后不能随意大改 core，否则后面无法吃上游修复。

建议：

```text
core/ 尽量小改
agent/ 尽量小改
Hive 定制放在 platform/hive、hive/、cmd/hive-bridge、service/
每月或重要 release 后做一次 upstream merge
```

如果必须改 core，要写清楚：

```text
why upstream cannot support this
which Hive product invariant requires this
test coverage
upstream merge risk
```

## 18. 许可和发布注意

当前本地 README 标注 cc-connect 是 MIT License。正式 fork 和发布前必须重新确认上游仓库的 LICENSE 文件和版权声明，并保留必要 attribution。

发布前检查：

```text
LICENSE
NOTICE if any
README attribution
npm package license field
binary build provenance
dependency licenses
```

## 19. 当前决策

采用：

```text
cc-connect fork as Hive Bridge local runner
HivePlatform as built-in platform
Hive Cloud as IM/control-plane truth source
cc-connect SessionManager as local Agent resume truth source
npm + skill as first user distribution path
```

废弃当前“完全自研本地 Agent runtime”作为主线。现有 Hive Local Agent Channel 代码不删除，它作为云端 IM/control-plane 基座继续保留，并按上述 contract 升级。

