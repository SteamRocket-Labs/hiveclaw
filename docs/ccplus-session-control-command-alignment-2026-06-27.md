# CCPlus Session Control Command 对齐方案

日期：2026-06-27

状态：Workstream A 的 typed result / raw JSON suppression、manual `/compact` / `/rewind` next-turn context consumption、workspace rewind snapshot、hidden/internal command user surface 裁决、前端 session command control panel 已按本文测试口径实装。后续 AgentTool / Completion Bus / Agent Team / A2A Session-first work 必须复用同一 typed session command result，不得新增第二条 slash-command 控制路径。

范围：session 内 slash command 的完整控制面，包括状态改变、只读查询、UI-only 交互、prompt 包装命令，以及 `/btw` 这类 side-question 命令。本文不处理 A2A 产品层协议。

文档关系：本文是 `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md` 主线 A（Session Control Spine）的专项 contract。上线前最后一轮的总顺序、与 AgentTool / Agent Team / A2A / TurnEnvelope 的依赖关系，以该 master plan 为准。

## 0.0 Workstream A 实装证据

复核修正：本节最初只证明 command result shape、metadata/event 写入和 raw JSON suppression。2026-06-27 后续实现已把 `/compact` / `/rewind` active projection 接入 `web_chat_runtime._load_runtime_context()`，把 workspace rewind snapshot 接入 user checkpoint capture / restore path，并把前端 `ui_action` 接入 session command control panel；证据见 `docs/ccplus-unclosed-gap-register-2026-06-27.md#1-已关闭项`。

实装入口：

- `backend/app/services/session_command_runtime.py`
  - 所有 session command result 统一返回 `ok` / `command` / `action` / `session_id` / `ui_action` / `control_event` / `debug_payload`。
  - `/compact` 调用 LLM session summary path，写 `session_compact`，并把 `active_projection.projection_reason = "compact"` 写入 `ChatSession.transcript_metadata_json`；如果没有 messages 或 summary model 不可用，返回 `ok=false action=not_supported`，不伪造成功。
  - `/rewind` 不再调用 `create_conversation_branch(...)`；无 checkpoint 时返回 `open_checkpoint_selector`，有 checkpoint 时写 `session_rewind` 并更新 active projection；`mode=workspace|both` 会查找 checkpoint workspace snapshot，缺失时返回显式 `not_supported`，存在时必须确认后恢复。
  - `/clear` 创建新 `ChatSession.id` 并返回 `ui_action.type = "switch_session"`。
  - `/branch` 是用户命令的唯一 branch path，调用 `create_conversation_branch(... mode="branch")` 并写 `session_branch`。
- `backend/app/services/session_workspace_snapshot.py`
  - 捕获和恢复范围硬限定为 `AGENT_DATA_DIR/<agent_id>/workspace`。
  - 每个 user checkpoint snapshot 写入 `ChatSession.transcript_metadata_json.workspace_snapshots`。
  - incomplete snapshot fail-closed，不恢复 memory、soul、skills、logs 或治理状态。
- `backend/app/services/conversation_branch_service.py`
  - 新增 `branch` mode；`fork` 保留为 legacy/internal compatibility，但 `/branch` 用户命令不再走 `fork` mode。
- `frontend/src/pages/agent-detail/sessionCommandResult.ts`
  - 前端统一识别 typed `ui_action`，typed session control result 默认不再格式化为 assistant JSON。
- `frontend/src/pages/AgentDetail.tsx`
  - slash command 执行后先消费 `ui_action`：`switch_session` 切 session，其它 control action 走 toast / panel / clipboard，不追加 raw assistant JSON。
  - `open_resume_picker`、checkpoint selector、projection/status、context/usage/export/permissions 都进入同一 `SessionCommandControlPanel`。
- `backend/app/api/commands.py`
  - Web command schema endpoint 和 execute endpoint 都复用同一个 user-visible surface gate。
  - `goal_start`、`team_create`、`task_create`、`schedule_create`、`schedule_once` 等 canonical/internal names 对 Web 用户 404；用户只走 `/goal`、`/team`、`/task`、`/schedule`、`/once`。
- `frontend/src/pages/agent-detail/slashCommand.ts`
  - 手写 slash parser 会拒绝 hidden/internal command names；隐藏命令不能绕过菜单直接从 Web composer 执行。

验证命令：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_session_command_runtime.py \
  tests/services/test_session_control_plane.py \
  -q
