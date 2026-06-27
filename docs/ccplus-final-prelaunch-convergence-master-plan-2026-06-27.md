# CCPlus Final Prelaunch Convergence Master Plan

日期：2026-06-27
状态：上线前最后一轮优化的统领性计划
范围：Session Control、AgentTool/Sub-agent/Completion Bus、Agent Team/A2A Session、TurnEnvelope/Workbench/Hooks/Skill/MCP 的统一实施顺序、依赖关系、验收口径和文档归属

## 0. 本文定位

本文是上线前最后一轮 CCPlus 优化的总入口。

旧的专项文档仍然保留为证据和细节来源，但后续排期、实现顺序、验收优先级以本文为准。任何专项文档如果与本文冲突，先按本文执行，再回写专项文档。

核心裁决：

```text
CC / FreeCode 是语义基底。
Codex 是工程控制增量。
Hive Memory / self-evolution / enterprise control plane 是显式 Hive-native 增量。

上线前最后一轮不是继续加功能，而是把已经存在的多条路径收束成一条 session spine。
```

本轮开始前最大的风险不是没有机制，而是机制分叉：

- session command 和 UI control result 分叉。
- session worker 和 A2A employee delegation 分叉。
- subagent completion、team mailbox、A2A child completion、workflow signal 分叉。
- team tool、command API、agent-teams API、Workbench create team 分叉。
- prompt/context/tool/skill/MCP/hook 信息没有统一 manifest。

本文要做的事就是把这些分叉按四条主线收束。

## 1. 文档地图

### 1.1 北极星与边界

| 文档 | 本轮用途 |
| --- | --- |
| `docs/ccplus-north-star-contract-2026-06-24.md` | CCPlus 边界契约：CC/FreeCode 语义基底优先，Codex 只能做工程增量，Hive-native 能力必须显式。 |
| `docs/cc-python-evolution-north-star-2026-06-22.md` | Hive 作为 CC Python evolution 的更大北极星。 |
| `docs/hive-sota-master-goal.md` | Hive SOTA 总目标和原子能力地图。 |

本文不重写这些北极星，只把它们落到上线前最后一轮的执行顺序。

### 1.2 本轮 CCPlus session-middle 总审计

| 文档 | 本轮用途 | 当前裁决 |
| --- | --- | --- |
| `docs/ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md` | Runtime / Context / AgentTool / Codex delta 的审计主文档。 | 作为证据和 P0/P1 差距来源；实施顺序由本文统一。 |
| `docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md` | Subagent、Agent Team、Skill、MCP、Hooks 的子系统附录。 | 作为子系统证据；不单独形成第二套 runtime 语义。 |
| `docs/ccplus-session-runtime-token-compaction-alignment-2026-06-27.md` | Session token / compaction / context window 底座。 | 底座已实装；后续重点是通过 session command / TurnEnvelope / Workbench 暴露和消费。 |
| `docs/ccplus-session-control-command-alignment-2026-06-27.md` | `/compact`、`/clear`、`/rewind`、`/branch`、只读 command、UI action 的契约。 | 是第一条实施主线的直接依据。 |
| `docs/ccplus-session-ux-contract-2026-06-26.md` | Session Workbench UX、工具折叠、artifact inspector、权限菜单、Plan Mode 卡片。 | 作为前端和 read model 的体验验收标准。 |

### 1.3 A2A 专项文档

| 文档 | 本轮用途 | 当前裁决 |
| --- | --- | --- |
| `docs/a2a-integrated-implementation-plan-2026-06-27.md` | A2A 三层总计划：Relationship / Session / Process Graph。 | Phase 1 Relationship gate 已基本闭合；后续进入 Session-first delegation，但必须排在 session spine 和 AgentTool/Completion Bus 之后。 |
| `docs/a2a-relationship-retirement-plan-2026-06-27.md` | 删除旧 Relationship 路径，A2A read model 成为唯一 To Employee 可调用名单。 | Phase 1 当前完成；后续只做 legacy 数据清理和人类 contacts 迁移，不再作为主线入口。 |
| `docs/a2a-session-substrate-design-2026-06-24.md` | Agent-Agent child session、human read-only、continuation、runtime/session 边界。 | 作为第三条主线 A2A Session-first 的细节来源。 |
| `docs/a2a-workflow-orchestration-design-2026-06-24.md` | A2A Process Graph、handoff envelope、artifact_ref、edge gate、retry/resume。 | 暂不先做；放在本轮 convergence 之后或最后完整 slice。 |

