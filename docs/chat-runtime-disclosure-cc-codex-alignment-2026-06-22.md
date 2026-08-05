# Chat Runtime Disclosure 对齐 CC/Codex 设计文档

> 状态：2026-07-01 当前设计入口。2026-06-29/30 runtime feedback、A2A completion wake、artifact delivery、transcript replay、Disclosure UI V2 的代码级闭合仍有效。2026-07-01 Session Interaction Parity 的工作包 1-5 已代码级落地：draft-first 新建、composer 与 WebSocket 解耦、active-run 404/null idle-safe、create-and-run 原子入口、tool result 一眼可读摘要均已实装并有 focused tests / build evidence。第 6 步 Product E2E 由人工验收，不在本轮自动完成范围。

## Implementation Status — 2026-06-22

- P0 文档入口：已完成，本文档挂入 `docs/README.md`。
- P1 前端 disclosure layer：已完成。普通 `tool_call` 不再默认隐藏；连续的 `thinking/tool_call/session_compact/permission` 会按 assistant turn 聚合成一个 `RunDisclosureBlock`；AskQuestion 保持单面板多问题交互。
- P2 后端 durable tool step contract：已完成。`running` tool call 也写 transcript；`done/failed` 写 completion event；事件带 `tool_call_id`、`step_id`、`duration_ms`、`visibility` 并回填到 WebSocket payload。
- P4 环境信息侧栏：跳过，不作为当前目标。

## Verification Evidence — 2026-07-01

- `cd backend && source .venv/bin/activate && ruff check app/api/chat_sessions.py tests/api/test_chat_session_runs.py` -> `All checks passed!`
- `cd backend && source .venv/bin/activate && pytest tests/api/test_chat_session_runs.py -q` -> `21 passed, 4 warnings`
- `cd frontend && npm test -- --run src/api/domains/chat.test.ts src/pages/agent-detail/chatRuntime.test.ts src/pages/agent-detail/chatDisclosureReducer.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/RunDisclosureBlock.test.tsx` -> `5 passed, 146 passed`
- `cd frontend && npm run build` -> `tsc && vite build`, `6968 modules transformed`, build exit 0

## Re-audit — 2026-06-29

本轮重新对照 CC / FreeCode 与 Codex 后，结论是：旧 P1-P3 不是“完全失效”，但还没有达到 CC/Codex 的 session loop 闭环标准。当前代码已经具备低层事件面，却没有形成稳定的产品级 disclosure contract。

## Session Interaction Parity Contract — 2026-07-01

### 总结论

当前 Hive 不是“完全没有对齐”，也不能说“除了 `+` 新建 session 以外都已对齐”。准确状态是：

| 面 | 当前判断 | 说明 |
| --- | --- | --- |
| runtime event substrate | 基本对齐 | 后端和前端已有 `runtime_action_started/progress/completed/blocked/failed`、tool call、thinking、artifact、A2A completion 等可回放事件面。 |
| disclosure / replay | 基本对齐，但仍需产品级全周期验收 | `RunDisclosureBlock`、transcript replay、reasoning restore 已有代码级闭合；还需要用完整真实 session 验证 live / refresh / reconnect 一致。 |
| A2A / background wake | 基本对齐 | A2A delegation、artifact delivery、completion wake 已有代码级闭合；还需要纳入同一套 session interaction E2E。 |
| composer / input lifecycle | 代码级对齐，待 Product E2E | composer 可用性改由 session contract / `canUseComposer` 决定；WebSocket 只影响 live update 状态提示，不再锁死 textarea / send / menu。 |
| clean session creation | 代码级对齐，待 Product E2E | 点击 `+` 只创建本地 draft human session；未输入不持久化，首条消息通过 create-and-run 原子入口创建 `ChatSession + RuntimeTask`。 |
| active-run null / 404 | 代码级对齐，待 Product E2E | 前端 API 将 active-run 404 归一为 `null`；draft session 不查 active run，null 只代表 idle。 |
| read-only boundary | 代码级对齐，待 Product E2E | A2A / agent participant session 仍只读；draft/current-user human chat 明确可写，pending lookup 不再污染新建会话。 |

因此，本节的目标是把 CC 的 session 朴素原则和 Codex 的工程化表达合并成 Hive 的最终交互契约。

### CC 基座原则

CC / FreeCode 的核心不是“最后给一个答案”，而是 session 中持续展示 agent 的可见推进路径：

```text
用户输入
  -> agent 说明下一步要做什么
  -> 调用工具
  -> 工具结果折叠记录
  -> agent 根据结果反思/归纳下一步
  -> 再调用工具
  -> 再反思
  -> 最终回答
```

这套循环中，主界面应该展示的是“可见思考路径表达”，不是 raw chain-of-thought，也不是 raw tool log。工具调用细节属于该 step 的证据，默认折叠，用户可展开。

CC / FreeCode 证据面：

- `free-code-main/src/remote/sdkMessageAdapter.ts` 会把 `tool_progress` 转成可见 system message，例如 `Tool <name> running for <seconds>s...`。
- `free-code-main/src/components/Messages.tsx` 和 REPL 层同时维护 `streamingThinking` 与 `streamingToolUses`，说明 thinking/process 与 tool progress 是 session 过程的一等展示对象。
- `claude-code-org/src/tools/AgentTool/AgentTool.tsx` 对 sub-agent 的 bash/powershell progress 也向 SDK 转发 `tool_progress`，说明 child / subagent 过程同样不能静默。

Hive 对齐 CC 时必须保留这个朴素原则：只要 agent 在一个 session 中 move forward，就要留下用户可见、可回放、可折叠的过程记录。

### Codex 增强原则

Codex 更值得吸收的是工程化组织方式：

- turn 开始时进入明确 `Working` 状态，而不是让用户猜是否还在跑。
- reasoning summary 在结束后固化成 history cell，刷新后仍能看到同样的过程摘要。
- tool call / subagent / multi-agent / collab activity 都有独立 history cell 或 status cell。
- 文件链接和 artifact 渲染保存 session cwd / context，避免刷新或跨工作区后路径语义漂移。

Hive 不需要照抄 Codex Rust 类型，但要吸收这些工程优化：

1. **状态分层**：input enabled、runtime running、transport connected 是三件事。
2. **过程固化**：live 看到的 step，刷新 replay 后必须仍能看到。
3. **协作可见**：A2A、subagent、workflow、background task 都必须有 start / progress / completion feedback。
4. **artifact 可交付**：最终输出要包括总结、呈现、文件或 artifact card；跨 workspace 文件不机械复制，但必须可预览、可下载、可追溯 owner/source/download agent。

### Hive 最终用户可见目标

Hive session 的目标形态如下：

```text
用户消息

正在处理 18s
  我先确认当前文档和代码事实，避免按旧结论修改。
    > 工具调用折叠：rg, read_file
  我发现 composer 仍被 wsConnected 锁死，这不是 CC/Codex 的交互模型。
    > 工具调用折叠：read AgentChatSection.tsx, read AgentDetail.tsx
  我会把 session lifecycle 拆成 draft、durable session、transport 三层。
    > 工具调用折叠：update docs, run checks

最终回答
  1. 总结
  2. 结论 / 呈现
  3. 文件 / artifact 入口
```

不变量：

- 主界面默认展示 step 的思考路径摘要，不默认展示 raw CoT。
- 工具调用默认折叠，但不能消失。
- `Thinking` 不能成为空白占位；它必须归并为当前 turn 的 process step。
- completed turn 可以折叠为摘要行，但展开后必须看到完整“思考 -> 工具 -> 思考 -> 工具 -> 最终回答”。
- live、refresh replay、disconnect/reconnect 三种路径看到的 timeline 必须一致。
- final answer 不得被 tool result、workspace file、notification、task wrapper 替代。

### 全周期 Session 复核矩阵

后续实现和验收必须覆盖完整 lifecycle，而不是只修单个按钮：

