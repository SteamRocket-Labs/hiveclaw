# CCPlus Runtime / Context / AgentTool 总差距审计

日期：2026-06-27  
范围：CC/FreeCode 语义基线、claude-code-org 交叉校验、Codex 工程增量、Hive 当前后端/前端/runtime 对齐差距。  
状态：审计与实施计划。本文档不声明差距已经关闭。

## 文档关系

本文是本轮 CCPlus runtime / context / AgentTool / Codex delta 的审计主文档。

上线前最后一轮优化的统领性计划已经提升到 `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md`。后续排期、四条主线、执行顺序和验收总口径以该 master plan 为准；本文继续作为 runtime / context / AgentTool / Codex delta 的证据和 P0/P1 差距来源。

同日文档 `docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md` 是 Subagent、Agent Team、Skill、MCP、Hooks 的子系统附录。若专项文档发生冲突，按 `docs/ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md` 的四条主线和执行顺序裁决；任何后续修改必须同步更新对应断点，不能形成第二套结论。

## 0. 北极星

Hive 的目标不是做一个泛泛的 multi-agent 产品。我们的目标一直是：

1. **CC / FreeCode 是语义基底。**
   Hive 必须对齐本地 CC 生命周期语义，包括 prompt 接收、上下文组装、model loop、tool loop、hooks、compaction、subagent、team、workflow、skill、MCP、session resume 和 session close。
2. **Codex 是工程增量。**
   Codex 可以提供 typed thread/turn state、active-turn snapshot、app-server API、sandbox/approval profile、input queue、workbench UI、observability 等工程优化，但不能重写 CC 的语义边界。
3. **Hive-native 增量必须显式。**
   Memory/T0/T2/T3/soul/Iter/self-evolution 和企业控制中台是 Hive 原生能力。它们包裹和增强 CC parity，但不能拿来掩盖 CC 行为缺口。

因此本轮目标必须明确：

> 先对齐 CC 语义，再吸收 Codex 工程控制，最后叠加 Hive Memory/控制中台。除非是带退休计划的兼容 shim，否则不允许出现第二条语义路径。

## 1. 总结论

当前 Hive **还没有完全达到 CCPlus 的 session-middle multi-agent 对齐要求**。

好的一面是底座已经有了。Hive 当前已经具备：

- `ChatSession`
- `RuntimeTask`
- T0 events
- `InvocationSpan`
- `spawn_subagent`
- durable background subagent run
- `AgentTeam` 表和 API
- Session Workbench
- hook catalog
- workflow daemon
- Skill/MCP progressive disclosure
- 前端控制面

真正的问题不是“完全没有机制”，而是：

- **断点多**
- **路径多**
- **模型可见 affordance 不统一**
- **完成反馈没有收束成唯一 Session input/wake bus**
- **前端和后端都已有若干面，但没有形成一个统一生命周期协议**

最重要的具体问题：

- CC 的模型可见主谓词是 `AgentTool`；Hive 现在把 `spawn_subagent`、`delegate_to_agent`、`team_create`、command API、workbench API、wake signal 拆成了若干相邻但不统一的面。
- CC 的 async worker completion 会在后续 turn 作为 user-role `<task-notification>` 自动回到主 Agent；Codex 的 inter-agent completion 进入统一 `InputQueue`；Hive 当前则有 durable run state、`CoordinationSignal`、parent T0 event append、`subagent_wake` invocation、`check_subagent`、内部 `consume_subagent_signals` helper。它们都是有用部件，但不是一个 canonical input/wake path。
- CC coordinator mode 使用 `AgentTool` 作为 worker spawn primitive；Hive coordinator 目前过滤掉 `spawn_subagent`，并引导模型使用 `delegate_to_agent`。
- `delegate_to_agent` 当前语义是 “send to another digital employee / A2A”，不是 “session 内创建 lightweight worker”。它被 A2A collaborator prompt 和 `bridge:self` gate 包裹，在单员工部署或没有协作关系时天然不会成为 CC-style subagent 触发路径。
- CC Agent Team 是 model-visible team workspace、teammate session 和 automatic message delivery 的组合；Hive 有 API/UI team creation，但 LLM tool `team_create` 只是返回 `requires_api_persist`，真正持久化在 API command route。这是两条语义路径。
- Codex 的 `TurnContext`、`InputQueue`、`ThreadState`、`ThreadConfigSnapshot`、active turn snapshot、background terminal API 是很强的工程面；Hive 有等价碎片，但还没有所有 runtime source 共同使用的 typed `TurnEnvelope`。

实际结论：

> 下一轮优化必须是 convergence pass，不是继续加功能。我们需要一个 AgentTool 语义面、一个 session input queue/wake path、一个 Agent Team runtime path、一个 context assembly manifest、一个 workbench state model。

## 2. 源码证据

### 2.1 CC / FreeCode 语义基线

主要本地基线：

```text
/Users/rocky243/vc-saas/free-code-main
```

关键证据：