# 27 passed, 3 warnings
```

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_session_workspace_snapshot.py tests/services/test_session_command_runtime.py tests/services/test_web_chat_runtime.py::test_start_web_chat_run_creates_runtime_task_and_user_message tests/services/test_web_chat_runtime.py::test_start_web_chat_run_queues_user_message_when_run_is_active -q
# 30 passed, 3 warnings
```

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_conversation_branch_service.py -q
# 5 passed
```

```bash
cd frontend && npm run test -- \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/api/domains/ccParity.test.ts \
  src/pages/agent-detail/sessionCommandResult.test.ts
# 4 files passed, 116 tests passed
```

```bash
cd frontend && npm run build
# tsc && vite build completed successfully
```

```bash
cd backend && source .venv/bin/activate && pytest tests/api/test_cc_codex_parity_api.py::test_commands_api_lists_compact_index_and_schema tests/api/test_cc_codex_parity_api.py::test_commands_api_schema_endpoint_uses_user_visible_names tests/api/test_cc_codex_parity_api.py::test_commands_api_rejects_internal_tool_commands_from_web tests/api/test_cc_codex_parity_api.py::test_commands_api_allows_internal_tool_commands_from_agent_origin -q
# 4 passed, 3 warnings

cd frontend && npm test -- --run src/pages/agent-detail/slashCommand.test.ts src/pages/agent-detail/CommandPalette.test.tsx src/pages/agent-detail/SlashCommandMenu.test.tsx src/api/domains/ccParity.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx
# 5 files passed, 99 tests passed
```

## 0. 本轮前裁决（历史审计基线）

本轮前 Hive 的 session command 有四个最高优先级错位：

1. `/compact` 只是追加了一个 `session_compact_command` 事件并触发 hook，没有真正压缩并替换当前上下文。
2. `/rewind` 当前调用 `create_conversation_branch(... mode="rewind")`，实际创建了新 `ChatSession`，这把 rewind 和 branch 混在了一起。
3. 前端把所有 command result 的 object 统一 `JSON.stringify` 成 assistant 消息，导致 `/compact`、`/rewind` 这种控制面对象裸露在聊天里。
4. `/branch` 的 session identity 没有被明确成“新 session id + 同一 session family”，导致 UI 和后端容易把 branch 当成同一个 `ChatSession.id` 下的剪枝。

正确方向：

```text
CC / FreeCode 决定语义：
  compact = 压缩并替换当前上下文
  clear   = 清空当前工作上下文并开始新 session identity
  rewind  = 回溯到 checkpoint，不等于 branch
  branch  = 复制当前 transcript 到一个新 session

Codex 作为工程参考：
  compact task / replace_compacted_history
  clear UI event
  fork current session（Codex 术语，仅作工程参考；Hive 用户命令统一叫 branch）
  thread rollback marker + replay + recompute token usage

Hive 落地方式：
  T0 仍 append-only，不破坏 raw evidence；
  active projection / session head 可以变化；
  UI 必须把 command 当 control event，不当 assistant reply。
```

核心 ID 裁决：

```text
ChatSession.id 代表一条具体可 resume 的执行线。
root_session_id 代表同一 session family / branch family。
parent_session_id + branch metadata 表示 branch lineage。

所以：
  rewind     不创建新 ChatSession.id，只改变当前 session 的 active head/projection；
  branch    创建新 ChatSession.id，但挂在同一个 root_session_id 下；
  clear      创建新 ChatSession.id，旧 session 是 parent；
  compact    不创建新 ChatSession.id，只替换当前 session 的有效上下文窗口。