| 生命周期环节 | CC/Codex 对齐目标 | Hive 当前判断 | 必须落地的检查点 |
| --- | --- | --- | --- |
| 新建会话 | draft-first；未输入不持久化 | 代码级对齐，待 Product E2E | `+` 只建本地 draft row；刷新后消失；不产生后端空 session。 |
| 第一条输入 | 输入立即可用；发送原子创建 turn/run | 代码级对齐，待 Product E2E | 首次发送通过 create-and-run 原子入口创建 `ChatSession + RuntimeTask`。 |
| 输入可用性 | 由 session contract 决定 | 代码级对齐，待 Product E2E | composer 禁用条件已收敛到 `canUseComposer`，不再使用 `!wsConnected` 作为硬禁用。 |
| transport | 只负责 live updates | 代码级对齐，待 Product E2E | WebSocket 断开只显示 reconnecting 状态，不阻止输入和 HTTP run 启动。 |
| active run | none 是 idle | 代码级对齐，待 Product E2E | `404/null` 统一解释为无运行中任务，不影响 composer。 |
| running turn | 可见 Working / processing 状态 | 基本对齐 | run header 显示 running / blocked / done / failed，duration 可见。 |
| reasoning step | 可见过程摘要 | 基本对齐 | live 与 replay 都要把 reasoning/thinking 还原进 timeline step。 |
| tool call | 折叠但可展开 | 基本对齐 | running/done/failed 用同一 `tool_call_id` 合并，raw result 不占主叙事。 |
| AskQuestion / permission | 阻塞 step | 基本对齐 | 问题/权限在同一 turn 内显示，回答后恢复 run。 |
| A2A / subagent / workflow | start/progress/completion 都反馈 | 基本对齐 | 所有后台执行发出后立即有 root session feedback，完成后 wake 主 Agent。 |
| A2A read-only | child / peer session 只读 | 基本对齐 | A2A session 两侧可见、带 A2A 标签、composer 不出现。 |
| cross-workspace artifact | 不复制但可交付 | 基本对齐 | artifact 带 owner/source/download/delivery agent，父会话可预览与下载。 |
| final answer | 总结 + 呈现 + 文件 | 需产品验收 | completion wake 后主 Agent 必须给最终交付，不让用户手动 check。 |
| refresh replay | 与 live 等价 | 需产品验收 | 刷新后 timeline、thinking、tool folded details、artifact card 全部还原。 |
| reconnect | 不改变会话语义 | 未完全对齐 | 断线期间可发送；重连后按 transcript sequence 补齐事件。 |
| resume/fork/checkpoint/compact | 都是 session lifecycle event | 需全量复核 | 每条路径都必须进入同一 disclosure/replay contract。 |

### 当前源码证据

本轮 2026-07-01 复核与实装后的源码事实：

- `frontend/src/pages/AgentDetail.tsx`：`createNewSession()` 已改为本地 draft-first，不再点击 `+` 时调用后端创建空 session；`startRunForActiveSession()` 对 draft 首条消息调用 `chatApi.createSessionRun(...)`，成功后用真实 session 替换 draft。
- `frontend/src/pages/agent-detail/AgentChatSection.tsx`：composer placeholder、textarea、send、upload、slash menu、plan/goal/schedule 控制已从 `wsConnected` 硬禁用切到 `canUseComposer`；WebSocket 断开只显示 live update reconnecting 状态。
- `frontend/src/api/domains/chat.ts`：`getActiveSessionRun()` 将 404 归一为 `null`；新增 `createSessionRun()`，路径为 `POST /agents/{agent_id}/sessions/runs`。
- `backend/app/api/chat_sessions.py`：新增 `CreateSessionRunIn` / `CreateSessionRunOut` 和 `create_session_run()`，同一请求内创建 human web session 并调用 `start_web_chat_run()` 启动首轮 durable run。
- `frontend/src/pages/agent-detail/chatDisclosureReducer.ts`：tool completion 默认折叠 raw payload，但对 file write / create / update 类结果给出可读 completion summary；command raw output 不提升到主叙事。
- `frontend/src/pages/agent-detail/chatRuntime.ts` 仍识别 `runtime_action_started/progress/completed/blocked/failed`，`RunDisclosureBlock` 仍负责 reasoning / A2A / workflow / subagent step 的聚合展示。

因此，当前主要缺口已经从“代码级 interaction contract 未分层”收敛为“Product E2E 验证 live / refresh / reconnect / A2A / artifact 是否符合用户可见目标”。

### 底层链路与 TUI 表达的分界

Plan Mode handoff 修复后的复核结论需要吸收到本文档：当前问题不能被简单描述为“Session 底层没有思考 -> 工具 -> 结果 -> 最终答案链路”。更准确的分界如下：

1. **底层 session chain 已存在**
   - 后端 `web_chat_runtime` 已在 tool running 阶段写 `tool_call`，在 done / failed 阶段写 `tool_result`。
   - `invoke_agent(...)` 已接入 `on_chunk`、`on_tool_call`、`on_thinking`、`on_event`。
   - 前端 `chatRuntime` 已能把 `thinking` 归入 assistant thinking 字段，把 `tool_call/tool_result` 归一成 `role="tool_call"`，并把 `assistant_message` 作为最终答案回放。
   - `chatDisclosureReducer` 和 `timelineModel` 已能把 tool_call、assistant thinking、session-native runtime event 合成 disclosure step / run cell。

2. **视觉上“不像 Codex”的主要原因在 TUI 表达层**
   - `tool_result` 当前主要并入同一个 tool step 的 details / rawResult，默认折叠；结构存在，但用户一眼看到的不是“每一步结果摘要”。
   - Plan Mode、AskQuestion、artifact、workflow 等交互型工具会额外渲染 inline card；这类卡片如果没有被纳入同一 run narrative，会让界面看起来像“大卡片 + 文本”，而不是统一的 step loop。
   - provider 不一定返回 reasoning / thinking；如果模型没有提供可展示 reasoning，前端不能伪造 private CoT，只能展示 runtime narrative：例如“正在读取文件”“搜索完成”“已委派给 X”“工具返回 3 条结果”。
   - completed run 的折叠密度和 step 摘要仍需要产品级打磨：目标不是展开 raw tool logs，而是让用户默认看到每一步推进的摘要。

3. **因此后续落地重心**
   - 不是重写后端主链。
   - 不是把所有 tool result 全部展开。
   - 不是伪造 provider 没有返回的 reasoning。
   - 而是把已有底层事件投影成更稳定的 CC/Codex 风格 TUI：可见 process summary、一致的 run block、折叠工具细节、清晰的 final answer / artifact 交付。

### 必须收口的设计边界

1. **`wsConnected` 不再控制 composer**
   - `wsConnected` 只能影响 live update badge。
   - composer 是否可输入由 `canSubmitMessage` 决定。
   - `canSubmitMessage` 来自 session contract：human direct chat 可写，A2A/delegation/agent participant 只读，pending lookup resolving，不由 transport 决定。

2. **`+` 新建改为 draft-first**
   - 点击 `+` 只创建本地 `draft_human_chat`。
   - 未输入不创建后端 session。
   - 首次发送时原子创建 durable session 和 run。

3. **active run 查询必须 idle-safe**
   - 无 active run 返回或适配为 `null`。
   - `null` 只清理 waiting/running，不改变 composer 可用性。
   - active-run 查询失败不能把 session 卡成 `Connecting...`。

4. **process narrative 是一等 UI**
   - 不允许只显示 tool chip。
   - 不允许只显示最终 answer。
   - 每个 move-forward step 都要有可见 process summary。

5. **后台/协作必须发出和回收双向反馈**
   - A2A / subagent / workflow / scheduled/background task 发出时写 `runtime_action_started`。
   - 运行中写 progress。
   - 完成/失败/阻塞写 completion event。
   - completion wake 后主 Agent 在 root session 给最终交付。

### 实施顺序

本轮 Session TUI / interaction parity 建议拆成六个工作包，顺序不能反过来：

1. **文档与 contract 冻结** — 已完成
   - 本节作为 2026-07-01 session interaction parity 的权威契约。
   - 旧的 2026-06-30 “runtime closure”不撤销，但范围限定为事件/回放/A2A completion，不覆盖 composer/session creation。
   - 明确“底层链路已存在，剩余重点是 TUI 表达和交互状态分层”，避免误判成后端主链重做。

