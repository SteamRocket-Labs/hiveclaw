# Agent Team / Session Workbench 根因复核与完整修复方案

日期：2026-07-02

状态：根因复核与修复 truth surface。本文只做诊断和方案，不替代后续代码实现。后续实现必须以本文作为验收口径，一次完整交付，不以 Workspace 隔离作为替代修复。

## 0. 结论

当前问题不是单一 UI bug，也不是“共享 workspace 必然导致污染”。根因是四层 contract 没有同时闭合：

1. **用户显式要求 Agent Team 时，运行时没有强制进入 Agent Team contract。** 当前后端已经有 `team_create` 容器和 `spawn_subagent(team_name + name)` teammate 分支，但这只是工具描述和可选路径。生产会话实际走了普通 `spawn_subagent`，没有创建 `AgentTeam` row，因此 UI 只能看到 Sub-agent completion wake。
2. **Plan Mode 被当成执行模式入口，却没有稳定携带 execution contract。** Plan Mode 应只是意图确认和风险边界；确认后如果目标是 Agent Team，必须 handoff 到 Agent Team runtime。当前 `exit_plan_mode` 的 contract 传递过弱，且 explicit Agent Team intent 没有成为硬约束。
3. **前端把 Agent Team / Team member 事件降级渲染成 Sub-agent。** `chatDisclosureReducer` 当前把 `agent_team`、`team_member` notification source 归成 `subagent`；右侧 panel 文案也把二者合并为 “Agent Team / Sub-agent”。这直接制造了概念混淆。
4. **Workspace 文件没有 current-session provenance 分层，artifact 写入也不是幂等。** CC/Codex 可以共享 workspace，因为它们的 transcript/thread/task/turn 边界清楚。Hive 当前的右栏和 artifact delivery 对“当前 session 产物、历史文件、其他话题文件”没有产品级分层，并且同一 run 的 tool result 与 final assistant 都可能插入同一 artifact row，生产已出现唯一约束冲突导致父 run 失败。

因此修复方向不是默认隔离 workspace，而是把 **Plan Mode、Workflow、Sub-agent、Agent Team、Artifact provenance** 五条线同时收口。

## 1. 文档要求与当前实现不符

### 1.1 既有文档的明确要求

`docs/frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md` 已经写明：

- 一个 session 对应一条 timeline。
- 一个 assistant turn 对应一个连续 active run cell。
- thinking、tool call、hook、permission、AskUserQuestion、Plan、Work Ledger、compaction、checkpoint、final answer 必须在同一条 thread 语法下表达。
- Team 是可进入的成员会话，不是高级版 Sub-agent 卡片。

`docs/ccplus-session-tui-unified-expression-plan-2026-06-28.md` 又进一步要求：

- 右侧是 Workspace Documents + Runtime Tables。
- Runtime Tables 至少区分 Agents、Tasks、Background、Workflow、Notifications、Commands、Runs、Governance、Raw。
- Agents tab 应能列出 Agent Team member、Sub-agent、Background Agent sessions，并支持切换中间 session window。

当前前端没有达到这些要求。它只有一个合并的 “Agent Team / Sub-agent” 卡片，且运行事件 reducer 将 Team member 归为 Sub-agent。

### 1.2 当前前端事实

源码入口：

- `frontend/src/pages/agent-detail/chatDisclosureReducer.ts`
- `frontend/src/pages/agent-detail/AgentChatSection.tsx`
- `frontend/src/pages/session-workbench/SessionNativeControls.tsx`

已核实问题：

1. `chatDisclosureReducer.ts` 的 `RunStepKind` 没有 `agent_team` 或 `team_member` 独立类型。
2. `kindForEventMessage()` 中，`source.includes('agent_team') || source.includes('team_member')` 会返回 `subagent`。
3. `agent_task_notification` 同样把 `agent_team` / `team_member` 映射成 `subagent`。
4. `SessionRuntimePanel` 右栏标题是 `Agent Team / Sub-agent`，并且只要 `teams.length === 0` 就显示 “No running agent sessions”，普通 subagent completion wake 又在下面作为通知出现。
5. `collectWorkspaceDocuments(messages)` 只从当前可见消息收集 artifact part，不区分 current session artifact、历史 artifact、显式引用文件、其他话题文件。

这解释了截图里的表现：用户要求 Agent Team，但 UI 只看到 Sub-agent 风格的步骤和 completion notification。

## 2. CC / FreeCode 与 Codex 基线核验

### 2.1 CC / FreeCode