```

## 0.5. 当前命令面总览

这次复核以后，不能只盯 `/compact`、`/clear`、`/rewind`、`/branch`。CC 的 slash command 分三类，Hive 也应该按这三类实现，而不是统一把后端结果 `JSON.stringify` 到聊天里。

### 0.5.1 CC / FreeCode 命令类型

| 类型 | CC 代表命令 | 语义 | Hive 落地要求 |
| --- | --- | --- | --- |
| Session 状态改变 | `/compact`、`/clear`、`/rewind`、`/branch`、`/resume`、`/rename`、`/tag`、`/permissions`、`/model`、`/plan`、`/sandbox` | 改变当前 session head、上下文窗口、会话身份、会话设置或模式 | 后端返回 typed control result；前端执行 UI action，不把 JSON 当 assistant message |
| Session 只读查询 | `/context`、`/usage`、`/status`、`/stats`、`/cost`、`/diff`、`/copy`、`/export`、`/skills`、`/agents`、`/mcp`、`/tasks`、`/hooks`、`/files` | 查询当前会话、上下文、成本、文件、技能、工具和诊断信息 | 前端打开面板、复制剪贴板、下载文件或展示紧凑卡片 |
| Prompt / capability 包装 | Skill command、Plugin command、Workflow command、`/task`、`/goal`、`/schedule`、`/once`、`/team`、`/agent` | 把用户自然语言变成一个 agent turn 或受控 runtime request | 用户只看到少量包装命令；内部 `*_create`、`*_update`、`*_delete` 不进入用户菜单 |

### 0.5.2 Hive 当前用户可见命令

当前 `surface=user` 真实返回 18 个命令：

| 用户命令 | canonical | 类型 | 当前状态 | 需要改什么 |
| --- | --- | --- | --- | --- |
| `/plan` | `plan` | prompt / mode | 已暴露 | 保持底部开关；slash 只作为进入 Plan Mode 的替代入口 |
| `/goal` | `goal_start` | prompt / runtime | 已暴露 | 保持包装命令；不要暴露 `goal_update`、`goal_stop` 给普通菜单 |
| `/task` | `task_create` | prompt / runtime | 已暴露 | 保持包装命令；内部 task 子命令只给 model/runtime |
| `/schedule` | `schedule_create` | prompt / runtime | 已暴露 | 保持包装命令；自然语言创建定时任务，不强绑 Plan Mode |
| `/once` | `schedule_once` | prompt / runtime | 已暴露 | 保持包装命令；一次性任务和 goal 不是同一概念 |
| `/team` | `team_create` | runtime | 已暴露 | 保持单入口；不要暴露 `team_create` / `team_delete` |
| `/skill` | `skill` | product / prompt | 已暴露 | 无参数打开 Skill catalog；有参数进入 Skill-guided turn |
| `/agent` | `agent` | product / prompt | 已暴露 | 无参数打开 Sub-Agent catalog；有参数委派或生成委派 turn |
| `/workflow` | `workflow` | product / workflow | 已暴露 | 只进入 Dynamic Workflow，不走 A2A fixed workflow |
| `/mcp` | `mcp` | tool / catalog | 已暴露 | 做成 MCP catalog / call wrapper 面板 |
| `/permissions` | `permissions` | session setting | 已暴露 | 打开 session-local 权限模式菜单，不走企业后台审批 |
| `/context` | `context` | diagnostic | 已暴露 | 打开 context 面板，不渲染 JSON |
| `/usage` | `usage` | diagnostic | 已暴露 | 打开 usage/cost 面板，不渲染 JSON |
| `/resume` | `resume` | session control | 已暴露 | 已打开 session 内 resume status panel；不渲染 raw JSON |
| `/rewind` | `rewind` | session control | 已暴露 | 已打开 checkpoint selector；conversation/both 更新 active projection，workspace/both 走 snapshot restore confirmation gate |
| `/branch` | `branch` | session control | 已暴露 | 保留用户命令名 `/branch`；存储层创建新 `ChatSession.id` |
| `/clear` | `clear` | session control | 已暴露 | 创建干净 session 后前端切换到新 session |
| `/compact` | `compact` | session control | 已暴露 | 执行真实 compact pipeline 并安装 compacted active projection |

### 0.5.3 Hive 后端已有但用户不可见的 session 命令

这些不是 Web 用户命令。它们已裁决为 internal/agent-origin/API-only 或其它产品 UI 的后台能力；不能通过 Web composer 手写 slash 绕过 user-visible command surface：

| 后端命令 | 当前 `visible_to_user` | 当前语义 | 裁决 |
| --- | --- | --- | --- |
| `/btw` | false | 创建 durable `side_question` branch session | internal/agent-origin only；未来若做用户 side-question drawer，必须先改 `visible_to_user` 并补 UI 测试 |
| `/checkpoints` | false | 返回 checkpoint JSON | internal data source；只作为 `/rewind` selector 数据源 |
| `/copy` | false | 返回 assistant content / code blocks | internal/API-only；Web 用户不得手写 slash，未来暴露需补 clipboard UI |
| `/export` | false | 返回 transcript / artifact JSON | internal/API-only；当前用户导出走 Workbench/session export API，不走 slash |
| `/interrupt` | false | cancel active run | internal/API-only；用户停止走现有 stop control，不走 slash |
| `/turn_steer` / `/steer` | false | 给 active turn 追加 steering message | internal/agent-origin only；用户继续输入走 composer/active turn path |
| `/rename` | false | 修改 session title | internal/API-only；不进入 slash 主菜单 |
| `/tag` | false | 写 tags metadata | internal/API-only；不进入 slash 主菜单 |
| `/rollback` | false | rewind compatibility wrapper | internal/agent-origin only；用户回溯统一走 `/rewind` |

### 0.5.4 `/branch` vs `/fork` 裁决

用户命令统一叫 `/branch`。`/fork` 不进入 Hive 用户命令面。

原因：

- CC / FreeCode 里 `branch` 的 alias 只有在另一个 feature flag 没占用 `/fork` 时才出现。
- Hive 产品语义已经叫 session branch / branch family。
- Codex 的 fork 只能作为工程参考，不应变成 Hive 的用户命令。
- 后端内部 metadata 可以保留 `mode="fork"` 作为 legacy 字段，但 API 和 UI 必须统一显示 `branch`。

### 0.5.5 UI control result 裁决

所有 command execute API 都需要返回一层前端可识别的 `ui_action`，至少包括：

| `ui_action.type` | 用途 | 代表命令 |
| --- | --- | --- |
| `switch_session` | 切换到新 session 或目标 session | `/clear`、`/branch`、`/resume` |
| `open_checkpoint_selector` | 打开 checkpoint selector | `/rewind` |
| `install_compacted_context` | 安装 compact 后的 active projection | `/compact` |
| `open_side_question` | 打开 side-question drawer/popover | `/btw` |
| `copy_to_clipboard` | 复制 assistant message 或 code block | `/copy` |
| `open_export_panel` | 打开导出/下载面板 | `/export` |
| `open_context_panel` | 打开上下文详情 | `/context` |
| `open_usage_panel` | 打开 usage / cost 详情 | `/usage` |
| `open_permissions_menu` | 打开三档 session-local 权限菜单 | `/permissions` |
| `open_resume_picker` | 打开 resume status / interrupted-turn 修复面板 | `/resume` |
| `toast` | 只需要轻提示的完成态 | `/rename`、`/tag` |

前端可以提供 debug 展开区，但默认不能把 backend result 原样渲染成 assistant message。

## 1. Baseline 源码结论

### 1.1 CC / FreeCode：`/compact`

源码：

- `/Users/example-owner/vc-saas/free-code-main/src/commands/compact/compact.ts`
- `/Users/example-owner/vc-saas/free-code-main/src/utils/processUserInput/processSlashCommand.tsx`

结论：

- `/compact` 会读取 compact boundary 后的 messages。
- 先尝试 session memory compaction；否则走 reactive compact 或 traditional compact。
- compaction 成功后返回 `type: "compact"` 和 `compactionResult`。
- `processSlashCommand.tsx` 收到 `type: "compact"` 后调用 `buildPostCompactMessages(...)`，用 compact 后的消息替换当前消息数组。
- 它不是“发一条 compact 完成消息”，而是改写当前 session 的有效上下文窗口。

本轮前 Hive 偏差：

- `backend/app/services/session_command_runtime.py` 的 `/compact` 只发 `PRE_COMPACTION` / `POST_COMPACTION` hook，然后追加 `session_compact_command`。
- 没有调用真实 summary compaction。
- 没有安装 replacement history / active projection。
- 没有重新计算 context token usage。

### 1.2 CC / FreeCode：`/rewind`

源码：

- `/Users/example-owner/vc-saas/free-code-main/src/commands/rewind/rewind.ts`
- `/Users/example-owner/vc-saas/free-code-main/src/screens/REPL.tsx`

结论：

- `/rewind` 只调用 `context.openMessageSelector()`。
- command 返回 `{ type: "skip" }`，不会追加 assistant 消息。
- 用户在 selector 中选择某个 user message 后，会先判断文件快照是否可恢复。
- 如果有文件快照，selector 提供三个核心恢复选项：
  - `Restore code and conversation`
  - `Restore conversation`
  - `Restore code`
- 对话恢复由 `rewindConversationTo(message)` 完成：
  - 把当前消息数组截断到该 message 之前。
  - 生成新的 conversation id。
  - reset microcompact state。
  - 恢复该 message 上的 permission mode。
  - 清掉 stale prompt suggestion。
- `restoreMessageSync(message)` 会把被选中的用户输入重新放回 composer，用于修改后重提。
- 代码恢复由 `fileHistoryRewind(..., message.uuid)` 完成，基于 file history snapshot 把文件系统回到目标 user message 前的状态。
- `MessageSelector` 的文案里会说 `The conversation will be forked.`，但这是 CLI/REPL 内部 conversation id 的新一轮状态，不是 `/branch` 那种复制 transcript 到另一个 session file。

本轮前 Hive 偏差：

- `/rewind` 直接创建新 `ChatSession`。
- 返回巨大 `branch` payload。
- 前端把 payload 渲染成 assistant JSON。
- 用户没有 checkpoint selector，也看不到“当前 session 被回溯到了哪里”。
- 代码/文件快照回滚能力还没有作为 session-level primitive 建模。

### 1.3 CC / FreeCode：`/branch`

源码：

- `/Users/example-owner/vc-saas/free-code-main/src/commands/branch/branch.ts`

结论：

- branch 会复制当前 transcript 文件到新 session id。
- 保留原始 metadata、content replacement records、forkedFrom 信息。
- 成功后进入新 branch session。
- 如果是从已 resume 的旧 session branch，branch 仍然创建一个新的 session id，并把 source session 写入 lineage。

本轮前 Hive 状态：

- `create_conversation_branch(...)` 这条路本质上应该属于 `/branch`。
- 这条路不应该被 `/rewind` 复用。
- 前端可以把这些新 session 聚合成“同一个会话下的多个分支”，但存储层必须是多条 `ChatSession.id`。

### 1.4 CC / FreeCode：`/clear`

源码：

- `/Users/example-owner/vc-saas/free-code-main/src/commands/clear/conversation.ts`

结论：

- clear 执行 session end hook。
- 清空当前 messages。
- 清理 session caches、read file state、skill/nested memory state、MCP state、plan slugs。
- regenerate session id，并把旧 session 作为 parent。
- 它是 fresh context boundary，不是删除旧证据。

本轮前 Hive 状态：

- 后端 `/clear` 已经创建新 `ChatSession` 并保留 `parent_session_id`，方向接近。
- 但前端没有把这个返回当作“切换到新 session”的 action，而是显示 raw JSON。

重要边界：

- `/clear` 不是删除 session。
- 删除 session / 删除 Agent / 删除企业资产是另一套高危动作，必须强确认并受企业硬规则约束。
- `/clear` 的正确含义是“开始新上下文”，旧 T0 evidence 继续存在且可 resume/查看。

### 1.5 Codex：工程增强参考

源码：

- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/chatwidget/slash_dispatch.rs`
- `/Users/example-owner/Context Engineering/codex/codex-rs/core/src/session/handlers.rs`
- `/Users/example-owner/Context Engineering/codex/codex-rs/core/src/compact.rs`
- `/Users/example-owner/Context Engineering/codex/codex-rs/core/src/session/mod.rs`