2. **Red tests** — 已完成
   - 新建 draft 不 POST 后端。
   - `wsConnected=false` 时 human direct session composer 仍可输入和发送。
   - A2A / pending lookup 仍不可输入。
   - active-run `404/null` 不改变 composer 可用性。
   - live / refresh / reconnect timeline 一致。
   - tool_result 默认折叠但有一眼可读的 result summary。
   - provider 无 thinking 时不出现空白 Thinking；runtime narrative 仍显示工具/任务进度。
   - Plan Mode / AskQuestion / artifact / workflow card 必须挂到同一 run cell，而不是破坏 process loop。

3. **Session interaction 修复** — 已完成
   - 新增 `draft_human_chat` session row 类型。
   - 新增 `canSubmitMessage` selector。
   - 移除 composer、send、upload、slash menu、plan/goal/schedule 对 `!wsConnected` 的硬禁用。
   - WebSocket 状态改成轻量 live update badge。
   - active-run `404/null` 统一走 idle-safe 清理。

4. **TUI 表达 Codex 化** — 已完成到代码级，待 Product E2E
   - `RunDisclosureBlock` 默认展示 process summary，工具 args/result/raw trace 二级折叠。
   - tool done / failed step 要有简短 result summary，不只藏在 details。
   - provider 有 thinking 时展示 reasoning summary；没有 thinking 时展示 runtime narrative，不显示空白 Thinking。
   - completed run 折叠后保留“做了什么”的摘要，展开后看到完整 step loop。
   - Plan Mode / AskQuestion / permission / artifact / workflow / A2A 都作为 run step 或 step detail 渲染，避免脱离 timeline 的独立大卡片。

5. **Backend / API** — 已完成
   - 增加 create-and-run 原子入口，或把现有 start run 扩成 `session_id` 可选且事务内创建 session。
   - 无 active run 统一为 `200 null`，或前端兼容 404 为 null。
   - session metadata 明确返回 `session_kind`、`read_only`、`participant_type`、`source_channel`。
   - 如现有后端事件摘要不足以支撑 TUI result summary，只补 summary contract，不重写 tool execution 主链。

6. **Product E2E** — 人工验收项
   - 覆盖新建、首发、运行、工具、AskQuestion、A2A、workflow、subagent、artifact、refresh、断线重连、completion wake。
   - 验收对象不是单个测试绿，而是用户看到的 session 是否符合“思考路径展开、工具折叠、最终交付”的整体体验。
   - 验收必须包含两类 provider：有 reasoning/thinking 输出的模型，以及无 reasoning/thinking 输出的模型。

## Clean Session Creation Contract — 2026-07-01

### 背景问题

2026-07-01 线上观察到一个新的产品断点：用户点击左侧 `+` 创建空白 session 后，界面能看到新 session 标题，但 composer 长时间显示 `Connecting...`，必须刷新页面后才能输入。

这不是单纯的权限问题，也不是后端一定没有创建 session。根因是当前前端把三个本应独立的概念耦合在一起：

1. **可输入性**：用户是否可以在当前会话输入下一条消息。
2. **实时事件通道**：WebSocket 是否已连接，用于接收 thinking/tool/runtime event。
3. **持久运行状态**：当前 session 是否有 active durable run。

当前实现把 composer 的可输入性绑到了 `wsConnected`。一旦新建空白 session 后 WebSocket 初始连接、active-run 查询或 session lookup 有任何时序抖动，用户就会看到一个已经创建但不可输入的 session。这与 Terminal、CC 和 Codex 的交互模型不一致：这些产品的输入框不应依赖实时事件通道先建立成功。

### 长期目标

长期方案不是继续修补 read-only 或 WebSocket race，而是把 session lifecycle 拆成三层：

1. **Draft Session**：用户点击 `+` 后得到的本地草稿会话。没有用户输入前不创建后端 `ChatSession`，刷新后不保留。
2. **Durable ChatSession**：用户第一次发送消息、打开历史 session、fork、resume、checkpoint、A2A child session 等需要持久证据时才创建或读取的后端 session。
3. **Runtime Transport**：WebSocket / SSE / polling 只是事件订阅与回放通道，不决定用户是否可以输入。

目标体验：

```text
点击 +
  -> 立即进入本地 Draft Session
  -> composer 立即可输入
  -> 没有输入就刷新/离开：不产生后端空 session

第一次发送
  -> 原子创建 durable ChatSession
  -> 原子启动 web_chat_turn RuntimeTask
  -> URL 从 draft 替换为真实 session_id
  -> WebSocket 订阅实时事件；如果 WebSocket 慢或失败，HTTP run + transcript polling 仍继续

运行中
  -> 输入框按 session/run policy 决定是否允许 steer，不按 wsConnected 决定
  -> WebSocket 只是 live feedback；断线时显示 reconnecting badge，不锁死 composer

完成/刷新
  -> transcript replay 还原同一套 reasoning/tool/runtime disclosure
```

### 核心不变量

- `wsConnected=false` 不能让可写 human chat session 变成不可输入。
- `getActiveSessionRun` 没有 active run 时必须被解释为“无运行中任务”，不能把 UI 卡进 `Connecting...`。
- 用户点击 `+` 但没有输入，不应污染后端 session 列表。
- A2A / delegated / agent-agent session 的 read-only 来自后端 session contract，不来自前端 fallback 猜测。
- `is_pending_session_lookup` 只能表示“正在解析一个已存在的 URL session_id”，不能用于新建用户草稿 session。
- session 可输入性由 session contract 判断：
  - current-user direct `human_chat` / `web` session：可输入。
  - A2A `agent_chat` / `delegation_run` / agent participant session：人类只读。
  - unknown requested session：先显示 resolving skeleton；解析失败后显示只读错误态，不伪装成普通会话。
- WebSocket 是 subscribe-only transport；HTTP `startSessionRun` 是启动 durable turn 的权威入口。

### 推荐 API / Runtime Contract

2026-07-01 已新增 create-and-run 原子入口，避免“session 已创建但 run 没启动 / run 查询 404 / WebSocket 未连接”三段状态散落前端。旧 `POST /agents/{agent_id}/sessions` 保留给 slash command 等确实需要先拿 durable session id 的路径；普通 human chat 首条自然语言输入不再走空白 session 创建。

```http
POST /api/agents/{agent_id}/sessions/runs
Content-Type: application/json

{
  "title": "optional title",
  "content": "用户第一条消息",
  "display_content": "用户可见消息",
  "attachments": [],
  "plan_mode_requested": false,
  "permission_mode": "bypassPermissions"
}
```

返回：

```json
{
  "session": {
    "id": "<chat_session_id>",
    "agent_id": "<agent_id>",
    "source_channel": "web",
    "session_kind": "human_chat",
    "is_current_user_session": true,
    "read_only": false
  },
  "run": {
    "run_id": "<runtime_task_id>",
    "status": "running"
  }
}
```

服务端语义：

- 该入口只处理 draft 首条消息：创建 `ChatSession` 后立即调用现有 `start_web_chat_run(...)`，由公共 runtime 写入 user transcript、创建 `RuntimeTask(web_chat_turn)` 并 commit。
- 如果 run 启动失败，返回明确错误；前端不会先落一个 durable 空 session。
- 已存在 durable session 仍走 `POST /agents/{agent_id}/sessions/{session_id}/runs`，继续复用原有权限、active-run、mid-run queue 语义。
- `draft_client_id` 幂等不是本轮必须项；如后续 Product E2E 发现用户重复点击/网络重试产生重复首轮，再补服务端幂等键。

### 前端状态模型

前端需要显式区分四类 session row：