本机基线：`/Users/rocky243/vc-saas/free-code-main`

关键源码：

- `src/Task.ts`
- `src/tasks/InProcessTeammateTask/types.ts`
- `src/tools/AgentTool/AgentTool.tsx`
- `src/tools/shared/spawnMultiAgent.ts`
- `src/utils/teammate.ts`

结论：

1. CC/FreeCode 明确区分 task type：
   - `local_agent`
   - `remote_agent`
   - `in_process_teammate`
   - `local_workflow`
2. `in_process_teammate` 有独立 task id 前缀 `t`，不是 local agent/subagent 的别名。
3. `TeammateIdentity` 包含 `agentId`、`agentName`、`teamName`、`planModeRequired`、`parentSessionId`。
4. `InProcessTeammateTaskState` 有自己的 `messages`、`pendingUserMessages`、`isIdle`、`shutdownRequested`、`awaitingPlanApproval`、`permissionMode`。
5. `AgentTool` 的 teammate 分支很明确：当 `teamName && name` 成立时调用 `spawnTeammate()`，返回 `teammate_spawned`；否则才进入普通 local agent/subagent 路径。
6. `isolation/worktree` 是可选执行隔离，不是 Team/Subagent 语义边界。

这说明 CC 的架构核心不是“隔离目录”，而是 **teammate identity 和 task lifecycle 是独立类型**。

### 2.2 Codex

本机基线：`/Users/rocky243/Context Engineering/codex/codex-rs`

关键源码：

- `app-server-protocol/schema/typescript/v2/CollabAgentTool.ts`
- `app-server-protocol/schema/typescript/v2/ThreadForkResponse.ts`
- `app-server-protocol/schema/typescript/v2/ThreadRollbackResponse.ts`
- `app-server-protocol/schema/typescript/v2/PermissionsRequestApprovalParams.ts`

结论：

1. `CollabAgentTool` 明确是 `"spawnAgent" | "sendInput" | "resumeAgent" | "wait" | "closeAgent"`。
2. 协作 agent 是可继续、可 resume、可关闭的 lifecycle，不只是一次性任务完成通知。
3. Thread fork/rollback/permission approval 都携带 thread/turn/item/environment/cwd 等身份边界。
4. Codex 可以面对共享 workspace，但 runtime state 不靠 workspace 目录判断当前上下文，而靠 thread/turn/tool item identity。

Hive 应吸收的是这个原则：**workspace 可以共享，但 transcript/run/artifact/permission 必须按 session/turn/task 明确归属**。

## 3. Hive 后端当前事实

### 3.1 Agent Team runtime 已有正确基础

源码入口：

- `backend/app/services/agent_team_runtime_service.py`
- `backend/app/tools/handlers/command_parity.py`
- `backend/app/tools/handlers/subagent.py`
- `backend/app/services/plan_mode_agent_team_handoff.py`
- `backend/app/services/session_control_plane.py`

当前正确点：

1. `agent_team_runtime_service.py` 的模块 docstring 已说明：
   - `team_create` 创建 session-local team container。
   - `spawn_subagent(team_name + name)` 创建 addressable teammate child sessions。
   - Agent Team 与 A2A employee delegation 分开。
2. `create_agent_team_runtime_result()` 当前禁止 inline members，错误信息明确：`TeamCreate creates the Team container only; spawn teammates with spawn_subagent team_name + name`。
3. `_build_team_member_records()` 会创建 `ChatSession(session_kind="team_member", source_channel="agent_team", runtime_source="team_member", visibility_scope="team", listed_surface="chat")`。
4. `spawn_subagent` 工具 schema 包含 `team_name` 与 `name`，并注明两者同时出现时走 Agent Team teammate branch。
5. `plan_mode_agent_team_handoff.py` 已经能在 `execution_contract.type='agent_team'` 时创建 Team container 并 spawn members。

### 3.2 关键缺口

1. **显式 Agent Team intent 没有硬约束。**
   用户说“使用 Agent Team”，模型仍可以直接调用普通 `spawn_subagent`。工具层没有拦截“当前请求已承诺 Agent Team，但现在调用普通 Sub-agent”的情况。

2. **Plan Mode contract 不是强制闭环。**
   `plan_mode_agent_team_handoff.py` 需要 `execution_contract.type='agent_team'`，但 `exit_plan_mode` 只有在 args 或 metadata 中成功取到 `execution_contract` 才会落到 plan_json。当前 intent 可以被问答和重复 plan 消耗掉，最后没有进入 Agent Team handoff。