### 1.4 Skill / MCP / Hooks / Workflow 支撑文档

| 文档 | 本轮用途 |
| --- | --- |
| `docs/agent-extension-surface-skill-mcp.md` | Skill / MCP / extension surface 背景。 |
| `docs/SKILLS_AND_PACKS_V2.md` | Skill / pack 现有设计和兼容边界。 |
| `docs/cc-tooling-alignment-and-plugin-system.md` | CC tool/plugin 对齐背景。 |
| `docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md` | Session 权限、企业硬规则、Hook 分层。 |
| `docs/dynamic-workflow-cc-alignment-redesign-2026-06-23.md` | Dynamic Workflow 与 CC alignment，不和 A2A Process Graph 混层。 |
| `docs/workflow-source-capability.md` | Workflow source capability 主文档。 |

这些文档支撑第四条主线，不抢前两条主线的顺序。

## 2. 当前已闭合与未闭合

### 2.1 已闭合或基本闭合

1. A2A Relationship 旧路径退役的第一阶段已经落地：
   - 后端 A2A 使用 same-owner、public/company-callable、active A2A group 作为唯一判定路径。
   - 旧 `relationships.py`、`relationships_file.py`、`relationships.md` prompt 注入路径已删除。
   - 新增 `/agents/{agent_id}/a2a/collaborators` read model。
   - 前端 Agent Detail 切到 A2A tab，删除 RelationshipEditor。
   - Gateway / M 端 legacy `relationships` 字段改为兼容壳，内容来自 A2A collaborators。

2. Session runtime token / compaction 底座已经完成第一轮：
   - `SessionContextController` 已接入 kernel request preflight。
   - active context tokens 与 cumulative run tokens 已区分。
   - tool-result budget pass、context status、compaction decision 进入 runtime event。
   - Workbench 有 context window read model。

3. Session UX 的若干基础体验已落地：
   - 权限菜单三档。
   - runtime disclosure summary。
   - artifact inspector。
   - Plan Mode proposal 结构。

这些是底座，不代表最终 CCPlus session behavior parity 已闭合。

### 2.2 仍未闭合的核心断点

1. Session command 仍未成为 typed control surface：
   - `/compact` 不应只是事件，应执行真实 compact 并安装 effective context。
   - `/rewind` 不应创建新 `ChatSession`，应更新当前 active projection。
   - `/branch` 才创建新 `ChatSession`。
   - 前端不能把 command result JSON 当 assistant message。

2. Session Worker 与 Employee Delegation 仍需彻底分层：
   - To Session Worker：CC-compatible AgentTool / internal spawn_subagent / session mailbox。
   - To Employee：A2A `delegate_to_agent` / `send_message_to_agent` / A2A Collaborators。
   - `delegate_to_agent` 不能再作为 coordinator 默认 worker spawn path。

3. Completion feedback 没有唯一 bus：
   - subagent wake、CoordinationSignal、T0 event、`check_subagent`、child session state、team context 都能表达完成。
   - 正常路径必须收束为 session mailbox / input queue，fallback 才允许检查工具。

4. Agent Team runtime path 本轮已收束到单一 backend/runtime service：
   - API / command / model tool / Plan Mode handoff 现在共用 `agent_team_runtime_service.py`。
   - model-visible `team_create` 已直接持久化，不再只是 handoff。
   - prompt-facing team context / Workbench projection 已在主线 D 接入，以 `AgentTeam` rows 为唯一 source of truth。

5. Turn context / prompt assembly manifest 已建立：
   - Codex 的工程优点是 typed turn/thread/config/permission/sandbox snapshot。
   - Hive 已新增统一 `TurnEnvelope` / `PromptAssemblyManifest` read model，并由 Session Workbench 暴露。

6. Hooks / Skill / MCP 已进入统一 session read model：
   - Hooks parser/registry + explicit hook state 已进入 `TurnEnvelope`；external runner 未 production wired 时显式 not-live。
   - Skill progressive disclosure 仍保留，`load_skill` tool events 已进入 `TurnEnvelope`。
   - MCP call 能力继续走 governed wrapper，MCP tools/prompts/resources 已进入 server read model 和 ExtensionRegistry affordance。

## 3. 四条主线

上线前最后一轮按四条主线执行。

### 主线 A：Session Control Spine

状态：已完成 Workstream A implementation pass。证据见 `docs/ccplus-session-control-command-alignment-2026-06-27.md` 的 “0.0 Workstream A 实装证据”。

目标：先把 session 的身份、上下文窗口、active projection、command result、UI action 收成一条稳定 spine。

