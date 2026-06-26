# Chat Runtime Disclosure 对齐 CC/Codex 设计文档

> 状态：2026-06-22 当前设计入口。P1-P3 已按本文档落到代码；P4 环境信息侧栏按产品判断跳过。本轮不部署。

## Implementation Status — 2026-06-22

- P0 文档入口：已完成，本文档挂入 `docs/README.md`。
- P1 前端 disclosure layer：已完成。普通 `tool_call` 不再默认隐藏；连续的 `thinking/tool_call/session_compact/permission` 会按 assistant turn 聚合成一个 `RunDisclosureBlock`；AskQuestion 保持单面板多问题交互。
- P2 后端 durable tool step contract：已完成。`running` tool call 也写 transcript；`done/failed` 写 completion event；事件带 `tool_call_id`、`step_id`、`duration_ms`、`visibility` 并回填到 WebSocket payload。
- P4 环境信息侧栏：跳过，不作为当前目标。

## 0. 结论

Hive 现在不是“完全没有 transcript replay 基础”。后端已经能广播和持久化 `thinking`、`tool_call`、`tool_result`、`permission`、`session_compact` 等事件；前端也已经能从 WebSocket 和 transcript history 还原部分消息。

真正缺的是 CC/Codex 标准里的 **turn-level disclosure layer**：

1. 每个 assistant turn 要有一个可折叠的运行过程块，类似 Codex 的“已处理 2m46s”。
2. 普通工具调用、搜索、读取、写入、命令、权限、压缩、AskQuestion 都必须成为可见 step，而不是默认吞掉。
3. 默认展示人能读懂的摘要；参数、结果、trace id、raw payload 放到展开区。
4. 刷新后要能从 transcript replay 出同一套过程，而不是只剩最终答案或卡在 Thinking。
5. 后端事件 contract 要补齐稳定 step id、run id、duration、started/done 持久化和 visibility policy。

这不是把所有内部日志倒给用户，也不是暴露私密 chain-of-thought；它是把“agent 做了什么”变成可复盘、可折叠、可治理的产品层。

## 1. 用户可见目标

对齐的目标以 CC/Codex 的对话体验为准：

```text
用户消息

已处理 2m46s  v
  Searched code and listed files
  Read 4 files
  Read 5 files
  Ran 1 command
  Conversation compacted
  Asked 3 clarification questions

最终回答
```

运行中则应是：

```text
正在处理 18s  v
  Thinking...
  Reading files...
  Calling web_search...
  Waiting for your answer...
```

核心交互：

- 完成后默认折叠为一行 summary，但用户可以展开看过程。
- 运行中默认展开关键 step，不能空白、不能只显示一个永远不结束的 Thinking。
- 普通 tool call 必须可见；只有 raw args/result 默认折叠。
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

### 2.3 当前关键缺口

`frontend/src/pages/agent-detail/AgentChatSection.tsx` 当前对 `role="tool_call"` 做了特殊分支：只有 Plan Mode、clarification、HR preview / create success 这些 special kind 会 inline render；普通 tool call 直接 `return null`。这就是用户看到“工具过程都没了”的直接原因之一。

另外，assistant 的 `thinking` 现在更像一个空 assistant 消息里的 `<details>` 卡片，而不是当前 turn 的 process step。最终效果是：

- 有些内部事件被发出，但默认 UI 不显示。
- 运行中长工具调用可能让用户只看到 Thinking 或空窗。
- `tool_call running` 和 `tool_call done` 没有稳定更新同一个可见 step。
- transcript replay 回来的是消息列表，不是一个完整的 per-turn disclosure snapshot。

## 3. 旧文档为什么不够

仓库里已有 `docs/archive/legacy-docs/CHAT_UX_SOTA_PLAN.md`，它抓到了几个正确问题：

- “Every agent action is visible enough to trust.”
- `thinking`、`tool_call running/done`、`permission`、`session_compact` 都应该进 timeline。
- `tool_call` 默认隐藏是不对的。
- 需要 reducer，把 WebSocket / transcript event reduce 成 `TimelineSnapshot`。

但它已经在 archive，不是当前 truth surface，并且还有几个缺口：

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

## 12. 当前最小下一步

下一轮实现应从 P1 开始，先写 frontend red tests：

1. `chatDisclosureReducer.test.ts`：定义 event -> timeline snapshot。
2. `AgentChatSection.test.tsx`：确认普通 tool call 可见、thinking 不再孤立、completed header 可展开。
3. `AskUserQuestionCard.test.tsx`：确认多问题单面板 stepper。

然后做最小实现。P1 完成后再推进 P2 后端 durability，避免只做 UI 糊层却无法刷新回放。