3. **UI read model 没有把 Team/Subagent/Workflow 分栏。**
   `session_control_plane.py` 能返回 `teams`、`completion_wakes`、`runtime_tasks`、`session_graph`，但前端把这些塞进一个 Collaboration section，造成用户理解上的同一类对象。

4. **普通 Sub-agent 和 Team member 都通过 `spawn_subagent` 暴露，模型容易误选。**
   为兼容 CC AgentTool，这个设计可以保留，但必须加 intent guard 和 UI typed projection，否则“工具同名”会继续造成产品语义混淆。

## 4. 生产证据

本轮已拉取 Railway production backend 日志和 health。

命令形态：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main
railway deployment list --service backend --environment production --limit 3 --json
curl -fsS https://backend-production-326d.up.railway.app/api/health
railway logs --service backend --environment production --lines 5000
```

已核实事实：

1. backend production 最新部署为 `SUCCESS`，health 返回 ok。
2. 目标会话中用户请求是“使用 Agent Team 给我一份详细的 ABS 深度报告”。
3. runtime summary 显示实际使用了 `request_plan_mode`、`ask_user_question`、`exit_plan_mode`、`spawn_subagent`、`track_todo`、`write_file`。
4. 没有观察到 `team_create`。
5. session workbench 中 `teams=[]`，但有多个 subagent/runtime task completion wake。
6. 生产日志出现父 run 失败：
   - 时间：2026-07-01T21:01:43Z
   - run：`8c8d7c309d8c4454831739654de8bfa3`
   - 异常：`asyncpg.exceptions.UniqueViolationError`
   - 约束：`uq_chat_artifacts_agent_session_run_path_snapshot`
   - 调用栈：`web_chat_runtime.py` final assistant persistence -> `append_session_event()` -> `db.flush()`
7. 同一失败 payload 中出现 ABS 文件和明显无关的 `workspace/minggushizhai_zhai_gutongquan_product.md`，说明 artifact manifest/currentness 也存在污染。

这个生产证据同时解释两件事：

- 用户请求 Agent Team，但 runtime 没创建 Agent Team。
- `[LLMError]` 不一定是模型能力问题，至少有一次明确是 artifact DB 写入冲突导致 run 失败。

## 5. 四个概念的边界

### 5.1 Plan Mode

Plan Mode 是确认边界，不是执行 substrate。

职责：

- 判断意图、成本、风险和是否需要用户确认。
- 生成用户可读计划。
- 记录 `execution_contract`。
- 等待 approve / revise / reject。

非职责：

- 不直接表示 Agent Team。
- 不直接表示 Workflow。
- 不把“多 agent”统一降级成 Sub-agent。

验收口径：

- 用户说 Agent Team 时，Plan Mode 可以先要求确认，但确认后的 handoff target 必须是 `agent_team`。
- 如果用户拒绝 Plan Mode 或继续直接执行，explicit Agent Team intent 仍必须约束后续工具选择。

### 5.2 Workflow + Sub-agent

Workflow 是 deterministic orchestration substrate。

职责：

- 固定步骤、gate、wait/resume、journal、quota。
- 每个 step 可以是 tool、LLM、subagent leaf、approval gate。
- 适合结构化流程和可重放流程。

Sub-agent 在 Workflow 中只是 leaf execution pattern，不等于 Agent Team。

### 5.3 Sub-agent

Sub-agent 是一次性或后台 worker。

职责：

- 承接单个任务片段。
- 返回 digest、artifact、completion wake。
- 可以有 child session 用于审计和结果恢复。

非职责：

- 不默认是可进入、可长期对话的 teammate。
- 不应该在 UI 上冒充 Agent Team。

### 5.4 Agent Team

Agent Team 是 session-local collaboration container。

职责：

- `team_create` 创建 Team container。
- `spawn_subagent(team_name + name)` 创建 addressable teammate。
- 每个 member 有独立 `team_member` ChatSession。
- 用户可以 enter member session、send follow-up、resume、close、consolidate。

非职责：

- 不等于 Workflow。
- 不等于普通 Sub-agent fanout。
- 不应只作为右侧 completion notification。

## 6. 污染根因：不是 workspace 隔离，而是 provenance 缺失

CC/Codex 也允许多个 session 面对同一个 cwd/workspace。它们不污染的关键是：

1. 当前 thread/session 是 prompt 的主上下文边界。
2. tool call、permission、artifact、approval 都绑定 turn/task/item id。
3. workspace 文件可以被读取，但不会自动成为当前 session 的交付物。
4. 历史文件只有在被当前 transcript 明确引用或当前 tool call 写入时，才提升为当前上下文。

Hive 当前存在的偏差：

1. workspace 文件是共享的，但 UI 右栏没有 current/historical/foreign 分类。
2. tool 可以 `list_files` 后把任何旧文件当成当前任务素材。
3. artifact part 只按消息可见性收集，没有明确 provenance。
4. `SessionContext.current_turn_writes` 是 per session 的，但它只能防止内存上下文串线，不能解决模型读取历史文件和 artifact manifest 误绑定的问题。
5. `create_chat_artifacts_for_message()` 只做入参数组内去重，没有对已存在 artifact row 幂等处理。
6. tool result 和 final assistant 都会调用 artifact creation，导致同一 run/path/snapshot 重复插入，生产已经触发唯一约束冲突。

因此正确修复是 **shared workspace + strict provenance + idempotent artifact delivery + current-session UI grouping**。

## 7. 完整修复方案

下面是一次完整修复 pass 的执行顺序，不是分期交付。每一组都需要代码、测试和验收证据同时闭合。

### 7.1 Agent Team intent hard binding

目标：只要用户明确要求 Agent Team，runtime 不允许静默降级成普通 Sub-agent fanout。

代码落点：

- `backend/app/tools/handlers/subagent.py`
- `backend/app/tools/handlers/command_parity.py`
- `backend/app/services/web_chat_runtime.py`
- `backend/app/services/plan_mode_agent_team_handoff.py`
- `backend/app/tools/handlers/plan_mode.py`

实现要求：

1. 在当前 session runtime metadata 中记录 `requested_execution_contract`：

```json
{
  "type": "agent_team",
  "source": "explicit_user_intent",
  "requested_at_turn_id": "...",
  "team_name": "...",
  "members": []
}
```

2. 当存在 `requested_execution_contract.type='agent_team'` 时，普通 `spawn_subagent` 调用如果没有 `team_name + name`，返回结构化错误：

```json
{
  "status": "error",
  "error_code": "agent_team_contract_required",
  "message": "This session is bound to Agent Team execution. Call team_create first, then spawn teammates with spawn_subagent({team_name, name, prompt}).",
  "repair": {
    "first_tool": "team_create",
    "then_tool": "spawn_subagent",
    "required_args": ["team_name", "name", "prompt"]
  }
}
```

3. `team_create` 成功后把 `agent_team_id`、`team_name` 写回 session runtime metadata。
4. `spawn_subagent(team_name + name)` 必须只绑定当前 session 中已存在且 active 的 team container。没有 team 时 fail closed。
5. 当用户明确要求 Team 编制或“完整 N 人团队”时，Plan Mode 里的 `execution_contract` 必须包含 `type='agent_team'`。

### 7.2 Plan Mode execution contract 修复

目标：Plan Mode 只做确认，不吞掉执行 contract。

代码落点：

- `backend/app/tools/handlers/plan_mode.py`
- `backend/app/services/plan_mode_session_handoff.py`
- `backend/app/services/plan_mode_agent_team_handoff.py`
- `backend/tests/tools/test_exit_plan_mode_tool.py`
- `backend/tests/services/test_plan_mode_agent_team_handoff.py`

实现要求：

1. `exit_plan_mode._execution_contract()` 必须同时读取：
   - tool args `execution_contract`
   - Plan Mode metadata `execution_contract`
   - session runtime metadata `requested_execution_contract`
2. `_handoff()` target 和 `execution_contract.type` 必须一致。
3. 如果 `execution_contract.type='agent_team'`，确认后必须进入 `agent_team_handoff`，不能继续普通 `web_chat_turn` fanout。
4. 如果用户选择“不需要 Plan Mode/暂不需要”，explicit Agent Team intent 仍保留在 session metadata 中，后续工具选择仍受 7.1 约束。

### 7.3 Backend read model 分栏

目标：API 返回 typed runtime sections，前端不再自己猜 Team/Subagent/Workflow。

代码落点：

- `backend/app/services/session_control_plane.py`
- `backend/app/api/chat_sessions.py`
- `backend/tests/services/test_session_control_plane.py`

建议返回结构：

```json
{
  "runtime_sections": {
    "agent_teams": [
      {
        "team_id": "...",
        "name": "...",
        "status": "...",
        "members": [
          {
            "member_id": "...",
            "member_name": "...",
            "chat_session_id": "...",
            "runtime_task_id": "...",
            "status": "...",
            "enterable": true
          }
        ]
      }
    ],
    "subagents": [],
    "workflows": [],
    "background": [],
    "notifications": [],
    "raw": []
  }
}
```

兼容期可以保留 `teams`、`completion_wakes`、`runtime_tasks`，但前端必须优先使用 `runtime_sections`。

### 7.4 Frontend taxonomy 与 TUI 修复

目标：用户在 UI 上能清楚看到当前是 Plan、Workflow、Sub-agent 还是 Agent Team。

代码落点：

- `frontend/src/pages/agent-detail/chatDisclosureReducer.ts`
- `frontend/src/pages/agent-detail/RunDisclosureBlock.tsx`
- `frontend/src/pages/agent-detail/AgentChatSection.tsx`
- `frontend/src/pages/session-workbench/timelineModel.ts`
- `frontend/src/pages/session-workbench/SessionNativeControls.tsx`
- `frontend/src/i18n/en.json`
- `frontend/src/i18n/zh.json`

实现要求：

1. `RunStepKind` 增加 `agent_team`、`team_member`。
2. `kindForEventMessage()` 不得把 `agent_team`、`team_member` 映射成 `subagent`。
3. 右栏 Runtime Tables 至少拆成：
   - Agent Team
   - Sub-agents
   - Workflow
   - Background
   - Notifications
   - Raw
4. 不再使用 “Agent Team / Sub-agent” 合并标题。
5. Agent Team member row 必须有 Enter / Send follow-up / Resume / Close / Consolidate 的入口。第一轮代码可以把部分按钮置 disabled，但必须显示生命周期边界和状态原因。
6. 中间 timeline 的 active run cell 必须显示：
   - Thinking
   - tool call
   - tool result
   - Plan / AskUserQuestion
   - Work Ledger todo/finding
   - artifact delivery
   - final answer
7. 点击 Team member 后，中间 session window 切到该 `chat_session_id`，composer footer 显示当前 active session label。

### 7.5 Artifact delivery 幂等与 provenance

目标：共享 workspace 保留，但当前 session 交付物不再被历史文件污染，artifact 写入不再打断 run。

代码落点：

- `backend/app/services/chat_artifact_delivery.py`
- `backend/app/services/web_chat_runtime.py`
- `backend/app/kernel/engine.py`
- `backend/app/runtime/session.py`
- `backend/tests/services/test_chat_artifact_delivery.py`
- `backend/tests/services/test_web_chat_runtime_artifacts.py`

实现要求：

1. `create_chat_artifacts_for_message()` 必须幂等：
   - 先查 `agent_id/session_id/runtime_task_id/path/snapshot_hash` 是否已存在。
   - 已存在则返回 existing artifact part，并在必要时绑定/补充 transcript event metadata。
   - 新建时才 insert。
   - PostgreSQL 可以使用 upsert 或 select-for-update，测试必须覆盖重复调用。
2. tool result 和 final assistant 不得重复插入同一 artifact row。
3. terminal artifact manifest 应只包含当前 turn 显式写入并且属于当前任务 deliverable 的文件。
4. artifact part 增加 provenance：

```json
{
  "scope": "current_session_artifact",
  "session_id": "...",
  "runtime_task_id": "...",
  "turn_id": "...",
  "tool_call_id": "...",
  "source": "workspace_write",
  "topic_signature": "..."
}
```

5. 右栏 Workspace Documents 至少分组：
   - Current session
   - Explicitly referenced
   - Same agent historical
   - Other / unattributed
6. 历史文件默认折叠，不自动进入当前交付物区。
7. 模型上下文中只应优先暴露 current session artifact manifest；历史 workspace 文件必须由用户引用或 agent 明确 `read_file` 后才进入当前 reasoning。

### 7.6 错误表达与可观察性

目标：生产中出现 transcript/artifact 写入错误时，用户和开发者都能看到真实错误边界。

代码落点：

- `backend/app/services/web_chat_runtime.py`
- `backend/app/services/web_chat_broker.py`
- `frontend/src/pages/agent-detail/AgentChatSection.tsx`
- `frontend/src/pages/agent-detail/RunDisclosureBlock.tsx`

实现要求：

1. `UniqueViolationError`、artifact persistence error、transcript append error 必须有专门 terminal reason，不得泛化成 `[LLMError] AI 模型调用异常`。
2. 日志必须包含：
   - `agent_id`
   - `session_id`
   - `runtime_task_id`
   - `tool_call_id`
   - `artifact_path`
   - `snapshot_hash`
   - `execution_contract.type`
   - `team_id/member_id`
3. 前端 failed run cell 显示“Artifact persistence failed”或“Transcript persistence failed”，并提供 raw detail disclosure。

## 8. 测试清单

### 8.1 Backend

新增或扩展测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest \
  tests/tools/test_exit_plan_mode_tool.py \
  tests/services/test_plan_mode_agent_team_handoff.py \
  tests/services/test_agent_team_runtime_service.py \
  tests/services/test_session_control_plane.py \
  tests/services/test_chat_artifact_delivery.py \
  tests/services/test_web_chat_runtime_artifacts.py \
  -q
```