| 类型 | 来源 | 是否持久化 | composer | URL |
| --- | --- | --- | --- | --- |
| `draft_human_chat` | 用户点击 `+` | 否 | 立即可输入 | 不带真实 `session_id`，或使用 `draft_id` |
| `human_chat` | 后端 `ChatSession` | 是 | 可输入 | 真实 `session_id` |
| `a2a_readonly` | 后端 A2A session contract | 是 | 不显示输入框 | 真实 `session_id` |
| `pending_lookup` | 直链打开但 metadata 尚未加载 | 未知 | resolving skeleton，不显示 read-only 标签 | 真实 `session_id` |

前端行为：

- 左侧 `+` 不再立即调用 `chatApi.createSession()`；只创建本地 draft row。
- draft row 可以显示在当前 agent 下，但刷新后消失。
- 用户输入第一条消息时，调用 create-and-run 原子 API；成功后用真实 session 替换 draft，并 `replace` URL。
- 如果 WebSocket 未连接，composer 仍可输入和发送；发送后通过 HTTP 返回的 run handle 进入 waiting/running 状态。
- WebSocket 连接状态只显示为轻量状态提示，例如 `Reconnecting live updates...`，不能作为发送按钮的 disabled 条件。
- 已存在 durable session 的 active-run 查询失败或返回 null 时，只清理 waiting/streaming，不改变 composer 可用性。

### Active Run 查询语义

当前前端 API 类型已经把 `getActiveSessionRun` 建模为 `SessionRun | null`。因此后端和前端必须统一：

- 推荐后端：没有 active run 时返回 `200 null`。
- 兼容前端：如果历史接口仍返回 `404`，前端必须把该 404 解释为 `null`，不显示全局错误，不阻塞输入。
- `active_run` 只影响 run badge、stop button、steer behavior 和 disclosure 状态；不能影响 human chat session 是否可以输入。

### WebSocket / Transport Contract

WebSocket 的职责：

- 订阅当前 session 的 live transcript/runtime events。
- 发送 keepalive / abort 等控制消息。
- 在 live 状态下减少 polling 延迟。

WebSocket 不应承担：

- 创建 session。
- 决定 composer 是否可输入。
- 作为第一条用户消息的唯一发送路径。
- 作为 active run 是否存在的唯一真相源。

如果 WebSocket 未连接：

- 用户仍可发送第一条消息和普通消息。
- 前端用 HTTP `startSessionRun` 或 create-and-run 原子 API 获取 `run_id`。
- 前端可用 `getSessionTranscript(after_sequence)` 或 active-run polling 回放事件。
- UI 明确显示“实时更新重连中”，但不让用户误以为整个 session 失败。

### 与 A2A / Read-only 的边界

这个长期方案不能破坏 A2A 的只读规则：

- A2A session 是否只读由后端字段和 session kind 决定：`source_channel=agent`、`participant_type=agent`、`session_kind=agent_chat/delegation_run`、`read_only=true`。
- draft session 只适用于 human-to-agent direct web chat，不适用于 A2A child session。
- 直链打开 A2A session 时，前端必须先加载后端 metadata；加载期间显示 resolving，不创建本地 writable draft。
- A2A child session 的用户干预仍回到 root session 或 session control action，不在 child composer 里直接输入。

### 迁移完成项

1. **Red tests / Regression tests**
   - `frontend/src/pages/agent-detail/chatRuntime.test.ts`：draft human session writable，且不是 A2A。
   - `frontend/src/pages/agent-detail/AgentDetailSections.test.tsx`：WebSocket disconnected / reconnecting 时 human web session composer 仍可输入，不出现 `Connecting...` 锁死。
   - `frontend/src/api/domains/chat.test.ts`：`chatApi.createSessionRun()` 必须走 `/agents/{agent_id}/sessions/runs`。
   - `backend/tests/api/test_chat_session_runs.py`：`create_session_run` 必须一次性创建 human session 并启动 runtime。
   - `frontend/src/pages/agent-detail/chatDisclosureReducer.test.ts`：tool completion summary 可见，raw payload 仍折叠。

2. **Backend**
   - 已新增 `POST /agents/{agent_id}/sessions/runs`。
   - 已复用 `start_web_chat_run(...)`，不新建第二套 run path。
   - `getActiveSessionRun` 暂由前端把 404 适配为 null；后端长期可再改为 `200 null`，但不再影响 composer 可用性。

3. **Frontend**
   - 已新增本地 draft human session 标记：`is_draft` / `draft_client_id`。
   - `AgentDetail` 的 `+` 已改为创建本地 draft，不直接 POST 后端。
   - `AgentChatSection` 的 composer disabled 条件已从 `!wsConnected` 收敛到 `canUseComposer`。
   - WebSocket 状态已改成状态提示，不再控制 placeholder 主文案和 send button。
   - 首次发送成功后，draft id 替换为后端 session id，并用 `navigate(..., { replace: true })` 更新 URL。

4. **Cleanup**
   - 空 session 不再是普通 `+` 新建路径。
   - pending lookup 与 draft human chat 已分离，A2A read-only contract 保持不变。
   - slash command 仍可按需先创建 durable session，这是显式命令路径，不是普通新建空会话路径。

5. **Observability / Product E2E**
   - 代码级测试已覆盖核心分支。
   - 轻量 telemetry 不是本轮必要项；如 Product E2E 发现线上仍有连接/重复提交/长尾错误，再按具体信号补埋点。
   - 线上人工验收必须覆盖：首次点击 `+`、不输入刷新、输入发送、WebSocket 断开时发送、A2A 只读直链、active-run 无任务状态。

### 验收标准

代码级落地后，以下行为应成立；最终以 Product E2E 为准：

- 点击 `+` 后立刻能输入，不出现需要刷新才能输入的 `Connecting...` 卡死。
- 未输入就刷新，不产生后端空 session。
- 输入第一条消息后才产生 durable session，并立即启动 run。
- WebSocket 断开不会阻止发送；只影响实时更新速度。
- active-run 无任务不会显示错误，也不会改变 composer 可用性。
- A2A session 仍然 read-only，且带 A2A 标签。
- 刷新后 durable session 通过 transcript replay 还原过程摘要、工具折叠、最终回答和 artifact card。

### 当前状态

本文节的核心代码级闭合已完成：draft-first、create-and-run、subscribe-only transport、active-run 404/null idle-safe、A2A read-only 边界均已接入现有主路径。第 6 步 Product E2E 由人工验证，重点确认线上用户看到的体验是否符合本文“用户可见目标”。

### 当前代码事实

- 后端 live run 已接入 `on_chunk`、`on_tool_call`、`on_thinking`、`on_event`，入口在 `backend/app/services/web_chat_runtime.py` 的 `invoke_agent(...)` 调用处。
- `thinking_to_ws(...)` 会广播 thinking delta；`tool_call_to_ws(...)` 会广播并持久化 `tool_call` / `tool_result`。
- 最终 assistant message 会把 `thinking` 写进 transcript parts。
- 前端 `applyTranscriptEvent(...)` 能处理 live `thinking`、`tool_call`、`assistant_message`。
- `RunDisclosureBlock` 已经能聚合 tool / event / reasoning step。

### 新发现的闭环断点

1. **replay 丢 reasoning**：durable `assistant_message` replay 时，前端没有从 `parts` 里的 reasoning/thinking 还原到 `message.thinking`。结果是 live 时可能看得到，刷新后过程消失或只剩最终答案。
2. **final assistant thinking 不进入 run step**：当前 `isDisclosureStepMessage(...)` 只把“无正文但有 thinking”的 assistant 当 reasoning step；如果 final answer 同时带 thinking，则 thinking 被并入 assistant bubble，不进入 `RunDisclosureBlock`。
3. **完成 turn 默认过度折叠**：`RunDisclosureBlock` 只对 running / blocked / failed 默认展开；completed turn 默认只露 chip。CC/Codex 的体验是“可见过程摘要 + 工具细节二级折叠”，不是只展示工具名。
4. **缺统一 runtime feedback contract**：工具、A2A、subagent、workflow、background task 虽然各自有事件或 wake path，但没有统一的 “action started / progress / completed / wake delivered” 可见事件语义。
5. **异步任务发出后缺即时反馈**：`delegate_to_agent` / workflow / background subagent 发出后，root session 应立即出现“已启动，正在后台执行，session/task handle 是什么”的可见反馈；不能等到用户手动 check。
6. **历史回放和 live 展示不等价**：同一个 turn 在 WebSocket live、刷新 transcript、断线重连后三种路径下不保证相同 timeline。