- `src/tools/AgentTool/AgentTool.tsx`
  - 输入支持 `description`、`prompt`、`subagent_type`、`model`、`run_in_background`，以及 team 场景的 `name` / `team_name`。
  - 如果省略 `subagent_type` 且 fork 未开启，默认是 `general-purpose`。
  - Team spawn 不是独立的另一套 runtime。`AgentTool` 带 `team_name` + `name` 时进入 teammate spawn。
- `src/tools/AgentTool/prompt.ts`
  - Agent tool prompt 教模型什么时候 fork/spawn、怎么给 fresh worker 写 prompt。
  - 明确说明 background/fork 结果会在后续 turn 作为独立 user-role message 回来。
  - 明确禁止默认 peek/poll，不允许 coordinator 在 notification 到达前编造结果。
- `src/tools/AgentTool/built-in/generalPurposeAgent.ts`
  - `general-purpose` 是默认的广义调查、多步任务 worker。
- `src/coordinator/coordinatorMode.ts`
  - Coordinator 的主工具是 `AgentTool`。
  - Worker 结果通过 `<task-notification>` user-role message 回来。
  - prompt 强调可以在同一条 assistant message 里发多个 `AgentTool` calls 来并行。
- `src/tools/TeamCreateTool/prompt.ts`
  - Team creation 对复杂协作任务应主动使用。
  - Team member 通过 `AgentTool` spawn。
  - Teammate 消息自动投递，team lead 不需要手动 poll。
- `src/query.ts`
  - queued task notifications 会 drain 成 attachment messages。
  - subagent 只消费发给自己 agentId 的 task-notification。
  - 已消费的 prompt/task-notification commands 会从 queue 移除。

claude-code-org 交叉校验：

```text
/Users/rocky243/Context Engineering/claude-code-org
```

该 checkout 的源码和机制说明确认同样语义：

- `AgentTool`
- `TeamCreateTool`
- `<task-notification>`
- `isMeta` user-role message 是统一注入总线，用于 CLAUDE.md、plan 约束、tick、cron、task notification 等“系统需要让模型知道但不是真人说的话”。

Python port 交叉校验：

```text
/Users/rocky243/Context Engineering/claw-code/src
```

当前 Python port 主要保留 TS tool 模块的 reference snapshot，不覆盖 FreeCode 判断。它强化了 FreeCode 是 essential baseline 的结论。

### 2.2 Codex 工程增量

主要本地基线：

```text
/Users/rocky243/Context Engineering/codex/codex-rs
```

Codex 不替代 CC 语义目标。它值得吸收的是工程面：

- `core/src/session/session.rs`
  - Session 统一持有 thread id、active turn、input queue、permission profile、sandbox policy、environment、parent/fork lineage、session source、`ThreadConfigSnapshot`。
- `core/src/session/turn_context.rs`
  - `TurnContext` 是 typed per-turn envelope，包含 sub id、trace id、model/provider、session source、parent thread、environment、cwd、current date/timezone、developer instructions、collaboration mode、multi-agent version、approval policy、permission profile、dynamic tools、extension data、skills、timing、terminal error。
- `core/src/session/input_queue.rs`
  - 一个 `InputQueue` 同时承载 `UserInput`、`ResponseItem`、`InterAgentCommunication`。
  - Mailbox input 可以触发新 turn，也可以投递到当前 turn。
- `core/src/tasks/mod.rs`
  - 一个 session 同时只有一个 active task。
  - start task 时标记 turn start、drain pending input、开 span、flush rollout、发 terminal event、清 active turn。
  - 如果有 trigger-turn mailbox work，可以在 idle 状态自动启动 turn。
- `core/src/session/inject.rs`
  - 统一处理“注入 active work”或“在 idle 时启动 work”。
  - 如果处于 Plan mode、busy、或已有 pending trigger-turn mailbox，则拒绝自动 idle work。
- `core/src/session/multi_agents.rs` 和 `features/src/feature_configs.rs`
  - `MultiAgentV2` 是 gated feature，包含 root/subagent usage hint、并发上限、wait timeout、namespace、proactive vs explicit-only mode。
- `app-server/src/thread_state.rs`
  - 跟踪 pending interrupts、pending rollbacks、active-turn snapshot、terminal turn id、current turn history、listener generation、raw event mode。
- `app-server-protocol/src/protocol/common.rs`
  - typed thread APIs 包括 start/resume/fork/archive/delete/unsubscribe/name/goal/settings/memory mode/read/list/items/turns/background terminals。
- background terminal 支持的是 process/terminal 管理，不是 CC background subagent 语义替代品。

Hive 应吸收的 Codex 工程点：

- 一个 typed `TurnContext` / `TurnEnvelope`
- 一个 session-scoped input queue，支持 inter-agent mailbox delivery
- active-turn snapshot 和 pending interrupt/rollback state
- thread/session control API 和 config snapshot
- per-turn permission profile + sandbox snapshot
- background terminal/process 作为独立 runtime category

## 3. Hive 当前状态

### 3.1 Runtime Entry

相关文件：

```text
backend/app/runtime/invoker.py
backend/app/kernel/engine.py
backend/app/runtime/prompt_builder.py
backend/app/services/web_chat_runtime.py
backend/app/runtime/session.py
```