依赖文档：

- `docs/ccplus-session-control-command-alignment-2026-06-27.md`
- `docs/ccplus-session-runtime-token-compaction-alignment-2026-06-27.md`
- `docs/ccplus-session-ux-contract-2026-06-26.md`

核心原则：

```text
T0 raw evidence append-only。
ChatSession.id 是一条可 resume 的执行线。
root_session_id 是 branch family。
active_projection 可以变，但不能破坏 raw transcript。
command result 是 typed control event，不是 assistant message。
```

必做项：

1. 后端新增 typed session command result：
   - `ok`
   - `command`
   - `action`
   - `session_id`
   - `ui_action`
   - `control_event`
   - `debug_payload`

2. `/compact` 接入真实 compact path：
   - 执行当前 session 的 effective context compaction。
   - 写 `session_compact` control event。
   - 更新 active compacted projection。
   - Workbench context usage 显示 compact 后状态。
   - 不再把 `session_compact_command` 当成功结果。

3. `/rewind` 改成 active projection update：
   - 不创建新 `ChatSession`。
   - 写 `session_rewind`。
   - 支持 `conversation | workspace | both`。
   - workspace snapshot 不存在时明确返回 `not_supported`，不能假装成功。

4. `/branch` 是唯一创建新 branch session 的用户命令：
   - 新 `ChatSession.id`。
   - 同一 `root_session_id`。
   - `parent_session_id` 指向 source session。
   - metadata 记录 source anchor。

5. `/clear` 创建 fresh context boundary：
   - 创建新 session。
   - 返回 `ui_action.type = "switch_session"`。
   - 旧 session 仍可恢复。

6. 只读/面板类命令返回 UI action：
   - `/context` -> `open_context_panel`
   - `/usage` -> `open_usage_panel`
   - `/permissions` -> `open_permissions_menu`
   - `/copy` -> `copy_to_clipboard`
   - `/export` -> `open_export_panel`
   - `/resume` -> `open_resume_picker` 或 `switch_session`
   - `/interrupt` -> 与 stop button 同一路径

7. 前端增加 command dispatcher：
   - slash command、加号菜单、stop button、权限菜单共用 dispatcher。
   - 默认只消费 `ui_action`。
   - raw payload 只进入 debug details，不进 assistant 正文。

验收测试：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_session_command_runtime.py \
  tests/services/test_session_control_plane.py \
  -q
```

```bash
cd frontend && npm run test -- \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/api/domains/ccParity.test.ts
```

完成标准：

- `/compact` 后 active context 确实变化，且 UI 不显示 raw JSON。
- `/rewind` 不创建新 session。
- `/branch` 才创建新 session。
- `/clear` 自动切换新 session。
- T0 append-only。
- Workbench 能解释当前 context / compact / branch / projection 状态。

### 主线 B：AgentTool / Sub-agent / Completion Bus

目标：恢复 CC 的 session worker 语义，让复杂任务、并行查询、独立验证、上下文隔离自然触发 subagent，而不是被 A2A relationship gate 劝退。

依赖文档：

- `docs/ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md`
- `docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md`
- `docs/subagent-source-capability.md`

核心分层：

```text
To Session Worker
  当前 session 内 lightweight worker / explorer / critic / team member。
  模型可见为 CC-compatible AgentTool。
  不需要 A2A Collaborators。

To Employee
  一个真实数字员工向另一个真实数字员工委派任务或发消息。
  模型可见为 delegate_to_agent / send_message_to_agent。
  必须经过 A2A Collaborators / A2A policy / Plan bridge。