结论：

- `SlashCommand::Clear` 发 `AppEvent::ClearUi`。
- `SlashCommand::Fork` 发 `AppEvent::ForkCurrentSession`。
- `SlashCommand::Compact` 启动 compact op。
- `ThreadRollback` 是单独 op：load persisted history、append rollback event、apply rollout reconstruction、recompute token usage。
- compact 成功后调用 `replace_compacted_history(...)`，安装 compacted replacement history。
- Codex app-server fork 也创建新 thread，并用 `forked_from_thread_id` 记录 lineage。

Codex 对 Hive 的价值是工程形态，不是覆盖 CC 语义：

- command 不应该裸露为 assistant JSON。
- compact / rollback 应该是 typed session event。
- rollback/rewind 应该有 marker、replay 和 token recompute。
- fork 是独立线程/会话操作。

## 2. Hive 应该采用的最终语义

### 2.0 Session ID / Branch Family 裁决

必须把“用户看到的同一会话”和“后端可 resume 的执行线”分开：

| 概念 | 后端字段 | 语义 |
| --- | --- | --- |
| 执行线 | `ChatSession.id` | 一条可以独立 resume / run / compact / rewind 的 session line |
| 分支家族 | `root_session_id` | UI 上属于同一个 session family 的所有 branch |
| 父子关系 | `parent_session_id` | clear、branch 的来源关系 |
| 分支元数据 | `transcript_metadata_json.branch` | anchor、branch_mode、forked_from、copied prefix |