必须覆盖：

1. `test_agent_team_intent_requires_team_create_before_plain_subagents`
2. `test_exit_plan_mode_preserves_metadata_execution_contract`
3. `test_agent_team_plan_confirmation_creates_container_and_enterable_members`
4. `test_plain_subagent_not_rendered_as_agent_team`
5. `test_session_workbench_separates_agent_team_subagent_workflow_notifications`
6. `test_chat_artifact_delivery_idempotent_for_same_run_path_snapshot`
7. `test_terminal_artifacts_exclude_historical_unreferenced_workspace_files`
8. `test_artifact_persistence_error_not_reported_as_llm_error`

### 8.2 Frontend

新增或扩展测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend

npm run test -- \
  chatDisclosureReducer.test.ts \
  AgentChatSection.test.tsx \
  SessionNativeControls.test.tsx \
  timelineModel.test.ts

npm run build
```

必须覆盖：

1. `agent_team` event 渲染为 Agent Team step。
2. `team_member` event 渲染为 Team member step。
3. 普通 `subagent` event 仍渲染为 Sub-agent step。
4. 右栏 Runtime Tables 分开显示 Agent Team、Sub-agents、Workflow、Notifications。
5. Agent Team member row 的 Enter 按钮调用 `onSelectSession(member.chat_session_id)`。
6. Workspace Documents 分组显示 current session 与 historical/unattributed。
7. active run cell 同时显示 thinking、tool call、tool result、artifact、final answer。

### 8.3 Production 验收

在后续代码实现并部署后，用以下 production smoke 验收：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main

railway deployment list --service backend --environment production --limit 1 --json
railway deployment list --service frontend --environment production --limit 1 --json

curl -fsS https://backend-production-326d.up.railway.app/api/health
curl -I -fsS https://frontend-production-0346.up.railway.app/

railway logs --service backend --environment production --lines 5000 | rg -i \
  "team_create|spawn_subagent|agent_team|team_member|UniqueViolationError|uq_chat_artifacts_agent_session_run_path_snapshot|artifact persistence|Transcript persistence"
```