已经做对的部分：

- `invoke_agent()` 是主 runtime entry。
- `USER_PROMPT_SUBMIT` 在 durable append 之后、model loop 之前触发。
- kernel loop 会解析 memory、retrieval、runtime metadata、permissions、deferred tools、coordinator prompt、prompt cache、tools。
- web chat 已经通过 `RuntimeTask(task_type="web_chat_turn")` durable 化。
- prompt 有 frozen prefix + dynamic suffix 分层。
- skill catalog 和 memory 在 dynamic suffix，保留 prompt-cache 边界。

缺口：

- 没有一个显式的 `TurnEnvelope` / `PromptAssemblyManifest`，记录某个 turn 的所有 input、context section、permission profile、active tool surface、runtime source、output budget。
- context assembly 是多个 resolver callbacks 和 prompt sections 拼出来的，能跑，但难审计，也难对齐 Codex typed `TurnContext`。

### 3.2 Agent Tool / Subagent

相关文件：

```text
backend/app/tools/handlers/subagent.py
backend/app/agents/subagent.py
backend/app/services/subagent_run_service.py
backend/app/services/subagent_wake_consumer.py
backend/app/services/workflow_daemon.py
backend/app/runtime/coordinator.py
backend/app/services/agent_tools.py
```

已经做对的部分：

- `spawn_subagent` 是 normal agent 的 core turn-1 visible tool。
- `check_subagent` 作为 fallback 存在。
- background subagent 会创建 durable `RuntimeTask(task_type="subagent")` 和 child `ChatSession`。
- completion signal 之前会先写 durable run terminal state。
- `workflow_daemon` 会 drain subagent wake signals，并能唤醒 parent agent。
- parent wake 会 append `subagent_wake` 的 T0 events。

缺口：

- Hive tool schema 是 `task`、`type`、`definition_name`、`run_in_background`；CC canonical AgentTool 是 `description`、`prompt`、`subagent_type`、`model`、`run_in_background`，默认 `general-purpose`。
- Hive built-ins 是 `explorer`、`worker`、`critic`；CC 默认是 `general-purpose`，named definitions 是叠加层。
- Hive coordinator mode 当前允许 `delegate_to_agent`、async task tools、messaging、files、triggers，但不允许 `spawn_subagent`。这和 CC coordinator 把 `AgentTool` 作为主 worker spawn primitive 冲突。
- parent wake prompt 里写了“use `consume_subagent_signals`”，但 `consume_subagent_signals` 只是 Python 内部 helper，不是 model-visible tool。这是实际 affordance 断点。
- completion 可能通过 signal、T0 event、wake message、`check_subagent`、child session state、team context block 多种方式呈现。它们必须收束成一个 input queue / mailbox projection。

### 3.2.1 `delegate_to_agent` / A2A 与 Session Worker 混层

新增确认的 P0 断点：

- `backend/app/tools/handlers/communication.py` 中 `delegate_to_agent` 的描述是 “another digital employee”，参数是 `agent_name`，并且 `plan_gate_action_kind="bridge:self"`。
- `backend/app/runtime/prompt_sections/executing_actions.py` 的 Collaboration prompt 要求：不要委托给自己，并通过 `A2A Collaborators` 确认存在同事后再委托。
- `backend/app/services/a2a_collaboration_policy.py` 明确有 self block、cross-owner group、active member 等 A2A policy。
- `backend/app/runtime/coordinator.py` 却把 `delegate_to_agent` 当作 coordinator worker 的 primary verb，并过滤掉 `spawn_subagent`。

这说明当前系统把两种不同语义混在了一起：

| 语义层 | 应该表示什么 | 应该使用的 runtime surface | 是否需要 relationships / A2A Collaborators |
|---|---|---|---|
| To Session Worker | 当前 session 内的 lightweight subagent、forked worker、critic、explorer、team member worker | CC-compatible `AgentTool` / 内部 `spawn_subagent` / team mailbox | 不需要 |
| To Employee | 给另一个真实数字员工发任务，跨 employee identity、owner、tool policy 和治理边界 | `delegate_to_agent` / `send_message_to_agent` / A2A Collaboration Group | 需要 |

因此 `relationships.md` 或 A2A Collaborators 只能约束 To Employee。它不应该决定 session 内 worker 是否可用。单 agent 部署下也必须能触发 To Session Worker；否则 Hive 在 Subagent/AgentTool 上永远达不到 CC 基线。

直接结论：

- `delegate_to_agent` 不能再作为 CC `AgentTool` 的等价物。
- Coordinator 默认 worker spawn 必须走 CC-compatible AgentTool surface。
- `delegate_to_agent` 保留为 governed A2A / digital employee bridge，只有用户明确要发给另一个 employee，或 prompt context 里存在可调用同事时才使用。
- Plan Mode / `bridge:self` gate 只应该保护 To Employee 这种身份/组织边界动作，不应该挡住 session 内 worker spawn。

### 3.2.2 Prompt Affordance Parity