因此：

- `/branch` 必须创建新的 `ChatSession.id`。
- `/rewind` 必须保持当前 `ChatSession.id`，只改变 active projection/head。
- `/clear` 必须创建新的 `ChatSession.id`，但它不是 branch；它是 fresh context boundary。
- `/compact` 必须保持当前 `ChatSession.id`，只安装 compacted effective context。
- UI 可以把 root family 展示成“一个 session 下面多个剪枝对话”，但 API 和 runtime 不能共用一个 `session_id`。

### 2.1 `/compact`

用户语义：

> 压缩当前 session 上下文，保留当前任务状态，继续在同一 session 内工作。

后端语义：

- 运行真实 compaction。
- 生成 compact summary / replacement history。
- 写入 T0 `session_compact` 边界事件。
- 更新 active projection / context head。
- 重新计算当前 context token usage。
- 保留 raw T0 events，不物理删除历史。

前端语义：

- 不追加 assistant JSON。
- 显示一个 session control card：

```text
上下文已压缩
保留：当前目标、最近文件、任务 ledger、权限模式
详情（折叠）
```

### 2.2 `/clear`

用户语义：

> 开始一个干净的新上下文，但保留旧 session 作为可追溯历史。

后端语义：

- 创建新 `ChatSession` 或等价的新 session identity。
- 旧 session 作为 parent。
- 不删除旧 T0。
- 新 session 继承必要的 agent、permission mode、workspace scope。

前端语义：