验收用例：

1. 新建 session，输入：`使用 Agent Team 给我一份详细的 ABS 深度报告，Team 编制完整 7 人 + 主报告 + 分册 + 评审`。
2. 如果进入 Plan Mode，确认计划后必须看到：
   - `team_create`
   - 至少一个 `spawn_subagent` 带 `team_name + name`
   - `sessionWorkbench.teams.length > 0`
   - 每个 member 有 `chat_session_id`
   - UI 右栏 Agent Team tab 显示 member，且 Enter 可切换中间 session
3. 不允许出现：
   - explicit Agent Team request 下只有普通 Sub-agent completion wake。
   - `agent_team` / `team_member` 被显示成 Sub-agent。
   - artifact unique constraint 冲突。
   - unrelated historical workspace file 自动出现在 Current session documents。

## 9. 最小验收定义

本问题只有在以下条件同时满足时才算关闭：

1. explicit Agent Team request 的第一组协调工具必须是 `team_create` + `spawn_subagent(team_name + name)`，而不是普通 Sub-agent fanout。
2. Plan Mode 只作为确认边界，不吞掉 Agent Team execution contract。
3. Agent Team、Sub-agent、Workflow、Plan Mode 在前端有独立视觉和 read model 分类。
4. Team member 是可进入的 child session，不只是 completed notification。
5. Shared workspace 保持共享，但 Workspace Documents 明确分出 current session / historical / foreign。
6. artifact delivery 重复调用幂等，不再让父 run 因唯一约束失败。
7. transcript/artifact persistence error 不再伪装成 LLMError。
8. 后端测试、前端测试、build、production smoke 全部有证据。