### CC / FreeCode 对照

FreeCode 的远端 SDK adapter 会把 `tool_progress` 转成可见 system message，例如：

```text
Tool <name> running for <seconds>s...
```

这说明 CC 的标准不是“最后给结果即可”，而是每个工具运行阶段都有可见进度；compact boundary、permission、tool result 也都有独立显示/折叠语义。

### Codex 对照

Codex turn runtime 在任务开始时清空 reasoning buffer、进入 `Working` 状态；任务结束时把 runtime metrics / work separator / reasoning summary 固化成 history cell。它区分：

- live task status
- reasoning summary cell
- tool call cell
- background/multi-agent activity cell
- final answer

Hive 要吸收的是这个分层，而不是照抄 Rust 类型。

### 2026-06-29 决议

接下来实现必须按四个闭环面推进：

1. **Runtime Feedback Contract**
   - 新增统一事件语义：`runtime_action_started`、`runtime_action_progress`、`runtime_action_completed`、`runtime_action_blocked`、`runtime_action_failed`。
   - 所有 tool call、A2A、subagent、Agent Team、workflow、background task 都必须映射到同一套 session-visible event。
   - 事件必须 durable，刷新后仍能 replay。

2. **Transcript Replay Parity**
   - `assistant_message.parts` 中的 reasoning/thinking 必须在 replay 时恢复。
   - replay 出来的 timeline 必须和 live WebSocket 看到的一致。
   - `thinking` 不能只依赖 streaming placeholder。

3. **Disclosure UI V2**
   - completed turn 不应只显示 tool chip；应显示过程摘要，工具 raw details 再二级折叠。
   - reasoning/process summary 是 run 的一等 step，不应只挂在最终 assistant bubble 内。
   - 默认显示“做了什么”，默认折叠“参数、raw result、trace payload”。

4. **Async / Background Wake Feedback**
   - 异步任务发出后，root session 立即写入 start/progress feedback。
   - 完成后走统一 wake path 唤醒主 Agent，并把 child result / artifact refs 纳入最终交付。
   - `check_*` 工具只作为 fallback inspection，不作为正常用户体验主路径。
   - 所有后台/child session 启动入口必须自动绑定当前 `parent_session_id` / `root_session_id`；不能依赖模型手动把 `parent_session_id` 填进 tool args。
   - 如果 parent binding 缺失，必须 fail-closed 或写入可见 runtime warning；不能静默启动一个无法 wake 回 root session 的后台任务。

### 当前实现判断

当前代码已经有机制骨架：同步 A2A 会直接返回 reply，异步 A2A / subagent / workflow 都已有 completion wake 入口。

2026-06-30 复核更新：A2A completion wake、artifact delivery 和 disclosure replay 的关键断点已闭合：

- `agent_task_notification` 不再作为 `role=user` 的用户消息进入父 session；timeline 可见内容是 runtime event 摘要，原始 `<task-notification>` envelope 只保留在 metadata 证据里。
- 父 Agent 被唤醒时，notification 通过 `runtime_mailbox_role=system` 进入 `system_prompt_suffix` / mid-run system drain，不覆盖最后一条 user history，也不触发 Plan Mode 用户输入判定。
- 旧 transcript replay 即使仍带 `role=user`，前端也按 `event_type=agent_task_notification` 渲染为 runtime event，不再显示为用户气泡。
- A2A 子 Agent 产物不会机械复制到父 Agent workspace。completion projection 生成 `a2a_delivery_ref` artifact part，保留 `owner_agent_id` / `source_agent_id` / `download_agent_id` 指向产物 Agent，`delivery_agent_id` 指向父 Agent，并携带 markdown/text snapshot 供父会话预览兜底。
- 前端 artifact card / inspector 优先使用 `download_agent_id` 读取或下载文件；如果跨 Agent 读取失败，markdown/text 文件使用 `preview_snapshot_content` 展示保存时快照。
- A2A delegation 发出后会写 durable `runtime_action_started`；completion projection 会先写 `runtime_action_completed` / `runtime_action_blocked` / `runtime_action_failed`，再写 `child_session` 细节与 artifact refs。
- A2A direct-link / session fallback 不再造 `source_channel=web` 的可写临时 session；未知 session 在 canonical metadata 加载前按 read-only pending session 处理。
- `send_message_to_agent` 已收窄为同步短咨询/通知，`msg_type=task_delegate` 退役；任务型语义、后台处理和 artifact 交付必须走 `delegate_to_agent`。
- A2A delegation brief / target prompt 之外，`delegate_to_agent` 现在还有通用 cross-workspace artifact contract：`target_artifacts[]` + `target_artifact_path` shorthand + `edit_mode`。当 `edit_mode=modify_existing` 或 artifact 级 `expected_action=modify_existing` 且完成时没有交付所有 required target paths，completion 被降级为 `runtime_action_blocked` / `delegation_artifact_contract_mismatch`，避免把“新建替代文档 / PPT / 代码文件”误报为交付完成。
- replay parity 已补强：刷新 transcript 时会从 `assistant_message.parts` 还原 reasoning/thinking；带 final answer 的 thinking 也进入 disclosure step；带 reasoning/A2A lifecycle 的 completed run 默认展开过程摘要。

2026-06-30 A2A / artifact / replay 证据：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/agents/test_orchestrator.py::test_delegation_completion_projects_child_session_event_to_parent \
  tests/agents/test_orchestrator.py::test_delegation_completion_projects_a2a_artifact_refs_to_parent \
  tests/agents/test_orchestrator.py::test_modify_existing_delegation_blocks_when_target_artifact_path_not_delivered \
  tests/runtime/test_unified_prompt_contracts.py::test_delegation_tool_descriptions_require_structured_briefs \
  tests/services/test_chat_artifact_delivery.py::test_a2a_delivery_projects_child_artifact_ref_without_copying_parent_workspace \
  tests/services/test_chat_artifact_delivery.py::test_a2a_delivery_projects_latest_duplicate_child_artifact_ref_only \
  tests/services/test_chat_message_parts.py::test_runtime_action_started_is_session_native_event \
  tests/services/test_web_chat_runtime.py::test_delegate_tool_result_builds_runtime_action_started_event -q
# 8 passed

cd backend && source .venv/bin/activate && pytest tests/runtime/test_unified_prompt_contracts.py::test_delegation_tool_descriptions_require_structured_briefs tests/services/test_prompt_contracts.py::test_a2a_prompt_defines_status_and_result_contract -q
# 2 passed

cd frontend && npm test -- --run src/pages/agent-detail/chatRuntime.test.ts src/pages/agent-detail/chatDisclosureReducer.test.ts src/pages/agent-detail/RunDisclosureBlock.test.tsx
# 3 test files passed, 57 tests passed