- 执行后立即切换到返回的新 session。
- 不显示 raw JSON。
- 可以显示轻量 toast / system divider：

```text
已开始新上下文
```

### 2.3 `/rewind`

用户语义：

> 回到某个 checkpoint。不是分叉，也不是复制新 session。

后端语义：

- 无参数时返回 checkpoint selector 数据，或者由前端直接打开 selector。
- 有 checkpoint id / num_turns 时：
  - 校验 checkpoint 是 user message turn boundary。
  - 在 T0 追加 `session_rewind` marker。
  - 更新当前 session 的 active projection head 到目标 checkpoint 之前。
  - 重新计算 context token usage。
  - 不创建新 `ChatSession`。
  - 不删除 raw events。

必须支持三种 mode：

| mode | 对话 | 文件/代码 | CC 对应 |
| --- | --- | --- | --- |
| `conversation` | 回到目标 user message 前，回填该用户输入到 composer | 不变 | `Restore conversation` |
| `workspace` | 不改变对话 | 回滚 file history snapshot | `Restore code` |
| `both` | 回到目标 user message 前，回填该用户输入到 composer | 回滚 file history snapshot | `Restore code and conversation` |

限制：

- 如果没有 workspace/file snapshot，只能执行 `conversation`；`workspace` / `both` 会返回 `not_supported`，不能假装成功。
- `workspace` / `both` 必须先有可恢复的文件快照元数据，并且必须显式确认 `confirm_workspace_restore=true` 后才会恢复文件。
- 任何涉及删除文件的回滚都要走删除强确认策略，因为它本质上会让文件回到“不存在”的快照状态。
- 对一个已经完成、后来被 resume 的旧 session 执行 `/rewind`，仍然是在该已 resume 的当前 session line 上写 `session_rewind` marker；除非用户显式 `/branch`，否则不新建 branch。
- Hive 当前 snapshot 范围只包括 agent `workspace/`，不包括 T0、memory、soul、skills、logs 或其它 governed agent state。

前端语义：

- `/rewind` 默认打开 checkpoint selector。
- 用户选择 checkpoint 后，timeline 只展示 active projection 内的内容。
- 选中的用户输入可回填 composer，便于修改后重提。
- 只显示简洁状态，不显示 payload：

```text
已回溯到 13:31 的用户消息
```

### 2.3.1 `/rewind` 与 `/branch` 的硬边界

不能再用 `create_conversation_branch(mode="rewind")` 实现 `/rewind`。

正确拆分：

```text
/rewind checkpoint=X mode=conversation
  -> 当前 session 写 session_rewind marker
  -> active_projection.head_event_id = checkpoint 之前
  -> 不创建新 session

/branch checkpoint=X
  -> 创建新 ChatSession.id
  -> 复制 prefix 到新 session
  -> 新 session.parent_session_id = source session
  -> 新 session.root_session_id = source.root_session_id or source.id
```

### 2.4 `/branch`

用户语义：

> 从当前 session 的某个位置复制出一个新 session，继续另一条线。

后端语义：

- 复用 `create_conversation_branch(...)`。
- mode 使用 `branch`，不再用 `rewind` 承载这件事。
- 返回新 session id。
- 新 session 的 `root_session_id` 必须指向原始 root。
- 新 session 的 `parent_session_id` 必须指向 source session。
- 新 session metadata 必须包含：

```json
{
  "branch_mode": "branch",
  "source_session_id": "...",
  "root_session_id": "...",
  "anchor_event_id": "...",
  "anchor_sequence": 123,
  "forked_from": {
    "session_id": "...",
    "event_id": "..."
  }
}
```

前端语义：

- 执行后切换到新 session，或显示“打开分支”按钮。
- lineage panel 展示 parent/root。
- payload 只作为 debug details，不进默认聊天正文。

## 3. 数据模型与 T0 关系

T0 继续 append-only。`compact`、`rewind`、`clear`、`branch` 都不能物理删除 raw events。

需要增加或明确以下 projection metadata：

```json
{
  "active_projection": {
    "head_event_id": "...",
    "last_control_event_id": "...",
    "projection_reason": "rewind | compact | clear",
    "created_at": "..."
  }
}
```

建议事件类型：

| event_type | 用途 |
| --- | --- |
| `session_compact` | 真实上下文压缩完成 |
| `session_rewind` | 当前 session active projection 回溯 |
| `session_workspace_rewind` | 当前 session workspace snapshot 恢复 |
| `session_rewind_with_workspace` | active projection 和 workspace snapshot 同时恢复 |
| `session_clear` | 新上下文边界 |
| `session_branch` | 新分支创建 |

这些是 session control events，不是 assistant messages。

## 4. 后端修复计划（已按 0.0 实装；保留为 traceability）