```

必做项：

1. 建立 model-visible canonical AgentTool-compatible surface：
   - `description`
   - `prompt`
   - `subagent_type`
   - `model`
   - `run_in_background`
   - `name`
   - `team_name`

2. 旧字段作为兼容 alias：
   - `task` -> `prompt`
   - `type` -> `subagent_type`
   - `definition_name` -> custom definition

3. 默认类型改为 CC-compatible：
   - 省略 `subagent_type` 时映射 `general-purpose`。
   - `general-purpose` 可以先映射到当前 worker，但 prompt 和 whenToUse 必须像 CC。

4. Coordinator mode 改用 AgentTool：
   - coordinator visible tools 包含 AgentTool / spawn_subagent。
   - coordinator 不再把 `delegate_to_agent` 当 primary worker spawn。
   - `delegate_to_agent` 继续存在，但只用于 To Employee。

5. prompt affordance 对齐 CC：
   - 常驻 Session Worker section。
   - When to use。
   - When NOT to use。
   - few-shot examples。
   - available agent types + whenToUse attachment。
   - parallel fan-out 指令。

6. 引入 Codex-style multi-agent mode：
   - `none`
   - `explicit_request_only`
   - `proactive`
   - mode 进入 TurnEnvelope，而不是散落在 prompt 文案。

7. 收束 completion bus：
   - 新增或统一 `AgentInputQueue` / session mailbox。
   - child completion 写入 parent session mailbox。
   - parent idle 时触发下一轮。
   - next turn 看到 `<task-notification>` 或 Hive-neutral equivalent。
   - exactly-once drain。
   - `check_subagent` 只保留为 debug/fallback。
   - prompt 不再引用非 tool 的 `consume_subagent_signals`。

验收测试：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/tools/test_agent_tool_cc_compat.py \
  tests/agents/test_subagent_spawn_tool.py \
  tests/runtime/test_subagent_listing_section.py \
  tests/runtime/test_coordinator.py \
  tests/runtime/test_coordinator_prompt.py \
  tests/runtime/test_coordinator_force_async_acceptance.py \
  tests/runtime/test_prompt_sections.py \
  tests/tools/test_plan_mode_policy.py \
  tests/services/test_subagent_run_service.py \
  tests/services/test_subagent_wake_consumer.py \
  -q
```

完成标准：

- 单 agent 部署也能主动触发 session worker。
- 用户要求 parallel / explore / independent verification 时，模型有强触发路径。
- `delegate_to_agent` 不再承担 session worker 语义。
- background subagent 完成会自动进入 parent turn。
- completion 不需要模型轮询。

实施结果（2026-06-27 / Workstream B）：

- 已建立 model-visible AgentTool-compatible `spawn_subagent` surface：`prompt`、`description`、`subagent_type`、`model`、`run_in_background`、`name`、`team_name`；旧 `task` / `type` 只作为兼容 alias。
- `subagent_type` 默认从旧 `explorer` 改为 `general-purpose`，内部映射到现有 edit-capable worker preset；`explorer` / `worker` / `critic` 继续保留。
- 新增 `## Session Worker Types` 常驻 prompt section，直接从 built-in `whenToUse` 渲染 `general-purpose` / `explorer` / `worker` / `critic`，进入 agent context；不再只藏在 tool schema。
- Coordinator mode 已切到 To Session Worker：allowed tools 为 `spawn_subagent` / `check_subagent` / `send_agent_session_message` 等；`delegate_to_agent` / `check_async_task` / `list_async_tasks` / `cancel_async_task` 不再是 coordinator worker path。
- `executing_actions` 常驻提示词已拆成 To Session Worker 与 To Employee：session 内并行、探索、隔离、独立验证走 `spawn_subagent`；真实数字员工协作才走 A2A `delegate_to_agent` / `send_message_to_agent`。
- `delegate_to_agent` tool description 已明确为 To Employee / A2A collaboration，不是 session-local worker。
- background subagent completion wake prompt 已移除内部 `consume_subagent_signals`，改为 parent session mailbox + wake path；`check_subagent` 只保留为 fallback status inspection。
- Codex-style `multi_agent_mode` 已作为 typed TurnEnvelope / Workbench 状态承载；不得另建第二套 coordinator path。

证据：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_subagent_wake_consumer.py \
  tests/runtime/test_subagent_listing_section.py \
  tests/tools/test_agent_tool_cc_compat.py \
  tests/agents/test_subagent_spawn_tool.py \
  tests/runtime/test_coordinator.py \
  tests/runtime/test_coordinator_prompt.py \
  tests/runtime/test_coordinator_force_async_acceptance.py \
  tests/runtime/test_prompt_sections.py \
  tests/tools/test_plan_mode_policy.py \
  tests/runtime/test_t2_guidance_surface.py \
  tests/runtime/test_unified_prompt_contracts.py \
  tests/services/test_prompt_contracts.py \
  tests/services/test_tool_registry.py \
  tests/services/test_agent_tools_core_surface.py \
  tests/tools/test_service.py \
  tests/tools/test_collector.py \
  tests/services/test_subagent_run_service.py \
  tests/kernel/test_runtime_guidance_catalog.py \
  -q