cd frontend && npm run build
# passed
```

### Residual Closure — 2026-06-30

上一轮遗留的三个产品闭环项已经补到代码级，不再作为本文档的 open blocker：

1. **非 A2A workflow/subagent/background task 统一 runtime action**
   - `WorkflowRuntimeService` 保留原有 `workflow_run` / `workflow_step` 证据事件，同时额外投影 `runtime_action_started`、`runtime_action_progress`、`runtime_action_completed` / `runtime_action_failed` / `runtime_action_blocked`。
   - `continue_parent_session_with_task_notification(...)` 对 workflow、subagent、agent team、background task 的 completion wake 先写 `runtime_action_*`，再进入 mailbox continuation。
   - A2A completion 不在 mailbox 层重复投影，因为 A2A orchestrator 已经有专用 `runtime_action_completed` / `blocked` / `failed` projection；这里显式跳过是为了避免重复 chip。

2. **统一 durable progress feedback**
   - Workflow step start/done/failed/suspended 均被映射为 durable `runtime_action_progress`，刷新 replay 后仍能被前端 disclosure reducer 识别为 workflow step。
   - `spawn_subagent(run_in_background=true)` 的 tool result 现在会被 `web_chat_runtime` 派生为 `runtime_action_started`，不再只藏在 raw tool result 里。
   - completion wake 的 runtime action 使用同一套 session-native event builder，前端不需要为 workflow/subagent/background 再写独立分支。

3. **live / replay / reconnect 回归矩阵补强**
   - 后端 `build_session_native_event(...)`、workflow session projection、task notification continuation、tool-result derived feedback 都有 focused regression。
   - 前端 `chatRuntime` / `chatDisclosureReducer` / `RunDisclosureBlock` 针对 transcript replay、reasoning restore、runtime action classification、completed run disclosure 继续保持绿灯。

本轮新增证据：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_workflow_runtime_service.py::test_start_run_projects_workflow_progress_into_parent_session \
  tests/services/test_agent_session_continuation.py::test_task_notification_projects_runtime_action_before_mailbox \
  tests/services/test_web_chat_runtime.py::test_spawn_subagent_tool_result_builds_runtime_action_started_event -q
# 3 passed, 3 warnings

cd backend && source .venv/bin/activate && pytest \
  tests/services/test_workflow_runtime_service.py \
  tests/services/test_agent_session_continuation.py \
  tests/services/test_web_chat_runtime.py::test_delegate_tool_result_builds_runtime_action_started_event \
  tests/services/test_web_chat_runtime.py::test_spawn_subagent_tool_result_builds_runtime_action_started_event \
  tests/services/test_chat_message_parts.py -q
# 36 passed, 3 warnings

cd backend && source .venv/bin/activate && ruff check \
  app/services/workflow_runtime_service.py \
  app/services/agent_session_continuation.py \
  app/services/web_chat_runtime.py \
  tests/services/test_workflow_runtime_service.py \
  tests/services/test_agent_session_continuation.py \
  tests/services/test_web_chat_runtime.py
# All checks passed

cd frontend && npm test -- --run \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/pages/agent-detail/chatDisclosureReducer.test.ts \
  src/pages/agent-detail/RunDisclosureBlock.test.tsx
# 3 test files passed, 58 tests passed
```

结论：本文档 2026-06-29/30 re-audit 中列出的 runtime feedback、transcript replay、Disclosure UI V2、async/background wake feedback 四个闭环面，当前都已有代码级闭合和 focused regression。未声明“生产所有长尾体验无需再观察”；但不再存在本轮列出的设计 blocker。

### 不变量

- 不默认暴露完整 chain-of-thought。
- 不把 raw logs 当产品反馈。
- 不允许 live 能看到、刷新后看不到。
- 不允许后台任务只在右侧状态栏变化而 root session 没有任何反馈。
- 不允许没有 parent/root session binding 的后台任务假装已进入闭环。
- 不允许 tool result / notification / workspace 文件替代 transcript。

## 0. 历史设计结论（2026-06-22 基线）

Hive 现在不是“完全没有 transcript replay 基础”。后端已经能广播和持久化 `thinking`、`tool_call`、`tool_result`、`permission`、`session_compact` 等事件；前端也已经能从 WebSocket 和 transcript history 还原部分消息。

2026-06-22 当时真正缺的是 CC/Codex 标准里的 **turn-level disclosure layer**；2026-06-29/30 re-audit 发现的剩余闭环项已经在上文 `Residual Closure — 2026-06-30` 里补齐并记录测试证据：

1. 每个 assistant turn 要有一个可折叠的运行过程块，类似 Codex 的“已处理 2m46s”。
2. 普通工具调用、搜索、读取、写入、命令、权限、压缩、AskQuestion 都必须成为可见 step，而不是默认吞掉。
3. 默认展示人能读懂的摘要；参数、结果、trace id、raw payload 放到展开区。
4. 刷新后要能从 transcript replay 出同一套过程，而不是只剩最终答案或卡在 Thinking。
5. 后端事件 contract 要补齐稳定 step id、run id、duration、started/done 持久化和 visibility policy。

这不是把所有内部日志倒给用户，也不是暴露私密 chain-of-thought；它是把“agent 做了什么”变成可复盘、可折叠、可治理的产品层。当前实现状态以上文 `Implementation Status`、`Re-audit` 和 `Residual Closure` 为准。

## 1. 用户可见目标

对齐的目标以 CC/Codex 的对话体验为准。用户首先看到的是 agent 每一步“为什么继续往前走”的可见过程摘要；工具调用是该 step 的执行证据，默认折叠在 step 下面，而不是把 raw tool call 当成主叙事。

```text
用户消息

已处理 2m46s  v
  我先确认当前文档和 A2A artifact contract 的真实状态，避免按旧结论修改。
    > tool calls collapsed: search code, read files
  我发现目标 artifact contract 已经存在，但完成回收还缺跨 workspace 路径校验。
    > tool calls collapsed: edit file, run focused tests
  测试通过后，我把文档更新成通用 cross-workspace artifact 规则。
    > tool calls collapsed: run tests, update docs

最终回答：
  1. 总结
  2. 呈现 / 结论
  3. 文件 / artifact 入口（如果有）
```

运行中则应是：

```text
正在处理 18s  v
  我会先检查当前实现和测试面，确认 runtime feedback 是否已经走统一 contract。
    > tool calls collapsed: rg, read files
  我现在发现 workflow 已有私有事件，但还没有投影为 runtime_action_progress。
    > tool calls collapsed: write regression test
  我先让测试红掉，再把 workflow/subagent/background wake 接入统一事件面。
    > tool calls collapsed: patch files, run tests
```

核心交互：

- 运行中默认展开关键 step 的思路摘要，不能空白、不能只显示一个永远不结束的 Thinking。
- 每个 step 的主内容是 agent 的可见思考路径表达：我为什么这么做、发现了什么、下一步怎么推进。
- 工具调用默认折叠在对应 step 下面；用户能展开看工具名、参数摘要、结果摘要、trace，但 raw args/result 不作为主界面叙事。
- 完成后 run block 可以折叠为一行 summary；展开后仍能看到“思考 -> 折叠工具 -> 思考 -> 折叠工具”的过程链。
- 最终 assistant 输出是组合交付：总结、呈现/结论、文件或 artifact 入口。workspace 文件、runtime notification、tool result 不能替代这个最终交付。
- AskQuestion 是一个阻塞交互 step，UI 用一个面板承载多个问题，并支持左右/下一题切换，不把多个问题散成互不关联的卡片。
- 刷新页面后，过程块和最终答案都能通过 transcript 回放出来。

## 2. 当前 Hive 已有基础

### 2.1 后端事件基础已经存在

当前后端已经有消息 parts 和事件 builder：

- `backend/app/services/chat_message_parts.py`：
  - `_build_tool_call_part(...)` 生成 `type="tool_call"` part。
  - `build_thinking_event(...)` 生成 `type="thinking"` event。
  - `build_tool_call_event(...)` 生成 `type="tool_call"` event。
  - `build_done_event(...)` 生成最终 assistant parts。
- `backend/app/services/web_chat_runtime.py`：
  - `thinking_to_ws(...)` 广播 thinking delta。
  - `runtime_event_to_ws(...)` 广播并持久化 `permission`、`session_compact`、`tool_group_activation` 等 runtime event。
  - `tool_call_to_ws(...)` 对 running tool call 直接广播，对 done tool call 持久化后再广播。
  - `_persist_tool_call(...)` 把 done tool result 落成 transcript event。
- `backend/app/models/chat_transcript_event.py`：
  - 已有 append-only replay surface：`event_type`、`sequence`、`run_id`、`actor_type`、`visibility_scope`、`listed_surface`、`parts_json`、`metadata_json`。

这说明后端不是从零开始；问题在于事件语义还没有稳定成一个“过程披露 contract”。

### 2.2 前端 replay 基础已经存在

当前前端已经有基本 replay 和 runtime normalization：