### 4.1 测试先行

先改或新增测试：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_session_command_runtime.py \
  tests/services/test_session_control_plane.py \
  -q
```

需要新增的失败测试：

1. `/compact` 必须调用真实 compaction service，并产生 `session_compact`，不能只产生 `session_compact_command`。
2. `/rewind` 不能创建新 `ChatSession`，必须返回当前 session id，并写入 active projection / rewind marker。
3. `/rewind mode=conversation` 只改变 active projection，不改变 workspace。
4. `/rewind mode=workspace` 只回滚 workspace snapshot，不改变 active projection。
5. `/rewind mode=both` 同时执行 projection rewind 和 workspace snapshot rewind。
6. `/branch` 才能创建新 `ChatSession`，且新 session 有 parent/root lineage。
7. `/clear` 返回 `action: "switch_session"` 或等价 contract，前端可消费。
8. `/btw` 不能成为 Web 用户手写 slash command；internal side-question control 不能污染主 timeline。
9. `/copy`、`/export`、`/interrupt` 不能从 Web 手写 slash 绕过 user-visible command surface。
10. `/resume` 返回 `open_resume_picker`，并进入 session command control panel。
11. 前端停止按钮共享底层 cancel active run 语义，但不暴露 `/interrupt` slash。
12. `/permissions` 返回 session-local 三档权限状态，不创建企业后台 approval。
13. `/context`、`/usage` 返回面板数据 contract，不作为聊天消息。
14. Web surface 只暴露包装命令；`task_create`、`team_create`、`goal_start` 等 canonical 内部命令不能直接出现在用户菜单里。

### 4.2 实现步骤

1. 新增 session control result 类型：

```python
{
    "ok": True,
    "command": "rewind",
    "action": "rewind_applied",
    "session_id": "...",
    "checkpoint": {...},
    "control_event": {...},
}
```

2. `/compact` 接入真实 compaction path。
   - 优先复用 `memory_service.maybe_compress_messages(...)`、`conversation_summarizer`、kernel compaction event builder。
   - 不再把 `session_compact_command` 当成功结果。

3. `/rewind` 改成 active projection update。
   - 不再调用 `create_conversation_branch(...)`。
   - append `session_rewind` marker。
   - 更新 `ChatSession.transcript_metadata_json.active_projection`。
   - 支持 `mode = conversation | workspace | both`。
   - workspace mode 接入 session workspace snapshot service；没有 snapshot 时返回明确 `not_supported`，有 snapshot 时必须显式确认后恢复。

4. `/branch` 保持 create branch，不再增加第二个用户命令别名。
   - `SESSION_COMMAND_NAMES` 保持 `branch` 作为唯一用户命令名。
   - `conversation_branch_service` 应移除或停用 `rewind` branch mode，避免后续误用。
   - 内部旧数据里的 `branch_mode=fork` 可做兼容读取，但新写入统一用 `branch`。

5. `/clear` 保持创建新 context boundary，但结果 contract 改成前端可理解的 action。

6. `/btw` 保留为 internal/agent-origin side-question control。
   - 不作为 Web 用户 slash command 暴露。
   - 不再默认创建用户可见 branch。
   - 后端可记录 background evidence，但返回给前端的是 `ui_action.type = "open_side_question"`。
   - side question 的上下文必须从 compact boundary 后的当前 active projection 取，不从全量 raw T0 取。

7. `/context`、`/usage`、`/permissions` 统一改成 typed read/control result；`/copy`、`/export` 保留为 internal/API-only 或 Workbench 原生入口。
   - 这些路径不应该生成 assistant message。
   - `debug_payload` 可以保留，但默认 UI 不展示。
   - `permissions` 必须是 session-local 模式查询/切换，和企业后台 approval 分离。

8. `/interrupt` 保留 internal/API-only，并与现有停止按钮共享底层 cancel active run 语义。
   - Web 用户点击 stop，不通过手写 slash `/interrupt`。
   - 结果只展示“已中断当前 turn”，不显示 `RuntimeTask` 原始对象。

## 5. 前端修复计划（已按 0.0 实装；保留为 traceability）

### 5.1 测试先行

```bash
cd frontend && npm test -- --run \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/api/domains/ccParity.test.ts
```

需要新增的失败测试：

1. `formatSlashCommandResult` 不再对 session control command 默认渲染 JSON。
2. `/compact` 渲染 control card / compaction event，不渲染 assistant JSON。
3. `/clear` 收到新 session 后自动切换。
4. `/rewind` 无参数时打开 checkpoint selector；带 checkpoint 时刷新 active projection。
5. `/branch` 才切换到新 branch session。
6. hidden/internal commands 不能被 Web 手写 slash parser 接受。
7. `/resume` 打开 session 内 resume status panel，不退化为 toast-only。
8. `/export` 用户路径走 Workbench/session export API，不走 Web slash。
9. `/context`、`/usage` 打开右侧/弹出面板。
10. `/permissions` 打开底部三档权限菜单，并和现有底部权限按钮共用状态。
11. `/interrupt` 和停止按钮共用 UI 状态，完成后清掉 running state。
12. slash 菜单里只出现用户包装命令；内部 canonical command 不出现。

### 5.2 实现步骤

1. 把 command result 分为三类：

```ts
type CommandUiAction =
  | { action: 'assistant_message'; content: string }
  | {
      action: 'session_control'
      kind:
        | 'compact'
        | 'rewind'
        | 'clear'
        | 'branch'
        | 'context'
        | 'usage'
        | 'permissions'
        | 'resume'
      payload: unknown
    }
  | { action: 'switch_session'; sessionId: string }
  | { action: 'open_panel'; panel: 'checkpoints' | 'resume' | 'context' | 'usage' | 'permissions' }