# 248 passed, 4 warnings
```

### 主线 C：Agent Team / A2A Session-first Collaboration

目标：把团队协作和真实数字员工协作都收进 session-first 模型，但保持二者边界清晰。

状态：已完成 Workstream C backend/runtime implementation pass。

本次闭合点：

- `backend/app/services/agent_team_runtime_service.py` 成为 Team create/message 的唯一 runtime service。
- `team_create` model tool 不再返回 `requires_api_persist=True` handoff，而是直接创建 durable `AgentTeam`、member `ChatSession`、`AgentTeamEvent` 和 parent session `team_member` event。
- `/commands/team_create`、`/agents/{agent_id}/agent-teams`、Plan Mode `agent_team` handoff 共用同一 create service。
- `send_agent_session_message` 支持 `team_id + member_name`，`member_name="*"` 时广播到 active team members；模型不再必须复制 child session UUID。
- `/agent-teams/{team_id}/members/{member_id}/messages` 和模型工具共用同一 `message_agent_team_members_runtime` mailbox path。
- `send_message_to_agent` 同步 A2A 返回结构化 `session_id` / `child_session_id` / `reply` payload；`delegate_to_agent` 工具描述改为 session-first continuation，`check_async_task` 只作为 fallback status inspection。

依赖文档：

- `docs/a2a-integrated-implementation-plan-2026-06-27.md`
- `docs/a2a-relationship-retirement-plan-2026-06-27.md`
- `docs/a2a-session-substrate-design-2026-06-24.md`
- `docs/ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md`
- `docs/ccplus-session-ux-contract-2026-06-26.md`

边界：

```text
Agent Team
  当前 root session 下的 team workspace / teammate sessions / mailbox。
  更接近 CC TeamCreate + SendMessage。

A2A To Employee
  一个真实 Agent 给另一个真实 Agent 发消息或委派。
  受 A2A policy 控制。

A2A Process Graph
  多个完整 Agent 按确定流程交接 artifact。
  本轮不先做拖拽 workflow / graph editor。
```

必做项：

1. 抽出 `AgentTeamRuntimeService`：
   - `team_create` model tool。
   - `/commands/team_create/execute`。
   - `/agents/{agent_id}/agent-teams` API。
   - Plan Mode team handoff。
   - Session Workbench create team。
   - 全部调用同一个 service。

2. `team_create` tool 必须产生真实 side effect：
   - 创建 durable `AgentTeam`。
   - 创建 member sessions。
   - 写 team events。
   - 返回 session/workbench 可消费 payload。
   - 不再只返回 `requires_api_persist`。

3. Team prompt context 以 `AgentTeam` rows 为 source of truth：
   - team name。
   - member name。
   - member role。
   - member session id。
   - mailbox pending items。
   - latest status。
   - runtime tasks / signals 只是 state overlay。

4. Team message 对齐 CC：
   - by-name send。
   - `*` broadcast。
   - 不要求模型知道 child session UUID。
   - teammate idle/completion notice 进入同一个 mailbox bus。

5. A2A session-first delegation：
   - `delegate_to_agent` 返回 `session_id` / `child_session_id` first。
   - `task_id` / `run_id` 作为兼容执行句柄。
   - tool description 不再教默认 poll `check_async_task`。
   - root session timeline 展示 A2A delegation card。
   - child Agent-Agent session 人类只读。
   - `send_agent_session_message` 用于 continuation，不是普通 worker completion path。
   - wait timeout 不等于 delegation failed。

6. A2A UI：
   - root timeline A2A card。
   - child session read-only view。
   - artifact/file preview side drawer。
   - folded tool evidence summary。
   - continuation / wait / interrupt / close controls。

验收测试：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_agent_team_runtime_service.py \
  tests/tools/test_cc_codex_parity_tools.py::test_team_create_tool_persists_through_agent_team_runtime \
  tests/agents/test_subagent_spawn_tool.py::test_send_agent_session_message_routes_agent_team_by_name_without_child_session \
  tests/agents/test_subagent_spawn_tool.py::test_send_agent_session_message_appends_child_mailbox_event \
  tests/api/test_agent_teams_events_api.py \
  tests/services/test_plan_mode_agent_team_handoff.py \
  tests/api/test_cc_codex_parity_api.py::test_commands_api_team_create_and_delete_are_durable \
  tests/api/test_cc_codex_parity_api.py::test_agent_teams_api_creates_control_index_and_member_sessions \
  tests/services/test_prompt_contracts.py \
  tests/runtime/test_unified_prompt_contracts.py \
  -q
```

```bash
cd frontend && npm run test -- \
  src/pages/session-workbench/SessionNativeControls.test.tsx \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/api/domains/a2a.test.ts
```

完成标准：