补充确认：Subagent 从不主动触发的主因不是 “LLM 看不见工具”，而是 **可见但缺少 CC-style 触发 affordance**。

Hive 当前 `spawn_subagent`、`check_subagent`、`delegate_to_agent`、`send_agent_session_message` 都属于 core tool surface。问题在 prompt 层：

- `executing_actions.py` 的默认基调是先自己做，`spawn_subagent` 只是分支选择。
- `spawn_subagent` tool description 有类型说明，但缺少 CC AgentTool 的 few-shot examples。
- `_TYPE_DESCRIPTIONS` 有 whenToUse，但没有类似 CC `agent_listing_delta` 的持续注入。
- 系统级 “何时用 subagent / 何时不要用” 没有形成独立常驻 section。
- 最系统的 delegation guide 是 A2A employee delegation skill，不是 session worker guide。

CC 提示词风格偏 **行为路由器**：when-to-use、when-not-to-use、few-shot、agent listing、parallel fan-out 指令都直接服务工具触发。Codex 提示词风格偏 **工程执行协议**：repo discipline、sandbox/approval、plan/tool discipline、以及配置化 `MultiAgentMode`。Hive 的修法必须同时吸收两者：

- 从 CC 吸收 AgentTool 的触发写法和 examples。
- 从 Codex 吸收 `explicitRequestOnly` / `proactive` 这种可审计模式开关。
- 用 TurnEnvelope/PromptAssemblyManifest 记录本 turn 到底处于哪种 multi-agent mode、注入了哪些 agent types、为什么允许或抑制 proactive spawn。

### 3.3 Agent Team

相关文件：

```text
backend/app/tools/handlers/command_parity.py
backend/app/api/commands.py
backend/app/api/agent_teams.py
backend/app/models/agent_team.py
backend/app/services/agent_team_context.py
backend/app/services/session_control_plane.py
frontend/src/pages/session-workbench/SessionNativeControls.tsx
frontend/src/api/domains/ccParity.ts
```

已经做对的部分：

- durable `AgentTeam`、`AgentTeamMember`、`AgentTeamEvent` 已存在。
- `/agents/{agent_id}/agent-teams` API 会创建 enterable member sessions。
- Session Workbench 可以创建 team、列 team、进入 member session、关闭 team、渲染 workbench state。
- `session_control_plane.py` 会把 team members 放入 session graph。

缺口：

- model-visible `team_create` tool 在 `command_parity.py` 里只返回 `requires_api_persist`；API command route 才做持久化。这制造了 LLM tool call 与 API/workbench call 两条语义路径。
- `agent_team_context.py` 当前主要投影 `RuntimeTask` 类型 `subagent`、`workflow`、`delegation` 和 `CoordinationSignal`；它没有把 `AgentTeam` rows 作为 prompt-facing team source of truth。
- CC Team flow 是 `TeamCreate -> Task list -> AgentTool(team_name, name) -> automatic teammate messages`。Hive 当前有 API/UI team creation，但 model-loop 语义还不是同一条链。
- 前端 `/team` slash menu 当前测试明确隐藏 `team_create`，但 Workbench 又暴露 team creation。这可以是产品选择，但不是 CC-like unified command/tool surface。

### 3.4 Background Agent

“Background Agent”必须拆成三类，不能混用：

1. **CC-style background subagent**
   - 由 AgentTool / `run_in_background` spawn。
   - 立即返回。
   - 后台完成。
   - completion 自动作为 model-visible next-turn input 投递给 parent session。
2. **Codex-style background terminal/process**
   - 是 unified exec 管理的 shell/process。
   - 通过 background terminal APIs list/terminate。
   - 不是 worker agent，也不是 teammate。
3. **Hive autonomous background work**
   - trigger、schedule、heartbeat、dream、long-running workflow、local bridge work request。
   - 是 enterprise-governed runtime task。
   - 可以 wake agent，但不能和 subagent completion 混成一个语义。

当前 Hive 在命名和 UI 上有混用风险。下一轮必须强制区分：

- `background_subagent` 是 inter-agent mailbox event。
- `background_terminal` 是 process/exec resource。
- `autonomous_run` 是 trigger/heartbeat/dream/workflow。

三者都可以使用 `RuntimeTask` 和 T0，但不能共享模型可见语义。

### 3.5 Hooks

相关文件：

```text
backend/app/runtime/hooks.py
backend/app/runtime/hooks_setup.py
backend/app/api/hooks.py
frontend/src/pages/session-workbench/SessionNativeControls.tsx
```

已经做对的部分：

- hook event catalog 包含 CC-compatible events：
  - PreToolUse
  - PostToolUse
  - UserPromptSubmit
  - SessionStart / SessionEnd
  - Stop / StopFailure
  - SubagentStart / SubagentStop
  - PreCompact / PostCompact
  - Notification
  - PermissionRequest / PermissionDenied
  - TeamCreated / TeamClosed
  - TeammateIdle
- blocking support 已存在：
  - `PRE_TOOL_USE`
  - `USER_PROMPT_SUBMIT`
  - `STOP`
  - `SUBAGENT_START`
  - `SUBAGENT_STOP`