```

2. 停止把 object result 直接 `JSON.stringify` 到 assistant message。

3. 新增 checkpoint selector：
   - 数据来自 `/checkpoints`。
   - 默认展示 user message snippet、时间、turn index。
   - 选择后调用 `/rewind` with checkpoint id。

4. Runtime timeline 过滤 active projection：
   - raw T0 仍保留。
   - 默认 UI 只显示 active projection 内消息。
   - debug inspector 可看到 rewind/compact markers。

5. 新增 command dispatcher，不再由 `CommandPalette` 自己 `JSON.stringify(response.result)`。
   - slash 输入、加号菜单、停止按钮、底部权限按钮都走同一个 dispatcher。
   - dispatcher 只处理 typed `ui_action`。
   - 未识别 payload 进入 debug fallback，不进入默认聊天正文。

6. 补齐用户触发 UI。
   - `/resume`：resume status panel。
   - `/rewind`：checkpoint selector。
   - `/branch`：branch 创建后切换 / 打开按钮。
   - `/context`、`/usage`：信息面板。
   - `/permissions`：底部三档权限菜单。
   - hidden/internal commands 不进入 Web slash；若未来暴露，必须先改 `visible_to_user` 并补 UI 测试。

## 6. UI 方向

用户看到的不是 command JSON，而是 control state：

```text
/compact
  -> 上下文已压缩

/rewind
  -> 打开 checkpoint selector
  -> 选择：恢复对话 / 恢复代码 / 恢复对话和代码
  -> 已回溯到某条消息

/branch
  -> 已创建分支
  -> 打开新分支

/clear
  -> 已开始新上下文
```

默认 UI 不展示：

- `copied_event_ids`
- `projection_path`
- `truth_path`
- `event_hash`
- hook event raw list
- branch metadata raw JSON

这些进入 debug details。

## 7. 验收标准

修复完成后必须满足：

1. `/compact` 后 context usage 下降或 active compact summary 进入当前上下文。
2. `/compact` 不再输出 `Command compact completed` + JSON。
3. `/rewind` 不创建新 `ChatSession`。
4. `/rewind` 后当前 timeline 回到指定 checkpoint。
5. `/rewind mode=workspace|both` 对新 checkpoint 可恢复 workspace snapshot；旧 checkpoint 缺 snapshot 时 fail-closed `not_supported`。
6. `/branch` 才创建新 session。
7. `/clear` 切到新上下文，旧 session 仍可从历史打开。
8. 所有 command control raw payload 默认折叠，不进入 assistant 正文。
9. T0 raw evidence append-only，不做破坏性删除。
10. 同一 `root_session_id` 下的 branch 可以被 UI 聚合展示；每条 branch 仍有独立 `ChatSession.id`。

## 8. 历史执行顺序

必须按这个顺序做：

1. 后端测试：锁定 `/compact`、`/rewind`、`/branch`、`/clear` 语义。
2. 后端实现：session command runtime 分离 control action。
3. 后端实现 workspace/file snapshot primitive；新 checkpoint 支持 workspace restore，旧 checkpoint 缺 snapshot 时明确 `not_supported`。
4. 前端测试：锁定 UI 不裸露 JSON、session 切换和 selector。
5. 前端实现：command result dispatcher + checkpoint selector。
6. 端到端验证：手动跑 `/compact`、`/rewind` 三模式、`/branch`、`/clear`。
7. 再回头看 A2A：A2A 必须建立在正确 session control 语义之上。

一句话：先修 session control command 的 contract，再谈 A2A 和更高级的 UI。否则会继续出现“看似 A2A 坏了，实际 session runtime/command 先坏了”的反复。