- Team creation backend/runtime 没有 tool/API/Plan Mode 第二路径。
- team message backend/runtime 没有 API/tool 第二路径。
- team context 和 team mailbox 以 Team rows + member sessions 为 source of truth。
- A2A delegation backend payload 从 task-first 改为 session-first。
- UI root timeline card、child read-only view、artifact drawer 和 Workbench state 由 typed TurnEnvelope/Workbench 收口，不再另立 Team/A2A 第二规则。

### 主线 D：TurnEnvelope / Workbench / Hooks / Skill / MCP Closure

目标：把 Codex 的 typed turn/thread 工程优势吸收进 Hive，并让 Hooks / Skill / MCP 全部挂到同一个 turn manifest 和 Workbench state。

状态：已完成 Workstream D manifest/status/read-model implementation pass：TurnEnvelope / PromptAssemblyManifest read model、Session Workbench projection、AgentTeam rows -> prompt-facing Team context、HookRegistry catalog -> explicit hook state、Skill/MCP tool-event refs -> envelope、MCP server tools/prompts/resources -> ExtensionRegistry 输入。

本次闭合点：

- 新增 `backend/app/runtime/turn_envelope.py`，从 active run metadata 构建 `hive.ccplus.turn_envelope.v1` 和 `hive.ccplus.prompt_assembly_manifest.v1`。
- `build_session_workbench()` 暴露 `turn_envelope` 和 `prompt_manifest`，Workbench 不再只能从 scattered fields 猜 active turn context。
- `agent_team_context.py` 现在读取 `AgentTeam` / `AgentTeamMember` rows，渲染 `## Agent Team Workspace`，Team context 的 source of truth 与 Workstream C 的 runtime path 一致。
- `TurnEnvelope.hook_state` 现在从 `HookRegistry.describe_event_catalog()` 自动投影每个 CC standard hook 的 explicit status：`supported_active` / `supported_observe_only` / `declared_not_wired` / `unsupported_with_reason`；active run metadata 仍可覆盖具体事件。
- `build_session_workbench()` 现在从本 session 的 `load_skill` / MCP tool events 自动派生 `skill_catalog_refs`、`mcp_server_refs` 和 `active_tool_names`，不再只依赖调用方预写 metadata。
- `get_agent_mcp_servers()` 现在返回 server-first 的 `tools`、`prompts`、`resources`，其中工具以 `mcp__server__tool` 形式进入 ExtensionRegistry / manifest affordance。
- Hooks external runner 仍保持 explicit not-live 状态；本轮不把 deferred `GovernedHookRunner` 偷接进 production。现有 HookRegistry / parser / plugin hook tests 继续作为 wire standard 和 live in-process hook 证据；外部 command/prompt/http/agent hook 在统一状态面标注，而不是形成第二条隐藏路径。

依赖文档：

- `docs/ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md`
- `docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md`
- `docs/ccplus-session-ux-contract-2026-06-26.md`
- `docs/agent-extension-surface-skill-mcp.md`
- `docs/SKILLS_AND_PACKS_V2.md`
- `docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md`

必做项：

1. 建立 `TurnEnvelope`：
   - `turn_id`
   - `session_id`
   - `runtime_task_id`
   - `source`
   - `channel`
   - `model`
   - `context_window`
   - `approval_policy`
   - `permission_profile`
   - `sandbox_policy`
   - `multi_agent_mode`
   - `active_tool_names`
   - `deferred_tool_names`
   - `skill_catalog_refs`
   - `mcp_server_refs`
   - `memory_refs`
   - `team_mailbox_refs`
   - `a2a_collaborator_refs`
   - `hook_state`
   - `prompt_sections`
   - `output_cap`
   - `trace/span ids`

2. 建立 `PromptAssemblyManifest`：
   - frozen sections。
   - dynamic sections。
   - context budget decisions。
   - loaded skills。
   - available agent types。
   - MCP instructions delta。
   - hook-added context。
   - redacted prompt preview。

3. Workbench active-turn snapshot：
   - mailbox。
   - tools。
   - approvals。
   - hooks。
   - permissions。
   - sandbox。
   - runtime task refs。
   - context window。
   - branch/projection state。
   - child sessions。

4. Hooks closure：
   - 已完成：每个 CC standard hook event 有状态：`supported_active`、`supported_observe_only`、`declared_not_wired`、`unsupported_with_reason`。
   - 已完成：状态进入 `TurnEnvelope` / Workbench，unsupported 不再 silent noop。
   - 明确边界：`GovernedHookRunner` 仍不是 production path；command / prompt / http / agent external hooks 未伪装为 active，必须继续走 existing tests 的 deferred contract，直到 code execution provider、outbound policy、durable span、resume-wake 全部接通。