- Session Workbench 已有 hook config UI。

缺口：

- 若干 CC events 仍是 disabled/noop 或 observe-only。
- hooks 可注册、可配置，但还不是完整的 external hook runtime 等价物。
- hook effects 还没有稳定进入一个 turn/session workbench state。

### 3.6 Skill / MCP

已经做对的部分：

- `load_skill` 和 `tool_search` 存在。
- Skill catalog 位于 dynamic prompt suffix，不在 frozen prefix。
- MCP list/import/call/resource tools 存在，并且经过 governance。

缺口：

- Skill 现在仍然分散在 system catalog、installed package、skill capsule、workflow skill、subagent definitions、MCP-declared tools 等多个相邻面。
- CC skill progressive disclosure 应该表现为一个 capability capsule loading contract，而不是多个相邻 discovery paths。
- MCP Skill/hook 行为应该进入同一套 permission/profile/turn manifest。

### 3.7 Frontend / UI / UX

已经做对的部分：

- Session Workbench 已存在。
- Session graph 和 `active_turn` payload 已存在。
- Team controls 已存在。
- Hook controls 已存在。
- Chat runtime events 已包含 child session 和 workflow identifiers。

缺口：

- UI 现在反映的是多个后端概念，不是一个统一的 session lifecycle surface。
- 还没有 Codex-like active-turn snapshot，把 pending input、pending mailbox、interrupt/rollback、tool state、permission profile、background terminals、subagents、teams 放在一个稳定位置。
- Agent Team UI 有了，但还没有和 model-visible CC Team lifecycle 深度绑定。
- background subagent completion 还不能保证以一个 canonical visible event 出现在 parent timeline/workbench。

## 4. 原子生命周期矩阵

| 生命周期环节 | CC 基线 | Codex 可吸收工程增量 | Hive 当前状态 | 差距 | 目标 |
|---|---|---|---|---|---|
| Agent definition | `.claude/agents` definitions + built-ins | selected capability roots + turn skills | Agent DB、soul、skills、subagent definitions | 概念存在但分散 | 一个 Agent/Skill/Subagent definition index，带 provenance |
| User prompt accepted | durable turn before model loop | thread/turn start event | `USER_PROMPT_SUBMIT`、T0/web chat append | 基本对齐 | 保持 |
| Context assembly | CLAUDE.md + tools + skills + queued attachments | typed `TurnContext` + `WorldState` | frozen prefix + dynamic suffix resolver graph | 没有单一 manifest | `TurnEnvelope + PromptAssemblyManifest` |
| Tool surface | AgentTool/Skill/MCP/Task/Team | dynamic tools + deferred tools | core tools + deferred packs | 名称和默认语义未 CC 对齐 | CC-compatible alias/schema canonical |
| Coordinator spawn | `AgentTool(subagent_type:"worker")` | MultiAgentV2 hint/gating | coordinator 用 `delegate_to_agent`，过滤 `spawn_subagent` | P0 mismatch | coordinator 可见 canonical AgentTool |
| Send to worker vs employee | Session worker 用 AgentTool；真实同事用 SendMessage/A2A | `InterAgentCommunication` 和 thread mailbox 可分 source | `delegate_to_agent` 同时承担 worker 和 employee 语义 | A2A gate 阻断单 agent session worker | To Session Worker / To Employee 分层 |
| Subagent sync | child returns digest | trace/span lineage | `spawn_subagent` sync returns digest | 接近，但 schema 不同 | CC AgentTool-compatible wrapper |
| Subagent async | later `<task-notification>` user message | mailbox `InterAgentCommunication` + trigger turn | signal + wake + check tool + T0 append | 多路径，prompt 提到内部 helper | 一个 parent session input queue/mailbox |
| Agent Team create | TeamCreate creates team/task list | thread graph/workbench | API creates team，LLM tool returns handoff | 两条路径 | tool/API/UI 共用一个 runtime service |
| Teammate work | AgentTool with `team_name` and `name` | subagent thread lineage | team member sessions/API | 未 model-loop 集成 | teammate spawn/message 进入同一 AgentTool/team mailbox |
| Completion feedback | automatic notification，无 polling | active/input queue wake | wake daemon + signal + check | 接近但不唯一 | automatic next-turn input，`check` 只作 debug |
| Hooks | blocking hooks around prompt/tool/stop/subagent | lifecycle events + app-server state | catalog + partial active hooks | 有 noop/observe-only | full hook runtime + workbench trace |
| Compaction | model-authored context collapse | rollout/history builder | proactive/reactive compaction | 底座基本有 | 进入 TurnEnvelope 和 workbench |
| Resume/fork | session files / resume | typed thread resume/fork/read/list | ChatSession/session graph/export | partial | typed session/thread APIs with snapshots |
| Background terminals | 非 AgentTool | explicit background terminal APIs | code exec provider 有 runtime，无统一 UI parity | 需要独立建模 | process resource panel，不走 subagent path |

## 5. P0 差距

### P0-1：Canonical AgentTool Surface

问题：