- `frontend/src/pages/AgentDetail.tsx`：
  - WebSocket 已处理 `thinking`、`chunk`、`tool_call`、`done`、`error`、`quota_exceeded`。
  - `thinking` 会追加到 streaming assistant placeholder。
  - `tool_call` 会追加为 `role="tool_call"` 消息。
- `frontend/src/pages/agent-detail/chatRuntime.ts`：
  - `applyTranscriptEvent(...)` 能处理 `thinking`、`chunk`、`tool_result/tool_call`、`assistant_message`。
  - `toolResultFromTranscriptEvent(...)` 能 unwrap 已持久化的 tool-result envelope。
  - `getRuntimeEventMessage(...)` 能把部分 runtime events 映射成 UI message。

这同样说明“刷新后回放”有基础，但当前 projection 不是 CC/Codex 的 turn timeline。

### 2.3 2026-06-22 关键缺口（历史）

`frontend/src/pages/agent-detail/AgentChatSection.tsx` 当前对 `role="tool_call"` 做了特殊分支：只有 Plan Mode、clarification、HR preview / create success 这些 special kind 会 inline render；普通 tool call 直接 `return null`。这就是用户看到“工具过程都没了”的直接原因之一。

另外，assistant 的 `thinking` 现在更像一个空 assistant 消息里的 `<details>` 卡片，而不是当前 turn 的 process step。最终效果是：

- 有些内部事件被发出，但默认 UI 不显示。
- 运行中长工具调用可能让用户只看到 Thinking 或空窗。
- `tool_call running` 和 `tool_call done` 没有稳定更新同一个可见 step。
- transcript replay 回来的是消息列表，不是一个完整的 per-turn disclosure snapshot。

这些是本文档创建时的历史缺口；2026-06-29/30 的代码级闭合与回归证据见文档顶部状态段。

## 3. 旧文档为什么不够

仓库里已有 `docs/archive/legacy-docs/CHAT_UX_SOTA_PLAN.md`，它抓到了几个正确问题：

- “Every agent action is visible enough to trust.”
- `thinking`、`tool_call running/done`、`permission`、`session_compact` 都应该进 timeline。
- `tool_call` 默认隐藏是不对的。
- 需要 reducer，把 WebSocket / transcript event reduce 成 `TimelineSnapshot`。

但它已经在 archive，不是当前 truth surface；在本文档创建时仍有以下历史缺口：

1. 对标对象主要是旧 Onyx timeline，不是这次用户要求的 CC/Codex。
2. 没把 Codex 的 `TurnItem`、tool status、duration、RequestUserInput 事件结构纳入设计。
3. 没把 Claude Code 的 `tool_progress`、compact boundary、permission request、tool-result collapse 作为对照标准。
4. 没充分强调 backend durability：running step 如果不持久化，刷新期间会丢过程。
5. 没把 AskQuestion 定义成一个 blocking process step + 单面板多问题交互。
6. 没把 visibility policy 明确拆成默认可见、默认折叠、debug/admin。

本文档替代它成为当前实现入口；旧文档只保留为历史参考。

## 4. CC/Codex 对标模型

### 4.1 Codex 的关键形态

本地 Codex source 里，conversation item 是 typed union：

```text
TurnItem =
  UserMessage
  AgentMessage
  Plan
  Reasoning
  WebSearch
  ImageView
  ImageGeneration
  FileChange
  McpToolCall
  ContextCompaction
```

其中：

- `ReasoningItem` 有 `summary_text` 和 `raw_content`，说明 summary 和 raw 是分层的。
- `McpToolCallItem` 有 `id`、`server`、`tool`、`arguments`、`status`、`result`、`error`、`duration`。
- `McpToolCallStatus` 有 `InProgress`、`Completed`、`Failed`。
- `RequestUserInputEvent` 绑定 `call_id`、`turn_id` 和 `questions`，说明 AskQuestion 属于某个 turn 的阻塞事件，不是散落消息。

Hive 要学习的是这个语义模型，而不是照抄 Rust 类型。

### 4.2 Claude Code 的关键形态

本地 Claude Code source 里，远端 SDK message adapter 把：

- `tool_progress` 转成可见 system message，例如 tool 正在运行多久。
- compact boundary 转成可见的 “Conversation compacted”。
- remote tool result 转成能像本地 tool result 一样 render 和 collapse 的消息。
- permission request 走明确的 `can_use_tool` 控制路径。

Hive 要学习的是：成功结果可以降噪，但过程本身不能消失；工具结果要能折叠，不是默认隐藏。

## 5. Hive 应采用的 Disclosure Taxonomy

### 5.1 默认可见

这些必须默认出现在聊天主流程里：

- 用户消息。
- assistant 最终回答。
- 当前 turn 的 running/completed header，例如“正在处理 18s”或“已处理 2m46s”。
- step 摘要：
  - Thinking / reasoning summary。
  - web_search / web_fetch / firecrawl / search。
  - read_file / write_file / edit_file / list_files。
  - execute_code / run_command / office / mcp tool。
  - permission request。
  - AskQuestion / Plan Mode confirmation。
  - session_compact。

### 5.2 默认折叠但可展开

这些不应占满主界面，但用户需要能打开：

- tool args summary。
- tool result summary。
- reasoning summary 的完整文本。
- compaction summary 和 token pressure detail。
- Work Ledger / TodoList / finding 细节。
- artifact metadata。

### 5.3 只在 debug/admin/trace 模式可见

这些不能默认暴露：

- provider raw request / raw response。
- prompt internals。
- secret、credential、token、URL userinfo。
- full private chain-of-thought，除非 provider 明确返回可展示摘要并通过 visibility policy。
- 大型 raw tool output。默认只给 summary 和 artifact/ref 链接。

## 6. 前端目标架构

### 6.1 新增 turn-level projection

新增一个专门的 runtime disclosure projection，不再让 `AgentChatSection` 直接理解所有原始 WebSocket event。

建议文件：

```text
frontend/src/pages/agent-detail/chatDisclosureTypes.ts
frontend/src/pages/agent-detail/chatDisclosureReducer.ts
frontend/src/pages/agent-detail/RunDisclosureBlock.tsx
frontend/src/pages/agent-detail/RunDisclosureHeader.tsx
frontend/src/pages/agent-detail/RunStepRow.tsx
frontend/src/pages/agent-detail/RunStepDetails.tsx
```

核心类型：

```ts
export type RunStepKind =
  | 'reasoning'
  | 'tool'
  | 'search'
  | 'file'
  | 'command'
  | 'permission'
  | 'question'
  | 'plan'
  | 'compaction'
  | 'workflow'
  | 'subagent'
  | 'artifact'
  | 'event';

export type RunStepStatus = 'queued' | 'running' | 'blocked' | 'done' | 'failed' | 'cancelled';

export interface RunStepSnapshot {
  id: string;
  runId?: string;
  toolCallId?: string;
  kind: RunStepKind;
  title: string;
  subtitle?: string;
  status: RunStepStatus;
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
  summary?: string;
  details?: unknown;
  rawRef?: string;
  visibility: 'visible' | 'collapsed' | 'debug';
  blocking?: boolean;
}

export interface RunTimelineSnapshot {
  turnId: string;
  runId?: string;
  status: 'idle' | 'running' | 'blocked' | 'done' | 'failed' | 'cancelled';
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
  steps: RunStepSnapshot[];
  answerMessageId?: string;
}
```

### 6.2 渲染结构

`AgentChatSection` 应把一个 assistant turn 渲染为：

```text
<RunDisclosureBlock snapshot={timeline} />
<AssistantAnswer message={assistantMessage} />
```

而不是：

```text
tool_call message scattered in chat list
thinking empty assistant card
assistant final answer
```

具体要求：

- 普通 `tool_call` 不允许再 `return null`。
- `thinking` 不再作为孤立 assistant empty message；它要 merge 成当前 turn 的 `reasoning` step。
- `tool_call running` 和 `tool_call done` 必须更新同一个 step。
- 如果只有 done transcript，没有 running event，也要能 replay 成一个 completed step。
- `permission`、`session_compact`、`tool_group_activation`、`pack_activation` 进入同一个 timeline。
- Work Ledger / TodoList 作为折叠 step 或右侧 dock，不覆盖主答案。