5. Skill closure：
   - Skill matching 时强化 blocking requirement：匹配 skill 的任务先 load/invoke skill，再回答。
   - Skill frontmatter hooks structured parse + runtime registration。
   - Skill forked execution 的等价路径需要明确：通过 AgentTool/session worker，还是 SkillTool wrapper。
   - 已完成：`load_skill` tool events 进入 TurnEnvelope 的 `skill_catalog_refs` / `active_tool_names`。

6. MCP closure：
   - 已完成：MCP server read model 返回 `tools` / `prompts` / `resources`，ExtensionRegistry 可把 MCP prompt/resource 映射到 command/skill runtime effects。
   - 已完成：MCP tool events 进入 TurnEnvelope 的 `mcp_server_refs` / `active_tool_names`。
   - MCP instructions delta live attachment。
   - MCP needs-auth pseudo-tool 或等价 auth flow。
   - 决定 model-visible MCP tool surface：
     - 当前采用 `call_mcp_tool` governed wrapper + `mcp__server__tool` read-model affordance，不新增绕过 governance 的直接调用路径。
   - resource list/read canonical naming 对齐。

验收测试：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/runtime/test_turn_envelope_prompt_manifest.py \
  tests/services/test_session_control_plane.py \
  tests/kernel/test_turn_state_acceptance.py \
  tests/services/test_agent_team_context.py \
  tests/services/test_session_graph_projection.py \
  tests/runtime/test_hooks.py \
  tests/runtime/test_hook_wire_standard.py \
  tests/runtime/test_governed_hook_runner.py \
  tests/services/test_extension_registry.py \
  tests/services/test_mcp_server_service.py \
  tests/api/test_extension_registry_api.py \
  -q
```

本轮证据（2026-06-27）：`ruff check app/runtime/turn_envelope.py app/services/session_control_plane.py app/services/mcp_server_service.py tests/runtime/test_turn_envelope_prompt_manifest.py tests/services/test_session_control_plane.py tests/services/test_mcp_server_service.py` -> `All checks passed!`；上述 pytest scope -> `105 passed, 4 warnings`。

最终跨主线收口证据（2026-06-27）：`ruff check app/runtime/turn_envelope.py app/services/session_control_plane.py app/services/mcp_server_service.py app/services/agent_team_runtime_service.py app/tools/handlers/subagent.py app/tools/handlers/command_parity.py app/api/commands.py app/api/agent_teams.py tests/runtime/test_turn_envelope_prompt_manifest.py tests/services/test_session_control_plane.py tests/services/test_mcp_server_service.py tests/services/test_agent_team_runtime_service.py tests/tools/test_cc_codex_parity_tools.py tests/agents/test_subagent_spawn_tool.py` -> `All checks passed!`；跨 A/B/C/D pytest scope -> `200 passed, 4 warnings`。

完成标准：

- 每个 turn 都能解释自己加载了什么、隐藏了什么、为什么。
- Workbench 不靠猜测拼 session state。
- Team context 读取真实 Team rows，不再只读 RuntimeTask/Signal。
- Hooks / Skill / MCP 的 session-visible 状态进入同一个 TurnEnvelope manifest；如果外部 runner 未接生产，必须显式标注 deferred/not-wired，不能假装 active。
- Hooks / Skill / MCP 不再只是存在入口，而是进入 session lifecycle read model / ExtensionRegistry / Workbench。
- Unsupported 能力显式标注，不伪装成已支持。

## 4. 总执行顺序

必须按以下顺序做，不并行改语义主干：

```text
0. 文档收敛和状态回写
1. Session Control Spine
2. AgentTool / Sub-agent / Completion Bus
3. Agent Team Runtime
4. A2A Session-first Delegation
5. TurnEnvelope / PromptAssemblyManifest / Workbench State
6. Hooks / Skill / MCP Closure
7. A2A Process Graph 完整 slice
```

解释：

1. Session command 是 session identity 和 projection 的底座，所以先做。
2. AgentTool 和 Completion Bus 是 session 内 multi-agent 触发与反馈的底座，所以第二。
3. Agent Team 依赖 AgentTool 和 mailbox，所以第三。
4. A2A Session-first 依赖 session command、mailbox、child session state，所以第四。
5. TurnEnvelope 可以在前几步逐步记录字段，但完整收口应在主要 runtime path 稳定后做。
6. Hooks / Skill / MCP 依赖 TurnEnvelope 和 Hook boundary，所以最后闭合。
7. A2A Process Graph 是更高层编排，不应抢在 session spine 前面。

## 5. 测试总矩阵

### 5.1 后端目标测试组

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest \
  tests/services/test_session_command_runtime.py \
  tests/services/test_session_control_plane.py \
  tests/tools/test_agent_tool_cc_compat.py \
  tests/runtime/test_coordinator_agenttool_visibility.py \
  tests/runtime/test_agenttool_employee_delegation_split.py \
  tests/runtime/test_subagent_prompt_affordance_contract.py \
  tests/services/test_subagent_completion_mailbox.py \
  tests/runtime/test_parent_turn_receives_subagent_notification.py \
  tests/tools/test_team_create_tool_persists_team.py \
  tests/services/test_agent_team_context.py \
  tests/services/test_a2a_session_first_delegation.py \
  tests/runtime/test_turn_envelope_prompt_manifest.py \
  tests/runtime/test_governed_hook_runner_live_wiring.py \
  tests/skills/test_skill_hooks_runtime_registration.py \
  tests/services/test_mcp_prompts_as_skills.py \
  -q
```