- Hive 的 `spawn_subagent` 是一个好 primitive，但还不是 CC-compatible AgentTool surface。
- Coordinator mode 没有暴露 `spawn_subagent`。
- 默认类型是 `explorer`，而 CC 省略 type 时默认是 `general-purpose`。
- `delegate_to_agent` 是 A2A employee delegation，不是 session worker spawn；不能继续让 coordinator 依赖它来模拟 CC AgentTool。
- prompt affordance 不足：缺少 AgentTool examples、When NOT to use、agent type listing delta、parallel fan-out 强触发。

必须修复：

- 引入 canonical model-visible AgentTool-compatible tool surface。内部服务可以继续叫 `spawn_subagent`，但模型可见 schema 必须接受：
  - `description`
  - `prompt`
  - `subagent_type`
  - `model`
  - `run_in_background`
  - `name`
  - `team_name`
- 旧字段 `task`、`type`、`definition_name` 只作为兼容 alias。
- 省略 `subagent_type` 时映射到 `general-purpose`。
- Hive built-ins 映射：
  - `general-purpose` -> 当前 `worker` 或新增真正的 general-purpose definition
  - `explorer` -> read-only investigate
  - `critic` -> verification
- Coordinator mode 必须包含这个工具，并使用 CC coordinator 语义。
- 增加 CC-style prompt contract：
  - 常驻 To Session Worker guidance。
  - `spawn_subagent`/AgentTool few-shot examples。
  - available agent types + whenToUse attachment。
  - explicit/proactive multi-agent mode 注入 TurnEnvelope。

测试先行：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_agent_tool_cc_compat.py -q
pytest tests/runtime/test_coordinator_agenttool_visibility.py -q
pytest tests/runtime/test_subagent_prompt_affordance_contract.py -q
```

### P0-1.5：To Session Worker / To Employee 分层

问题：

- 当前 prompt 和 coordinator 把 “delegate work” 统一推向 `delegate_to_agent`。
- 但 `delegate_to_agent` 是另一个数字员工的 A2A bridge，有 relationships / collaborator / self block / cross-owner policy。
- 这会导致单员工部署中 multi-agent session loop 直接失效：模型被提示词劝退，runtime 也可能被 `bridge:self` 或 A2A policy 拦住。

必须修复：

- 形成唯一分层，不再让一个 tool 承担两种语义：
  - **To Session Worker**：session 内 subagent / fork / critic / explorer / team member。模型可见为 CC-compatible `AgentTool`，内部可以调用 `spawn_subagent` 和 session mailbox。
  - **To Employee**：真实数字员工之间的 A2A delegation。模型可见为 `delegate_to_agent` / `send_message_to_agent`，必须经过 A2A Collaborators、relationships、capability gate、Plan Mode bridge。
- `executing_actions.py` 的 Collaboration 文案要改成：
  - session 内并行/探索/验证任务使用 AgentTool / subagent；
  - 只有发给另一个 digital employee 时才读 A2A Collaborators；
  - 不再把 “每个 delegated task” 都绑定到 colleague existence。
- `runtime/coordinator.py` 的 primary worker verb 从 `delegate_to_agent` 改为 canonical AgentTool surface。
- `delegate_to_agent` 的 tool description 保留 “another digital employee”，并明确不是 session-internal worker。
- `check_async_task` / `send_agent_session_message` 只作为 A2A child continuation 或兼容工具；session worker completion 走 mailbox/input queue。

测试先行：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_agenttool_employee_delegation_split.py -q
pytest tests/runtime/test_coordinator_agenttool_visibility.py -q
pytest tests/tools/test_delegate_to_agent_a2a_only.py -q
```

### P0-2：唯一 Background Completion Path

问题：

- background subagent completion 有多条平行 surface。
- wake prompt 引用了 `consume_subagent_signals`，但它不是 model-visible tool。
- parent 可以被 synthetic user message 唤醒，但所有 pending completions 没有统一 queue 和 exactly-once delivery 语义。

必须修复：

- 新增或收敛成一个 session-scoped `AgentInputQueue` / mailbox service：
  - `enqueue_inter_agent_message(parent_session_id, message, trigger_turn=true)`
  - `drain_for_turn(session_id, delivery_phase=current|next)`
  - exactly-once consumption
  - T0 event refs
  - RuntimeTask refs
- Background subagent completion flow 固定为：
  1. child finishes
  2. durable run terminal state written
  3. completion message enqueued into parent session mailbox
  4. if parent idle, scheduler starts one parent turn
  5. model sees `<task-notification>` 或 Hive-neutral equivalent as user-role/system-meta input
  6. message consumed once
- `check_subagent` 保留为 fallback/debug tool，不作为正常 wait path。
- 从 prompt 中删除 `consume_subagent_signals`，除非它被明确注册成 governed tool。更推荐不暴露 consume tool，像 CC/Codex 一样自动投递。