### 6.3 AskQuestion 交互标准

AskQuestion 是一个 blocking step：

```text
正在等待你的回答
  3 个问题 · 第 1/3 个
```

UI 标准：

- 一个面板承载一个 tool call 的所有 questions。
- 多问题用 stepper / carousel / 下一题，而不是多个并行卡片。
- 回答前该 turn 状态是 `blocked`。
- 回答后 step 变 `done`，保留问题和用户答案摘要。
- 刷新后面板仍能根据 transcript / active run 恢复。

这个设计对齐 Codex `RequestUserInputEvent(call_id, turn_id, questions)` 的语义。

## 7. 后端 contract 差距

现有后端可以发事件，但要达到 CC/Codex 级别，还要补稳定 contract。

### 7.1 必须补齐的字段

所有 runtime disclosure event 应尽量带：

```json
{
  "type": "tool_call",
  "run_id": "...",
  "turn_id": "...",
  "step_id": "...",
  "tool_call_id": "...",
  "name": "read_file",
  "status": "running|done|failed",
  "started_at": "...",
  "completed_at": "...",
  "duration_ms": 1234,
  "visibility_scope": "direct_user",
  "listed_surface": "chat",
  "summary": "Read 4 files",
  "details": {},
  "raw_ref": "..."
}
```

### 7.2 running 事件也要可恢复

当前 `tool_call_to_ws(...)` 对非 done 状态主要是广播，不一定持久化成 replayable step。这样刷新期间会丢掉“正在做什么”的信息，只能靠 active run polling 猜。

目标：

- `tool_started` / `tool_call running` 也持久化轻量 transcript event。
- `tool_call done/failed` 更新或追加同一 `tool_call_id` 的 completion event。
- 前端 reducer 用 `tool_call_id` 合并 running/done。
- 如果历史只有 done，也能降级显示 completed step。

### 7.3 raw payload 不应内联进主消息

Codex rollout trace 会把重 payload 作为 reference，而不是全塞进主 UI item。Hive 也应该把大型 tool result、provider raw payload、artifact body 放到 `raw_ref` / artifact ref，主消息只放 summary。

### 7.4 visibility policy 要成为 contract

`ChatTranscriptEvent` 已有 `visibility_scope` 和 `listed_surface`，但 disclosure UI 需要正式使用它：

- `visible`：默认进主 timeline。
- `collapsed`：默认有 step，但详情折叠。
- `debug`：普通用户主界面不展示，只在调试/管理员 trace 面打开。
- `redacted`：敏感字段脱敏，只显示安全摘要。

## 8. Runtime Source 统一原则

每种 runtime source 都应 create/bind `ChatSession`，并把“为什么启动、发生了什么、治理事件、最终结果、产物链接”写成可回放 transcript。

范围包括：

- Web chat。
- Plan Mode。
- AskQuestion。
- Workflow。
- Subagent/delegation。
- Trigger / schedule。
- Heartbeat / dream。
- Office / document work。
- Remote workstation / code execution。

Workspace 文件仍是文件 source of truth，但不能替代 chat transcript。`RuntimeTask.result_summary` 和 notification 只能是摘要/入口，不是完整 completion path。

## 9. 实施计划

### P0 — 文档和 contract 冻结

交付：

- 本文档进入 `docs/README.md` Active Design Areas。
- 明确旧 `CHAT_UX_SOTA_PLAN.md` 只作为历史参考。

验收：

```bash
rg -n "chat-runtime-disclosure|Chat Runtime Disclosure" docs/README.md docs/chat-runtime-disclosure-cc-codex-alignment-2026-06-22.md
git diff --check -- docs/chat-runtime-disclosure-cc-codex-alignment-2026-06-22.md docs/README.md
```

### P1 — 前端先闭环可见性

目标：不等后端大改，先用现有事件把用户最痛的问题修掉。

Red tests：

```bash
cd frontend
npm test -- --run \
  src/pages/agent-detail/chatDisclosureReducer.test.ts \
  src/pages/agent-detail/AgentChatSection.test.tsx \
  src/pages/agent-detail/AskUserQuestionCard.test.tsx
```

必须覆盖：

- `thinking` reduce 成 `reasoning` step。
- `tool_call running` 生成 running step。
- `tool_call done` 更新同一 step。
- transcript `tool_result` replay 生成 completed step。
- 普通 tool call 默认可见，不再 `return null`。
- `session_compact` 默认短状态，detail 折叠。
- AskQuestion 多问题在一个面板中 stepper 化。
- refresh 后过程块仍存在。

实现：

- 新增 disclosure reducer 和 UI components。
- `AgentChatSection` 改为渲染 `RunDisclosureBlock + AssistantAnswer`。
- 继续保留 Plan Mode / HR preview 的特殊交互，但挂到 question/plan step 下，而不是绕开 timeline。

### P2 — 后端事件 durability

目标：刷新、断线、重启后，过程不丢。

Backend red tests：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_web_chat_runtime.py \
  tests/api/test_chat_sessions.py \
  -q
```

必须覆盖：

- running tool call 落 transcript event。
- done/failed tool call 带同一个 `tool_call_id`。
- event 带 `run_id`、`turn_id`、`step_id`、duration。
- visibility policy 对 secrets / raw payload 做 redaction。
- transcript replay order 稳定。

实现：

- 扩展 `chat_message_parts.py` 的 tool event schema。
- 扩展 `web_chat_runtime.py::_persist_tool_call(...)` 或新增 `_persist_tool_step(...)`。
- 对 `runtime_event_to_ws(...)` 的 permission/compaction/tool activation 统一 step event metadata。
- 更新 frontend adapter 兼容新旧 event。


目标：后台长任务和协作任务不要另开一套不可回放 UI。

覆盖：

- Workflow step/leaf journal 映射进 nested timeline。
- Subagent spawn/delegate/result 映射进 nested timeline。
- Trigger / schedule 触发的结果有 ChatSession replay window。

### P4 — 环境信息和高级 trace

对齐 Codex 右侧环境卡，但不要抢主聊天空间。

可选信息：

- 当前 workspace / branch / changed files。
- agent / model / budget / run id。
- source / connector / channel。
- artifact summary。
- cost / token / duration。

默认只显示摘要；完整 trace 进入 debug/admin panel。

## 10. 验收标准

功能验收：

- 用户能在最终答案前看到 agent 正在做什么。
- 用户能展开完成 turn 的过程，看到搜索、读写、工具调用、命令、权限、压缩、问题。
- 普通工具调用不再被默认隐藏。
- AskQuestion 多问题是单面板 stepper，不是多个并行问题卡。
- 刷新页面后，过程和最终答案都能 replay。
- 断线重连时，active run 的正在执行 step 不消失。
- 过程可见，但 raw payload / secrets / provider internals 不默认暴露。

工程验收：

- Frontend reducer 有 deterministic tests。
- Backend transcript event 有 durability tests。
- 新旧 transcript event 兼容。
- `npm run build` 通过。
- 相关 backend targeted tests 通过。

## 11. 非目标

本设计不做这些事：

- 不默认暴露完整 chain-of-thought。
- 不把 debug console 搬进聊天主界面。
- 不把所有 raw tool result 展开显示。
- 不用 “showInternalTrace” 作为普通工作过程的开关；核心过程默认就是产品体验的一部分。
- 不让 workspace/result_summary/notification 替代 session transcript。

## 12. 历史最小下一步（已执行）

当时的下一轮实现从 P1 开始，先写 frontend red tests：

1. `chatDisclosureReducer.test.ts`：定义 event -> timeline snapshot。
2. `AgentChatSection.test.tsx`：确认普通 tool call 可见、thinking 不再孤立、completed header 可展开。
3. `AskUserQuestionCard.test.tsx`：确认多问题单面板 stepper。

然后做最小实现。P1 完成后再推进 P2 后端 durability，避免只做 UI 糊层却无法刷新回放。当前状态不再以本节为待办；以上文 2026-06-30 的 closure evidence 为准。