### 5.2 前端目标测试组

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend

npm run test -- \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/pages/session-workbench/SessionNativeControls.test.tsx \
  src/api/domains/ccParity.test.ts \
  src/api/domains/a2a.test.ts
```

### 5.3 最终 smoke

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run build
```

如果全量后端测试仍受无关 collection error 阻断，必须单独记账，不能把局部通过包装成全量通过。

## 6. 验收总口径

上线前最后一轮完成时，必须能回答以下问题：

1. 一个用户 turn 从提交到模型调用，经过了哪些 context、permission、tools、skills、hooks、mailbox？
   - 答案必须来自 TurnEnvelope / PromptAssemblyManifest。

2. `/compact`、`/rewind`、`/branch`、`/clear` 到底改变了什么？
   - 答案必须来自 session control event 和 active projection。

3. 模型什么时候应该创建 session worker？
   - 答案必须来自 AgentTool prompt affordance 和 multi-agent mode。

4. 模型什么时候应该发给另一个真实数字员工？
   - 答案必须来自 A2A Collaborators 和 A2A policy。

5. 子任务完成后主 Agent 怎么知道？
   - 答案必须是 session mailbox / input queue automatic delivery。

6. Team 是怎么创建和通信的？
   - 答案必须是同一个 AgentTeamRuntimeService 和 mailbox，而不是 tool/API/UI 多路径。

7. Hooks / Skill / MCP 是否真的进入 session lifecycle？
   - 答案必须能在 TurnEnvelope / HookRegistry / Workbench snapshot 里看到。

8. 用户看到什么？
   - 默认看到工作判断、控制卡片、artifact、必要阻塞；raw JSON 和 tool detail 默认折叠。

## 7. 非目标

本轮不做：

- A2A drag-and-drop workflow editor。
- A2A Process Graph 的大而全 UI。
- 把 subagent 升级成完整数字员工。
- 把 A2A 与 Dynamic Workflow 合并。
- 把 Codex fork 命令改名暴露给 Hive 用户；用户命令统一叫 branch。
- 把企业后台 approval 队列和 session-local permission mode 混在一起。
- 为了触发 subagent 而放宽 A2A policy。
- 为了上线快而绕过 T0 / InvocationSpan / ToolRuntimeService / governance。

## 8. 文档更新规则

从本文创建后：

1. `docs/ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md` 保留为 runtime evidence，不再单独作为上线前总计划。
2. `docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md` 保留为子系统 evidence。
3. `docs/ccplus-session-control-command-alignment-2026-06-27.md` 是主线 A 的专项 contract。
4. `docs/ccplus-session-runtime-token-compaction-alignment-2026-06-27.md` 是主线 A 的底座 evidence；其中“当前不应该先修 A2A”的旧措辞应理解为“不要绕过 session spine 先做 A2A 高层产品化”。
5. `docs/a2a-integrated-implementation-plan-2026-06-27.md` 是主线 C 的 A2A 专项；Phase 1 已完成，下一步不是回到 Relationship，而是 Session-first delegation。
6. `docs/a2a-relationship-retirement-plan-2026-06-27.md` 是 To Employee 权限/read model 边界，不再影响 To Session Worker。

任何后续实现 PR 或 session 计划，都必须先说明自己属于主线 A/B/C/D 的哪一项。