测试先行：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_subagent_completion_mailbox.py -q
pytest tests/services/test_subagent_wake_consumer.py -q
pytest tests/runtime/test_parent_turn_receives_subagent_notification.py -q
```

### P0-3：唯一 Agent Team Runtime Path

问题：

- API/workbench team creation 会持久化 team。
- LLM `team_create` 只是返回 persistence handoff。
- Prompt-facing team context 没有以 `AgentTeam` rows 为主。

必须修复：

- 抽出一个 `AgentTeamRuntimeService`。
- 以下入口全部调用同一个 service：
  - `team_create` model tool
  - `/commands/team_create/execute`
  - `/agent-teams` API
  - Plan Mode agent-team handoff
  - Session Workbench create team
- Prompt-facing team context 以 `AgentTeam` / `AgentTeamMember` 为 source of truth，再附加 runtime tasks/signals 作为 member state。
- 增加与 CC SendMessage 对齐的 by-name 或 `*` broadcast teammate message 语义。

测试先行：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/api/test_agent_teams.py -q
pytest tests/tools/test_team_create_tool_persists_team.py -q
pytest tests/services/test_agent_team_context.py -q
```

### P0-4：TurnEnvelope / PromptAssemblyManifest

问题：

- prompt/context assembly 组件层面可运行，但没有一个整体可审计 artifact。
- Codex 的 `TurnContext` 会让每个 turn 的 environment、permissions、source、tool surface、config 显式化。Hive 需要同等工程控制。

必须修复：

- 每次 invocation 生成 `TurnEnvelope`：
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
  - `active_tool_names`
  - `deferred_tool_names`
  - `skill_catalog_refs`
  - `memory_refs`
  - `team_mailbox_refs`
  - `prompt_sections`
  - `output_cap`
  - `trace/span ids`
- 持久化或在 Session Workbench 暴露 redacted copy。
- 不允许再只加 prompt glue 而不进入 manifest。

测试先行：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_turn_envelope_prompt_manifest.py -q
pytest tests/services/test_session_control_plane.py -q
```

## 6. P1 差距

### P1-1：Codex-style Session/Thread Control Surface

Hive 应吸收 Codex app-server control 思路：

- active turn snapshot
- pending interrupt
- pending rollback
- thread/session config snapshot
- resume/fork/archive/read/list
- goal state
- turn/item listing
- background terminal list/terminate

Hive 已有部分分布在：

```text
backend/app/api/chat_sessions.py
backend/app/services/session_control_plane.py
frontend/src/pages/session-workbench/*
```

缺口是 API shape 和 single state model。

目标：

- `GET /agents/{agent_id}/sessions/{session_id}/workbench` 成为 canonical read model。
- 增加或对齐：
  - session resume
  - session fork
  - session rollback
  - session active turn
  - session items/turns
  - background terminals
- 前端只消费这个统一模型，不再拼 scattered runtime fields。

### P1-2：Hooks Full Runtime Closure

目标：

- 每个 standard CC hook event 都必须有明确状态：
  - active blocking implementation
  - active observe-only implementation with reason
  - documented unsupported boundary
- standard CC event 不允许 silent disabled/noop。
- Workbench 能按 turn 展示 hook firings。

### P1-3：Skill/MCP Capsule Convergence

目标：

- Skill package、workflow skill、subagent definitions、MCP declared tools、scripts、templates、hooks 都属于一个 capability capsule contract。
- Loading skill 只通过 governed surfaces 改变 context/tool availability。
- tool/search/load events 进入 `TurnEnvelope`。

## 7. UI / UX 目标

UI 不应该暴露内部碎片化。它应该呈现一个 session lifecycle：

- Timeline：
  - user input
  - assistant output
  - tool calls
  - child agent notifications
  - workflow leaves
  - hooks
  - approvals
- Active turn：
  - model
  - runtime source
  - current status
  - tool wait
  - pending mailbox
  - cancellable/interruptible state
- Agent graph：
  - parent
  - subagents
  - teams
  - workflow leaves
  - delegated employees
- Context inspector：
  - prompt sections
  - memory refs
  - skills
  - deferred tools
  - permission profile
  - sandbox
- Background resources：
  - Background subagents 放在 Agent graph/mailbox。
  - Background terminals 放在 Exec/Terminal resources。
  - Autonomous triggers/heartbeat/dream 放在 Autonomy。
- Team panel：
  - team roster
  - member sessions
  - member task state
  - broadcast/send message
  - close/consolidate

前端已经有 Session Workbench 和 Team controls，所以这里主要是收敛和 state modeling，不是从零做新 UI。

## 8. 唯一路径决策

为了消除断点和臃肿，下一轮实现必须采用这些唯一性规则：

1. **一个 spawn verb。**
   模型可见 worker spawning 采用 CC-compatible AgentTool semantics。内部 service 可以继续叫 `spawn_subagent`，但 prompt/schema/default 必须对齐。
2. **一个 employee bridge。**
   `delegate_to_agent` 只表示 To Employee / A2A bridge，不再作为 session 内 worker spawn 的 primary verb。
3. **一个 completion bus。**
   child/teammate/workflow completion 进入 session mailbox/input queue，并 exactly once drain 到 parent turn。
4. **一个 team service。**
   tool、slash command、API、Plan Mode handoff、Workbench 都调用同一个 `AgentTeamRuntimeService`。
5. **一个 turn envelope。**
   每个 runtime source 都构建同一个 typed `TurnEnvelope`；prompt assembly 从它派生并产出 manifest。
6. **一个 workbench model。**
   前端读取一个 canonical session control-plane payload。
7. **fallback 必须显式。**
   `check_subagent`、raw signal reads、direct child session inspection 是 debug/fallback，不是正常 loop mechanics。

## 9. 实施顺序

### Pass A：AgentTool 与 Completion Bus

预计涉及文件：

```text
backend/app/tools/handlers/subagent.py
backend/app/agents/subagent.py
backend/app/services/subagent_run_service.py
backend/app/services/subagent_wake_consumer.py
backend/app/runtime/coordinator.py
backend/app/services/agent_tools.py
backend/tests/tools/test_agent_tool_cc_compat.py
backend/tests/runtime/test_coordinator_agenttool_visibility.py
backend/tests/services/test_subagent_completion_mailbox.py
```

验收标准：

- Coordinator 能看到并调用 canonical AgentTool-compatible spawn tool。
- `delegate_to_agent` 不再被 coordinator 当作默认 worker spawn path。
- 省略 `subagent_type` 会映射为 `general-purpose`。
- parent turn prompt 含 session worker when-to-use、few-shot、available agent types / whenToUse。
- async child completion 会作为 automatic model-visible input 出现在 parent session。
- prompt 不再引用非 tool 的 `consume_subagent_signals`。

### Pass B：Agent Team 收敛

预计涉及文件：

```text
backend/app/services/agent_team_runtime_service.py
backend/app/tools/handlers/command_parity.py
backend/app/api/commands.py
backend/app/api/agent_teams.py
backend/app/services/plan_mode_agent_team_handoff.py
backend/app/services/agent_team_context.py
backend/app/services/session_control_plane.py
frontend/src/pages/session-workbench/SessionNativeControls.tsx
```

验收标准：

- `team_create` tool call 通过同一 service 持久化。
- prompt-facing context 读取真实 teams。
- team member messages 和 idle/completion notices 进入同一个 mailbox bus。

### Pass C：TurnEnvelope / Workbench State

预计涉及文件：

```text
backend/app/runtime/turn_envelope.py
backend/app/runtime/invoker.py
backend/app/kernel/engine.py
backend/app/runtime/prompt_builder.py
backend/app/services/session_control_plane.py
frontend/src/api/domains/ccParity.ts
frontend/src/pages/session-workbench/*
```

验收标准：

- 每个 turn 暴露 redacted prompt/context manifest。
- Workbench 有一个 active-turn snapshot，包含 mailbox、tools、approvals、hooks、permissions、sandbox、runtime task refs。

### Pass D：Hooks / Skill / MCP Closure

预计涉及文件：

```text
backend/app/runtime/hooks.py
backend/app/runtime/hooks_setup.py
backend/app/services/plugin_hook_service.py
backend/app/tools/handlers/skills.py
backend/app/tools/handlers/mcp.py
backend/app/services/skill_*
backend/app/api/hooks.py
```

验收标准：

- 每个 standard hook 都有 active/observe/unsupported 明确状态。
- Skill/MCP load/call surfaces 都进入 `TurnEnvelope`。

## 10. 验证命令

文档交付校验：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main
test -f docs/ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md
rg -n "P0-1|P0-1.5|P0-2|To Session Worker|To Employee|TurnEnvelope|AgentTeamRuntimeService|consume_subagent_signals" docs/ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md
```

未来实现后的目标测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/tools/test_agent_tool_cc_compat.py \
  tests/runtime/test_coordinator_agenttool_visibility.py \
  tests/services/test_subagent_completion_mailbox.py \
  tests/tools/test_team_create_tool_persists_team.py \
  tests/runtime/test_turn_envelope_prompt_manifest.py \
  tests/services/test_session_control_plane.py \
  -q
```

完整回归目标：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run build
```

## 11. 最终评估

差距大小：**中高，但可控**。

为什么不是巨大差距：

- Hive 已有 durable runs、session graph、subagent runtime、team API、hooks、workbench、skill/MCP surfaces、prompt-cache-aware context assembly。

为什么不是小差距：

- CC 的关键行为不是“存在一个 subagent 工具”。
- 关键是主 Agent 如何自然决定 spawn、async result 如何回到同一个 session、模型如何无需 polling 或 hidden tool 就能看到 completion。
- 这条闭环目前仍然碎片化。

下一轮不应该新增产品概念，而应该把现有代码收敛成 CC/Codex-aligned lifecycle：

```text
User prompt
  -> TurnEnvelope
  -> PromptAssemblyManifest
  -> Model loop
  -> CC-compatible AgentTool / Team / Skill / MCP / Workflow tools
  -> RuntimeTask + T0 + InvocationSpan
  -> Session mailbox/input queue
  -> Parent wake / next turn
  -> Session Workbench
```

这是 Hive 在 session-middle multi-agent 工作上可信地自称 CC Plus 的最低形态。
