# CCPlus Session-Middle Parity Audit

日期：2026-06-24
状态：当前代码级复核结论；2026-06-24 已完成机制层复核和 CCPlus control-plane follow-up 闭环
范围：Plan Mode、Subagent、Hooks、Agent Team、Schedule/Trigger、Session Goal，以及 CC/FreeCode baseline + Codex thread/turn delta

## 0. 最终结论

按 `docs/agent-lifecycle-full-cc-parity-review-2026-06-22.md` 的范围，当前代码机制层面已经完成对齐。该文档 51 行之后的 P0/P1/P2 是原始 review 发现，当前状态应以前面的 `Implementation Closure` 和 `Turn Checkpoint Closure` 为准。

更准确的判断是：

1. Mechanism parity layer: 已对齐。覆盖 UserPromptSubmit、SessionStart/End、TurnStop/Abort、Stop/StopFailure、SubagentStart/Stop、Workflow preview/start binding、T0-first resume/checkpoint/rollback，以及 Plan Mode/Trigger/Goal 的核心 runtime handoff。
2. Detail/logic parity under that scope: 已由当前测试覆盖，不再应使用旧 review 的“缺口”判断。
3. CCPlus product/control-plane layer: 本轮已补齐统一 session workbench/export、hook management catalog/config、Agent Team workbench、JSON export/workbench 化。后续若继续深化 preview/trust/workspace policy，只能作为更高阶治理增强，不能反向判定机制层未对齐。
4. Codex 增益应继续吸收为统一操作面和可观察性，而不是替代 CC/FreeCode 机制层 baseline。

因此，当前状态是：

```text
Agent lifecycle mechanism parity: aligned for the documented scope.
CCPlus unified control plane: current follow-up scope completed.
```

## 1. 本轮判定标准

“全面对齐”不能按同名功能计算。必须按 full lifecycle 判断：

| Stage | 必须有的对齐面 |
| --- | --- |
| Definition | 能力定义、参与者身份、权限、上下文边界明确 |
| Accepted prompt | 输入被接受后先 durable append，再进入 model loop |
| Transcript | 每个 runtime source 有可回放 transcript / T0 事实层 |
| Model loop | 统一 invoke/kernel 路径，不绕过治理 |
| Tool loop | pre/post/failure hooks、capability gate、approval/action preflight 不绕过 |
| Hook boundary | 对应 CC hook event、schema、output、blocking/continuation 语义 |
| Runtime state | running / waiting / blocked / completed / failed / cancelled 可解释 |
| Resume | crash/restart 后可恢复、可失败闭环、可人工 reconciliation |
| Continuation | follow-up message / mailbox / interrupt / cancellation 不只是写日志 |
| Session close | turn/session boundary 清晰，close 不是正常 turn checkpoint 的替代 |
| Export | 最终能作为完整 JSON/T0 Session 保存和 replay |

Codex delta 的判定标准：

```text
thread/start
thread/resume
thread/fork
thread/read
thread/list
turn/start
turn/steer
turn/interrupt
typed runtime notifications
```

这些不是要求 Hive 复制 Codex API 名称，而是要求 Hive 的所有 agent work 都能映射到同等能力。

### 1.1 Detail / Logic Parity Rule

Hook 的严格标准也适用于 Plan Mode、Subagent、Agent Team、Schedule/Trigger 和 Goal。不能只看“功能入口是否存在”，必须看完整 runtime logic 是否等价：

1. Trigger timing: 什么时候进入该能力，是否在正确的 session/turn 边界触发。
2. Accepted input: 用户/agent 输入是否先 durable append，再进入 runtime，而不是只存在于临时对象或 API 参数。
3. Typed state: 是否有清晰的 typed state machine，而不是散落的 status string 和 metadata。
4. Runtime consumer: 事件、mailbox、handoff、approval、follow-up 是否有真实 consumer 驱动下一步运行。
5. Output/effect application: plan confirmation、subagent result、team message、trigger result、goal continuation 是否被下游正确消费。
6. Blocking / waiting / resume: 等待用户、权限、子任务、hook、trigger、goal budget 时，状态是否可解释、可恢复、可取消。
7. Continuation / interrupt: follow-up、steer、interrupt、cancel 是否改变活跃 runtime，而不是只写 transcript。
8. Replay / export: 是否能从 `ChatSession + T0 + RuntimeTask` 复原完整 JSON session。
9. Governance path: 是否始终经过 tool runtime、capability gate、approval/action preflight、hook boundary。
10. UI/control plane: 是否能被统一 session workbench/read/list/wait/interrupt/fork/export 控制，而不是每个功能有孤立 API。

按这个标准复核后，当前代码在 `agent-lifecycle-full-cc-parity-review` 覆盖的机制层已对齐；如果继续追求更完整的 CCPlus，应把剩余项命名为 control-plane/product-surface optimization，而不是 mechanism parity failure。

## 2. Baseline 证据

### 2.1 FreeCode / CC baseline

本轮用本地 FreeCode 作为 CC 第一参考：

```text
/Users/example-owner/vc-saas/free-code-main
```

关键 baseline：

1. Plan Mode exit 不只是“确认计划”。FreeCode 支持 clear context / keep context / permission mode / auto mode / bypass permissions / manual approve / team hint；其中 `auto mode`、`bypass permissions`、`accept edits` 这类产品标签不逐字复刻，Hive 只映射其底层语义：确认后的上下文策略、治理安全的 approval/policy mode、执行 handoff、team handoff。本轮不把 CC 专属远程高级计划入口纳入范围：
   - `/Users/example-owner/vc-saas/free-code-main/src/components/permissions/ExitPlanModePermissionRequest/ExitPlanModePermissionRequest.tsx`
2. Hooks 不是单一 event bus。FreeCode schema 包含完整事件和 hook-specific outputs。Hive 的目标是对齐全部 CC hook semantic surface：事件、typed input、hook-specific output、runtime consumer、可观察执行记录都要有。某些 hook 可以在当前部署形态下默认不触发或被 policy disabled，但不能缺 schema/契约/映射。
   - `PreToolUse`
   - `PostToolUse`
   - `PostToolUseFailure`
   - `Notification`
   - `UserPromptSubmit`
   - `SessionStart`
   - `SessionEnd`
   - `Stop`
   - `StopFailure`
   - `SubagentStart`
   - `SubagentStop`
   - `PreCompact`
   - `PostCompact`
   - `PermissionRequest`
   - `PermissionDenied`
   - `Setup`
   - `TeammateIdle`
   - `TaskCreated`
   - `TaskCompleted`
   - `Elicitation`
   - `ElicitationResult`
   - `ConfigChange`
   - `WorktreeCreate`
   - `WorktreeRemove`
   - `InstructionsLoaded`
   - `CwdChanged`
   - `FileChanged`
   - `/Users/example-owner/vc-saas/free-code-main/src/entrypoints/sdk/coreSchemas.ts`
3. Agent Team 不是静态索引。FreeCode team create 会写 team file、lead session、task list，并把 team context 放进 AppState；local agent task 有 pending message queue、drain、foreground/background、abort/background signal。Hive 应对齐的是 team session、member mailbox、running/waiting/blocked/cancel lifecycle；FreeCode 的 React state 和 `AbortController` 机制是本地实现细节，不作为 Hive 复刻目标。
   - `/Users/example-owner/vc-saas/free-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts`
   - `/Users/example-owner/vc-saas/free-code-main/src/tasks/LocalAgentTask/LocalAgentTask.tsx`

### 2.2 Codex delta

Codex 的可吸收优势不是替代 CC baseline，而是给 Hive 加统一 thread/turn 操作面：

```text
/Users/example-owner/Context Engineering/codex/codex-rs/docs/codex_mcp_interface.md
```

Codex v2 RPC 重点：

- `thread/start`
- `thread/resume`
- `thread/fork`
- `thread/read`
- `thread/list`
- `turn/start`
- `turn/steer`
- `turn/interrupt`
- typed notifications

Hive 要做 CCPlus，应该把这些能力融入自己的 Session/T0/RuntimeTask 模型，而不是把每个功能继续做成孤立 endpoint。

### 2.3 Scope guard

为避免重犯把 CC 产品专属能力当成 Hive parity gap 的错误，本轮审计按下面规则收口：

```text
Required parity = session-middle semantic equivalent.
Not required = product label, vendor-hosted service, local CLI UI state, or exact implementation mechanism.
```

明确不做 exact-copy 的项：

- CC 专属远程高级计划入口不纳入本轮；只对齐 Plan Mode 本身。
- `auto mode`、`bypass permissions`、`accept edits` 是 CC 产品标签；Hive 只映射为受治理约束的 approval/policy mode。
- Hook 这一层不按“当前是否常用”裁剪。CC 27 个 hook 都应进入 Hive runtime parity target；可以默认不用、按 runtime/policy disabled、或只有在对应能力存在时触发，但不能缺事件契约、typed payload、output schema、审计记录和 Hive semantic mapping。
- `Setup` 是必需的 semantic parity item，但 Hive 映射为 config/trust/onboarding lifecycle hook，不照抄 local trust dialog、keybinding setup、terminal setup。
- `Elicitation` / `ElicitationResult` 是必需的 semantic parity item，因为 MCP user-input flow 本质是 Agent Session 中的受管交互；如果某个 deployment 当前未启用对应 runtime，也应保留 contract / audit / future-enable path，不降级为 out-of-scope。
- `Notification`、`ConfigChange`、`InstructionsLoaded`、`PermissionDenied`、`PostToolUseFailure`、`TaskCreated`、`TaskCompleted`、`TeammateIdle` 都是非纯 coding hook，必须纳入 Hook parity。
- `WorktreeCreate` / `WorktreeRemove`、`CwdChanged` / `FileChanged` 也要纳入 Hook parity。Hive 云端也有 workspace，所以这些事件必须映射为 Hive workspace semantic equivalent；只是不能照抄 CC 本地 CLI 的 path/watch UI 机制。`CwdChanged` 可映射为 workspace context/root/subpath change，`FileChanged` 可映射为 workspace artifact/file mutation，`WorktreeCreate/Remove` 可映射为 workspace branch/fork/session workspace lifecycle。即使某个 runtime 暂时不用，也应有 disabled/no-op capable contract。
- FreeCode `LocalAgentTask` 的 React state、foreground/background UI、`AbortController` 不是 parity 目标；Hive 要做的是 session state、mailbox drain、cancellation intent、runtime cancel 的语义等价。
- Plan Mode V2 的 subscription/rate-limit gated agent count、experiment flag、`Opus Plan Mode` / model-specific tips 属于 CC 产品和供应商配置；Hive 如果做多代理 planning，只能落到自己的 budget/governance/team-session policy，不按 CC 配额或模型标签复刻。
- FreeCode reconstructed build 中 disabled/unavailable 的 `VerifyPlanExecutionTool` 不能作为当前 Hive 缺口；只有真实 baseline 存在可执行语义时才重新纳入审计。
- Codex `thread/turn` API 名字不复制；只吸收统一 session/turn/read/wait/fork/interrupt/export 的内部服务形态。

## 3. 当前 Hive 代码事实

### 3.1 Plan Mode

当前已具备：

- `exit_plan_mode` 生成 confirmable plan card，不直接执行。
- 如果 runtime provisioned exact plan file，`exit_plan_mode` 会读取该文件作为 trusted plan body。
- `plan_markdown` 是主产物，structured fields 是 governance extraction。
- `ask_user_question` 是 clarification path，不是 approval。
- `request_plan_mode` 只是请求进入 Plan Mode，不会从 tool result 自动激活。
- `Plan Mode` readonly policy 允许 read/search/load_skill/tool_search/preview_workflow/check_subagent/work ledger planning aids。
- `spawn_subagent` 在 Plan Mode 中只允许同步 inline `explorer` / `critic`，禁止 worker/background/definition/ledger ownership。
- reminder scheduler 以 transient injection 方式注入 Plan Mode reminders，不污染 transcript/memory。
- `advanced_plan` 存在，以 `runtime_task_type="advanced_plan"` 走 durable chat runtime；它是 Hive 现有任务类型，不等同于 CC 专属远程高级计划入口，也不是本轮 Plan Mode parity 的硬要求。

关键代码：

- `backend/app/tools/handlers/plan_mode.py`
- `backend/app/tools/plan_mode_policy.py`
- `backend/app/kernel/reminder_scheduler.py`
- `backend/app/api/advanced_plan.py`

判断：

```text
Plan Mode mechanism parity is aligned for the current documented scope.
The previously listed CCPlus control-plane follow-up scope is now implemented at API/workbench/export level.
```

### 3.2 Subagent

当前已具备：

- `spawn_subagent` 是 lightweight worker 入口；peer digital employee delegation 仍由 `delegate_to_agent` 表达。
- 公开 built-in types：`general-purpose`、`explorer`、`critic`；历史 `worker` 值只作为兼容 alias 归一到 `general-purpose`。
- persistent `definition_name` 支持 agent-scope first, tenant shared library second。
- `run_in_background` 已暴露到 schema。
- background spawn 先创建 durable `RuntimeTask(task_type="subagent")`，再启动 worker。
- background spawn 返回 `run_id` 和 `child_session_id`。
- `create_subagent_child_session` 会创建 `ChatSession(session_kind="subagent", runtime_source="subagent")`。
- child session 会 append `subagent_task_started`。
- completion 会更新 RuntimeTask，并 append `subagent_task_completed` / `subagent_task_failed`。
- `check_subagent` 能读 run status、child session refs、continuation contract。
- restart resume 对 explorer/critic replay-safe；worker/mutating lane 进入 `needs_reconciliation`，不盲目重放副作用。
- completion wake 会唤醒 parent agent，并把 wake 写入 parent T0。

关键代码：

- `backend/app/tools/handlers/subagent.py`
- `backend/app/services/subagent_run_service.py`
- `backend/app/agents/subagent.py`
- `backend/app/services/subagent_wake_consumer.py`

控制面后续项：

1. 把 child session read/list/wait/interrupt/close/fork 收敛到统一 session/turn operation surface。
2. 把 long-lived Agent-Agent conversational continuation 做成统一 turn/steer API，而不是散在具体工具里。
3. Codex-style `last_n` 可以作为可选 context fork policy，但不是当前 lifecycle parity blocker。

判断：

```text
Subagent lifecycle mechanism parity is aligned for the current documented scope.
Long-lived A2A session controls now have the same session/workbench/export control-plane surface; deeper live steering remains enhancement work.
```

### 3.3 Hooks

当前已具备：

- `HookEvent` 有 tool lifecycle、session lifecycle、compaction、delegation、trigger/heartbeat/dream、memory extracted、notification。
- 近期补了部分 FreeCode parity names：
  - `PERMISSION_REQUEST`
  - `TASK_CREATED`
  - `TASK_COMPLETED`
  - `ELICITATION`
  - `CONFIG_CHANGE`
  - `INSTRUCTIONS_LOADED`
  - `WORKSPACE_CONTEXT_CHANGED`
  - `ARTIFACT_CHANGED`
  - `TEAM_CREATED`
  - `TEAM_CLOSED`
  - `TEAMMATE_IDLE`
- `HookResult` 支持 `block`、`modified_args`、`additional_contexts`、`prevent_continuation`。
- registry 支持 handler registration、matcher、timeout、failure policy。

关键代码：

- `backend/app/runtime/hooks.py`
- `backend/app/runtime/hooks_setup.py`
- `backend/tests/runtime/test_hooks_cc_parity.py`
- `backend/tests/runtime/test_hooks.py`

控制面后续项：

1. 当前 `agent-lifecycle-full-cc-parity-review` 覆盖的 core lifecycle hooks 已对齐：UserPromptSubmit、SessionStart/End、TurnStop/Abort、Stop/StopFailure、SubagentStart/Stop。
2. CC 27 hook semantic list 仍是长期 runtime target，但剩余项应归类为 hook registry / workspace / governance productization，而不是当前 lifecycle closure 未完成。
3. 后续可继续把 `HookContext` 细化为 event-specific typed payload，并增加 Codex-style hook preview/list/trust/config-layer 管理。

判断：

```text
Hook mechanism parity is aligned for the current documented lifecycle scope.
CCPlus hook management now exposes event catalog, registered hooks, runtime config, and UI enable/disable controls; deeper trust/hash/preview remains hardening work.
```

### 3.4 Agent Team

当前已具备：

- `AgentTeam` / `AgentTeamMember` / `AgentTeamEvent` models。
- `AgentTeam.parent_session_id` 指向 lead session。
- member 必有 `chat_session_id`。
- member session 创建时使用：
  - `source_channel="agent_team"`
  - `session_kind="team_member"`
  - `runtime_source="team_member"`
  - `visibility_scope="team"`
- `create_agent_team` 会创建 team 和 member sessions。
- `enter_agent_team_member` 返回 `chat_session_id`、`runtime_task_id`、`runtime_task_type`、`status`。
- `close_agent_team` 会关闭 team/member，并给出 consolidation plan。
- `TEAM_CREATED`、`TEAM_CLOSED`、`PERMISSION_REQUEST`、`TEAMMATE_IDLE` 事件有部分 emit。
- `agent_team_context.py` 能把 RuntimeTask/CoordinationSignal 渲染为 prompt-facing team context/mailbox。

关键代码：

- `backend/app/models/agent_team.py`
- `backend/app/api/agent_teams.py`
- `backend/app/api/commands.py`
- `backend/app/services/team_runtime.py`
- `backend/app/services/agent_team_context.py`

控制面后续项：

1. team/member session、events、prompt-facing team context 已具备当前机制层所需基础。
2. live teammate workbench、member runtime controls、mailbox UI、consolidation UI 可作为 CCPlus 产品化后续。
3. CC `LocalAgentTask` 的 background signal / abort controller 是本地实现机制，不作为 exact-copy 目标；Hive 应保持 session/runtime/control-plane 语义等价。

判断：

```text
Agent Team context/event mechanism is aligned for the current documented scope.
Agent Team now has a workbench surface for team/member/event summaries; deeper live mailbox and consolidation editing remain product enhancements.
```

### 3.5 Schedule / Trigger

当前已具备：

- `schedules.py` 明确是 legacy schedules API backed by `AgentTrigger` cron wake policies。
- create/update/manual run 都有 Plan Mode gate，用 confirmed plan 或 declined recommendation exemption。
- `trigger_daemon` 会把 fired triggers group by agent。
- 每组 trigger 会创建 `RuntimeTask`。
- trigger run 会创建 `ChatSession(session_kind="trigger_run", runtime_source="trigger")`。
- trigger context 作为 `user_message` append 到 session。
- tool call/tool result 会 append 到 trigger session。
- assistant final 会 append 到 trigger session。
- runtime task 会绑定 child session。
- `TRIGGER_END` hook 会 emit。

关键代码：

- `backend/app/api/schedules.py`
- `backend/app/services/trigger_daemon.py`
- `backend/app/services/plan_mode_handoff.py`

控制面后续项：

1. Schedule 仍是 legacy surface，真实语义是 trigger wake policy。
2. Trigger run 已 sessionized；后续应统一到 session/turn API：
   - read
   - list
   - interrupt
   - fork
   - steer
3. trigger/schedule 与 Goal 的关系已经拆清楚：schedule is wake policy, not objective；后续是统一 Session control plane。

判断：

```text
Schedule/Trigger mechanism parity is aligned for trigger runtime scope.
Unified thread/turn operations now have a session workbench/export aggregation surface; wait/interrupt/steer can continue to deepen on top of the same control-plane model.
```

### 3.6 Session Goal

当前已具备：

- `AgentSessionGoal` 是 session-scoped model。
- active goal 对同一 agent/session 有唯一约束。
- `GoalStatus` 包含 active/paused/blocked/complete/usage_limited/budget_limited/cancelled。
- `should_continue_goal` 会阻止：
  - 非 active
  - Plan Mode
  - ephemeral session
  - pending user input
  - active run exists
  - token budget exhausted
  - continuation turn cap reached
- `continue_session_goal` 通过 `start_web_chat_run(... runtime_task_type="goal_continuation")` 继续，不绕过 web chat runtime。
- `maybe_continue_session_goal_after_turn` 只在 normal `web_chat_turn` completed 后触发，且阻止 recursive `goal_continuation`。
- command path 可以 create/update/stop session goal。

关键代码：

- `backend/app/models/agent_session_goal.py`
- `backend/app/services/session_goal_runtime.py`
- `backend/app/services/goal_continuation_service.py`
- `backend/app/api/session_goals.py`
- `backend/app/api/commands.py`

控制面后续项：

1. Goal continuation 已是 session-local governed loop。
2. 后续应统一 goal command/API persist surface。
3. 后续补 status notification stream。
4. 后续补完整 JSON session export，包含 goal state + continuation run lineage + transcript refs。

判断：

```text
Goal is a useful Codex-inspired addition.
Goal continuation mechanism parity is aligned for the current documented scope.
Unified thread/turn/session control API now has a concrete session workbench/export surface.
```

## 4. 汇总矩阵

### 4.1 Mechanism Parity Matrix

按当前代码和定向测试复核，机制层结论如下：

| 能力 | 机制层状态 | 当前已验证的逻辑 |
| --- | --- | --- |
| Plan Mode | Aligned for current mechanism scope | explicit Plan Mode state、read-only planning boundary、exact plan file、clarification path、Plan Mode reminders、confirmed plan handoff/provenance、tool-intercept 不自动进入 Plan Mode。 |
| Subagent | Aligned for lifecycle scope | background `RuntimeTask`、child `ChatSession`、T0 sidechain、SUBAGENT_START/SUBAGENT_STOP、completion wake、restart replay/reconciliation、subagent wake consumer。 |
| Hooks | Aligned for agent-lifecycle review scope | USER_PROMPT_SUBMIT -> SESSION_START -> kernel -> SESSION_END -> TURN_STOP，TURN_ABORT，STOP/STOP_FAILURE，SUBAGENT_START/SUBAGENT_STOP，blocking Stop continuation，hook additional context injection。 |
| Agent Team | Aligned for current context/event scope | team/member `ChatSession` shell、team events、prompt-facing team context/mailbox rendering、TEAM_CREATED/TEAM_CLOSED/TEAMMATE_IDLE event path。 |
| Schedule/Trigger | Aligned for trigger runtime scope | Plan gate/provenance、confirmed-plan trigger handoff、trigger daemon sessionization、`trigger_run` transcript、RuntimeTask binding、trigger context/result handling。 |
| Goal | Aligned for continuation scope | `AgentSessionGoal`、GoalStatus guards、Plan Mode/pending/budget/active-run blockers、`goal_continuation` 走 `start_web_chat_run`，避免 recursive continuation。 |

### 4.2 CCPlus Control-Plane Closure

下面这些曾是机制层之上的 CCPlus follow-up。本轮已按 API/workbench/export 层闭环：

| 面向 | 当前闭环 |
| --- | --- |
| Unified session/turn API | 新增 `build_session_workbench` / `build_session_json_export`，通过 `/agents/{agent_id}/sessions/{session_id}/workbench` 和 `/export` 聚合 `ChatSession + T0-first transcript + RuntimeTask + Goal + Agent Team`。 |
| Hook management | `/agents/{agent_id}/hooks` 返回完整 event catalog、registered events、runtime config；`PATCH /agents/{agent_id}/hooks/{hook_key}` 保留 per-agent enable/disable/timeout/failure policy，前端侧栏已接入 enable/disable。 |
| Agent Team productization | 新增 `/agents/{agent_id}/agent-teams/{team_id}/workbench`，聚合 team、members、events、member summaries、T0 refs、enter links；前端侧栏可查看 team workbench 摘要。 |
| JSON export / replay | `/sessions/{session_id}/export` 输出 `hive.ccplus.session_export.v1`，包含 session workbench、truth_source、transcript events、runtime lineage、goal/team metadata。 |
| Workspace hook policy | Hook catalog 已覆盖 workspace 类事件的 control-plane 可见性；更细的 trusted hash、preview/run、workspace policy 是治理增强，不再是当前 CCPlus follow-up 阻塞项。 |

因此，现在不能再写“这些能力细节/逻辑没有对齐”。正确口径是：

```text
Mechanism layer: aligned for the documented lifecycle scope.
CCPlus control plane: session workbench/export, hook management, Agent Team workbench, and JSON export are implemented.
```

## 5. CCPlus 目标架构

### 5.1 统一 Agent Session

所有交互最终都应该收敛到：

```text
AgentSession
  id
  kind: human_chat | agent_chat | subagent | team_member | trigger_run | goal_continuation | workflow_run | local_agent_channel
  participants
  parent_session_id
  root_session_id
  state: open | running | waiting | blocked | completed | failed | cancelled
  transcript: append-only events
  active_turn_id
  active_runtime_task_id
  governance_context
  memory_scope
  visibility_scope
  export_refs
```

当前 `ChatSession` 已经有大量字段接近这个目标，不需要另造一个并行事实层。应优先把 `ChatSession + T0 events + RuntimeTask` 的关系收敛成硬 contract。

### 5.2 统一 Turn API

Hive 不一定复制 Codex API 名称，但需要等价能力：

```text
session/start
session/resume
session/fork
session/read
session/list
turn/start
turn/steer
turn/interrupt
turn/wait
turn/close
```

映射：

| Codex | Hive equivalent |
| --- | --- |
| `thread/start` | create/open `ChatSession` |
| `thread/resume` | resume session from T0/DB transcript |
| `thread/fork` | create child/fork session with lineage |
| `thread/read` | read transcript/events/state |
| `thread/list` | list sessions by agent/owner/source |
| `turn/start` | create `RuntimeTask` + append accepted input |
| `turn/steer` | append mailbox/follow-up event and notify active runtime |
| `turn/interrupt` | cancellation intent + runtime cancellation + transcript event |

### 5.3 RuntimeTask 位置

`RuntimeTask` 不是协作本体。它只表示某个 session 中的一次 execution run。

正确关系：

```text
ChatSession / T0 transcript = truth
RuntimeTask = active or historical run record
CoordinationSignal = delivery/notification/read model
AgentTeam = control index
```

禁止让 `RuntimeTask.result_summary`、notification、workspace-only artifact 成为唯一事实层。

### 5.4 Unified child-session continuation

当前 subagent lifecycle / completion wake 已经在机制层闭环。CCPlus 后续如果要把 child session 做成长期 Agent-Agent 对话，应收敛到统一 turn/steer API，目标闭环是：

```text
send_agent_session_message
  -> append agent_session_message event
  -> if child runtime active:
       deliver steer/interrupt signal
     else if child session open:
       enqueue next child turn
     else:
       return terminal_session_cannot_continue
  -> child run consumes mailbox
  -> child assistant/tool events append to child session
  -> parent wake/notification if needed
```

这属于 long-lived A2A session control-plane 能力，不反向影响当前 subagent lifecycle parity closure。

### 5.5 Agent Team runtime

Agent Team 的完整能力应包含：

1. team create creates lead/member sessions。
2. member can be started as a real runtime turn。
3. member mailbox can be appended and drained。
4. lead can send message to member by member id/name/session id。
5. member can become running/waiting/blocked/completed。
6. lead can close team and consolidate member outputs with T0 refs。
7. team supports background/foreground-like mode semantics where applicable。
8. teammate idle/permission/task hooks are runtime triggered, not only API event append。

### 5.6 Hook management parity

当前 agent-lifecycle scope 的核心 hook 机制已经 closure。CCPlus 后续的 hook management 应继续按三层做强：

1. Event name parity。
2. Event-specific input schema parity。
3. Hook-specific output semantics parity。

Required CC semantic hook set：

```text
PreToolUse
PostToolUse
PostToolUseFailure
Notification
PermissionRequest
PermissionDenied
UserPromptSubmit
SessionStart
SessionEnd
Stop
StopFailure
SubagentStart
SubagentStop
PreCompact
PostCompact
Setup
ConfigChange
InstructionsLoaded
TaskCreated
TaskCompleted
TeammateIdle
Elicitation
ElicitationResult
WorktreeCreate
WorktreeRemove
CwdChanged
FileChanged
```

Required CC output/effect set：

```text
PreToolUse decision block/approve + updated input + additional context
PostToolUse additional context + MCP/tool output rewrite where governed
PostToolUseFailure additional context / recovery signal
PermissionRequest decision allow/deny
PermissionDenied retry/replan signal
UserPromptSubmit block/add context
SessionStart additional context + initial message/workspace watch setup
Stop prevent continuation
SubagentStart block/add context
Setup config/trust/onboarding context
Elicitation accept/decline/cancel
ElicitationResult observe/override response before MCP server receives it
ConfigChange/InstructionsLoaded context refresh
TaskCreated/TaskCompleted runtime effects
TeammateIdle wake/signal effects
WorktreeCreate/WorktreeRemove workspace branch/fork/session workspace lifecycle
CwdChanged/FileChanged workspace context/artifact mutation watch effects
```

Hook management rule:

Hook management parity is not complete when event names exist. Each hook should also align on the detailed runtime logic:

- Trigger point: when the hook fires in the lifecycle, and whether it fires before/after model input, tool execution, permission resolution, compaction, session resume/stop, subagent delegation, workspace mutation, or user elicitation.
- Matcher semantics: which runtime fields may select handlers. Examples: tool name for tool hooks, source for session-start hooks, trigger type for compaction hooks, subagent kind for subagent hooks, and workspace path/pattern for workspace hooks.
- Typed input payload: each hook must expose an event-specific typed request, not only a generic metadata bucket.
- Universal output contract: support the CC-style shared outputs such as continue/stop, suppress output, stop reason, decision, system message, reason, sync/async mode, and timeout where applicable.
- Hook-specific output contract: support the CC-style per-event outputs, including input rewrite, output rewrite, additional context injection, permission allow/deny/update, retry, elicitation action/content, watch paths, and worktree/workspace path handoff.
- Runtime consumer: every output field must have a real caller that consumes it. A schema field that is never applied is not parity.
- Blocking and rewrite behavior: blocking must stop the correct runtime step; rewritten input/output must be the value seen by the following step; injected context must reach the model at the same semantic boundary.
- Evidence and replay: every hook run must be recorded as trace evidence with enough payload/result data to replay or audit the decision.
- Disabled/no-op behavior: a hook may be disabled by policy, feature flag, or workspace runtime, but its contract, audit shape, and future enablement path must still exist.

Current Hive evidence after the 2026-06-24 follow-up closure:

- `runtime/hooks.py` has `HookEvent` and `HookRegistry`.
- Core lifecycle scope is wired and tested: `USER_PROMPT_SUBMIT`, `SESSION_START`, `SESSION_END`, `TURN_STOP`, `TURN_ABORT`, `STOP`, `STOP_FAILURE`, `SUBAGENT_START`, `SUBAGENT_STOP`.
- Blocking support is explicit for the lifecycle events that need blocking in current scope: `PRE_TOOL_USE`, `USER_PROMPT_SUBMIT`, `STOP`, `SUBAGENT_START`, `SUBAGENT_STOP`.
- `PRE_TOOL_USE` can apply `modified_args`.
- `USER_PROMPT_SUBMIT` additional context is consumed by the invoker.
- `STOP` blocking result is consumed by the kernel and forces continuation.
- `SUBAGENT_START` / `SUBAGENT_STOP` are emitted around child invoke and carry transcript-path semantics.
- Broader CC semantic hook events are present in the hook catalog, including `PERMISSION_DENIED`, `ELICITATION_RESULT`, `SETUP`, `WORKTREE_CREATE`, `WORKTREE_REMOVE`, `CWD_CHANGED`, and `FILE_CHANGED`.
- Hook catalog entries now expose lifecycle state (`active` / `active_observe` / `disabled_noop`), trigger point, matcher fields, typed input schema, typed output schema, and runtime consumer.
- Workspace/setup hooks that should exist but not run in the current cloud runtime are represented as `disabled_noop` contracts with audit-compatible schemas rather than missing event names.
- Agent Team runtime controls are wired through durable `team_member` runs and mailbox continuation, not only manual event append.

Interpretation:

- Hive's lifecycle hook mechanism remains aligned for the documented scope.
- CCPlus hook management now has a concrete contract surface for the broader CC semantic hook set; active hooks name their runtime consumer, and disabled/no-op hooks have an explicit future enablement/audit contract.
- Remaining hook work after this closure is product/governance depth (richer UI, trust approval workflow, handler authoring), not a missing lifecycle/runtime contract.

### 5.7 Codex Hook Advantages to Absorb

Codex is not the hook baseline because its current public hook surface is smaller than CC: `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SessionStart`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, and `Stop`. These 10 events should not replace the CC 27-event target. However, Codex has several implementation qualities worth absorbing into Hive as CC Plus improvements:

1. Typed request/outcome objects per event

   Codex models hook calls as event-specific request and outcome types instead of a loose metadata map. Hive should adopt the same shape for all CC hooks: one typed input and one typed output per hook event, with a shared envelope only for common fields.

2. Preview/run split

   Codex separates hook preview/listing from actual execution. Hive should expose the same lifecycle so the web UI, admin console, and cloud workspace can show which hooks will run before they mutate or block anything.

3. Hook discovery with warnings

   Codex can list configured hooks and surface warnings. Hive should add an equivalent hook registry inspection API for tenant/admin/debug surfaces: resolved handler list, matcher, source layer, enabled state, trust state, and validation warnings.

4. Trust state per handler

   Codex tracks per-hook `enabled` and `trusted_hash`. Hive should adapt this to enterprise governance: hook handler identity, content hash, approval state, owner/tenant scope, last reviewer, and revocation metadata.

5. Config layer discipline

   Codex distinguishes configuration layers and avoids letting every layer mutate trusted state. Hive should map this into tenant/org/user/session/workspace layers: managed and project layers may declare hooks, but only authorized user/org control surfaces may approve trust and enablement.

6. Matcher groups and alias dedupe

   Codex resolves event matchers and deduplicates aliases so the same handler does not run twice because two matcher names point to the same semantic target. Hive should implement equivalent dedupe for tools, MCP tools, workflow steps, workspace paths, and subagent kinds.

7. Concurrent execution with stable reporting order

   Codex can run handlers concurrently while presenting results in configured order. Hive should use the same pattern where effects are merge-safe: execute independent handlers concurrently, preserve deterministic configured-order reporting, and define explicit conflict resolution for blocking/rewrite outputs.

8. Thread scope vs turn scope

   Codex distinguishes hooks that belong to a full thread/session from hooks that belong to one turn. Hive should encode hook scope explicitly: session/thread hooks, turn hooks, task hooks, subagent-session hooks, workflow-run hooks, and workspace hooks.

9. Handler execution metadata

   Codex handler config includes timeout, async execution, status message, and platform-specific command variants. Hive should generalize this into cloud-safe handler metadata: timeout, async policy, status text, retry policy, sandbox/workspace binding, and allowed execution provider.

10. UI-oriented hook descriptions

    Codex has user-facing hook descriptions in its UI layer. Hive should expose the same clarity in the control plane: every hook should have a short purpose, trigger timing, risk level, allowed effects, and current enabled/trusted state.

The resulting target is: CC hook semantic breadth plus Codex hook-engine discipline. CC defines what must exist; Codex contributes how to make it inspectable, trusted, deterministic, and manageable in a product UI.

## 6. CCPlus Follow-Up 优先级

### P0 - Unified Agent Session / Turn control API

原因：这是融合 Codex 优势的主轴。机制层已经对齐后，下一步不是继续说某个能力“没对齐”，而是把 human chat、subagent、team member、trigger run、goal continuation、workflow run 都放到统一 session/turn/read/wait/interrupt/fork/export 操作面上。

完成判据：

- `ChatSession` state/read/export contract 明确。
- `RuntimeTask` 统一绑定 session/turn。
- session read/list/fork/resume/interrupt/wait 有统一内部服务。
- Web/API/tool surfaces 复用同一服务，而不是各写一套。
- JSON export 包含 T0 refs、runtime lineage、goal/trigger/team metadata。

### P1 - Hook management hardening

原因：核心 lifecycle hooks 已对齐；下一步是把 broader hook surface 做成可配置、可审计、可禁用、可解释的控制面能力。

完成判据：

- FreeCode broader hook events 在 Hive 中有明确 semantic equivalent 或 disabled/no-op capable contract。
- 每个 event 有 typed input payload、trigger-point contract、matcher semantics。
- hook-specific outputs 有 schema 和 runtime consumer。
- 吸收 Codex hook engine 优势：preview/run split、hook listing with warnings、handler trust state、config-layer resolution、matcher alias dedupe、thread-vs-turn scope、timeout/async metadata、deterministic reporting order、cloud-safe execution metadata。

### P2 - Long-lived A2A / Subagent continuation controls

原因：subagent lifecycle 已闭环；如果要把 child session 做成长期 Agent-Agent session，需要统一 continuation controls。

完成判据：

- `send_agent_session_message` 有 consumer。
- active run 能 steer/interrupt。
- inactive open child session 能启动 continuation turn。
- terminal child session 拒绝 continuation，并写 transcript。
- tests 覆盖 queued/running/completed/failed 四种状态。

### P3 - Agent Team product controls

原因：team context/events 机制已存在；下一步是把它产品化成 live teammate workbench 和统一 runtime controls。

完成判据：

- team member runtime can start。
- member mailbox can drain。
- team events are runtime-driven。
- close/consolidation reads member session/T0 refs。
- plan/team hint 能从 Plan Mode handoff 到真实 team runtime。

### P4 - Plan Mode UX / policy polish

原因：Plan Mode 机制层已对齐；后续是把 confirmed plan 后的策略、团队提示、执行绑定和用户可见 artifact 做得更清晰。

Hive 需要补齐的是：

1. confirmed plan 的 clear/keep context 策略。
2. confirmed plan 后的 Hive approval/policy mode 映射。
3. plan 中 team hint 到真实 Agent Team runtime 的 handoff。
4. confirmed plan -> execution turn/session binding。
5. plan artifact 与 machine execution contract 分层，避免把内部工具路径暴露给用户可见计划。

完成判据：

- clear/keep context policy 有 Hive equivalent。
- approval/policy mode / team hint 有明确 Hive mapping。
- confirmed plan -> execution turn/session binding 在统一 session control plane 中可追踪。

### P5 - Schedule/Trigger/Goal workbench integration

原因：Schedule/Trigger/Goal 的 runtime 机制已 sessionized；后续是统一 workbench/read/export/control。

完成判据：

- trigger/goal continuation 可以通过 session read/list/wait/interrupt 查看和控制。
- schedule manual run / triggered run 都可打开同一 Session Workbench。
- JSON export 包含 goal/trigger metadata、runtime task lineage、T0 refs。

### 6.1 Follow-Up Closure — 2026-06-24

The P0-P5 follow-up criteria above are now implemented at the code-mechanism layer:

- P0 Session/Turn: `session_control_plane.py`, `session_command_runtime.py`, and chat session APIs expose read/export/fork/resume/interrupt/steer/workbench surfaces over the same Session/T0/RuntimeTask substrate.
- P1 Hooks: `runtime/hooks.py` exposes the broader CC semantic hook catalog with typed input/output schemas, trigger points, matcher fields, lifecycle state, and runtime consumer. Workspace/setup hooks are explicit disabled/no-op contracts when they should not currently execute.
- P2 A2A/Subagent continuation: `agent_session_continuation.py` gives `send_agent_session_message` a real consumer. Active child sessions queue into mid-run drain, inactive open sessions start continuation turns, and terminal sessions reject continuation while writing transcript evidence.
- P3 Agent Team: `agent_teams.py` can start `team_member` durable runs, send member mailbox messages through the same continuation consumer, write runtime-driven team events, and close with member session/T0 refs.
- P4 Plan Mode: confirmed current-session execution remains bound to durable turns. The hidden `execution_contract.type="agent_team"` route is a CCPlus/Hive-native composition of CC Plan Mode approval + CC teammate delegation semantics: it creates real Agent Team member sessions instead of staying as a textual hint, but it must not be described as raw CC Plan Mode behavior.
- P5 Schedule/Goal: goal continuation and trigger/session workbench/export integration remain covered by the existing runtime task/session control plane tests.

## 7. 不应混淆的边界

### 7.1 Memory / Iter 是 Hive-native delta

Memory、T0/T2/T3、soul、Skill evolution、Iter 不是要降级成 CC 行为。它们是 Hive 的非对标增量。

但是不能用 Memory/Iter 的强项替代 session-middle 机制层和控制面。CCPlus 的顺序是：

```text
CC lifecycle parity first
Codex thread/turn delta second
Hive Memory/Iter/control-plane third
```

### 7.2 Subagent 不是 Full Digital Employee

`spawn_subagent` 是 lightweight worker，不应该拥有完整 digital employee identity、soul、dream、长期 agent identity。

Full digital employee / peer A2A delegation 应该走 standalone agent/session relationship，不要和 lightweight subagent 混。

### 7.3 Agent Team 不是普通 Subagent

Agent Team member 是可进入、可追问、可汇总的 member session。它比一次性 subagent 更接近 CC teammate。

因此 Agent Team 的目标不是“多个 subagent 并行”，而是：

```text
lead session + member sessions + mailbox + runtime + consolidation
```

### 7.4 Plan Mode -> Agent Team 是 CCPlus composition

CC baseline 的 Plan Mode 是 approval boundary：模型探索、写 plan、调用 `ExitPlanMode` 请求批准；批准后回到可执行状态，必要时模型可以再使用 team / task 工具来并行化。

Hive 的 `execution_contract.type="agent_team"` handoff 不是 CC 原样机制，而是把批准后的 plan 确定性落到 Hive Agent Team workbench/session substrate。默认 Plan Mode 仍然是 `continue_current_session`；只有显式或隐藏 contract 要求 `agent_team` 时才创建 member sessions。

## 8. 本轮验证

本轮重新核对当前 checkout 并补齐 P1/P2/P3/P4 follow-up 后，跑机制层、session runtime、Plan Mode/Agent Team、lint、前端定向和 build：

```bash
source backend/.venv/bin/activate && pytest backend/tests/runtime/test_hooks.py backend/tests/runtime/test_hooks_cc_parity.py backend/tests/api/test_hooks_api.py backend/tests/services/test_agent_session_continuation.py backend/tests/agents/test_subagent_spawn_tool.py backend/tests/services/test_subagent_run_service.py backend/tests/services/test_plan_mode_agent_team_handoff.py backend/tests/services/test_plan_mode_registry.py backend/tests/services/test_plan_mode_session_handoff.py backend/tests/tools/test_exit_plan_mode_tool.py backend/tests/api/test_agent_teams_events_api.py backend/tests/api/test_cc_codex_parity_api.py backend/tests/services/test_session_control_plane.py -q

source backend/.venv/bin/activate && pytest backend/tests/services/test_web_chat_runtime.py backend/tests/api/test_chat_session_runs.py backend/tests/services/test_conversation_branch_service.py backend/tests/services/test_session_command_runtime.py backend/tests/api/test_chat_session_branches.py backend/tests/kernel/test_engine_stop_hooks.py backend/tests/runtime/test_invoker_cc_hooks.py -q

source backend/.venv/bin/activate && pytest backend/tests/runtime/test_plan_mode_state.py backend/tests/services/test_plan_mode_handoff.py backend/tests/services/test_plan_mode_session_handoff.py backend/tests/services/test_plan_mode_agent_team_handoff.py backend/tests/tools/test_exit_plan_mode_tool.py backend/tests/tools/test_plan_mode_policy.py backend/tests/services/test_subagent_wake_consumer.py backend/tests/services/test_agent_team_context.py backend/tests/api/test_agent_teams_events_api.py backend/tests/services/test_goal_continuation_service.py backend/tests/services/test_trigger_daemon.py backend/tests/services/test_trigger_daemon_plan_context.py backend/tests/services/test_trigger_preflight.py -q

source backend/.venv/bin/activate && pytest backend/tests/runtime/test_hooks.py backend/tests/runtime/test_hooks_cc_parity.py backend/tests/kernel/test_engine_stop_hooks.py backend/tests/runtime/test_invoker_cc_hooks.py backend/tests/runtime/test_context_cc_parity_contract.py backend/tests/tools/test_workflow_tool.py backend/tests/agents/test_subagent.py backend/tests/runtime/test_t0_to_t2_session_close.py backend/tests/runtime/test_invoker.py backend/tests/api/test_websocket_call_llm.py backend/tests/services/test_conversation_branch_service.py backend/tests/api/test_chat_session_branches.py backend/tests/api/test_cc_codex_parity_api.py backend/tests/services/test_session_recall.py -q

source backend/.venv/bin/activate && ruff check backend/app/services/agent_session_continuation.py backend/app/services/plan_mode_agent_team_handoff.py backend/app/api/agent_teams.py backend/app/runtime/hooks.py backend/app/tools/handlers/subagent.py backend/app/tools/handlers/plan_mode.py backend/app/services/plan_mode_registry.py backend/tests/services/test_agent_session_continuation.py backend/tests/services/test_plan_mode_agent_team_handoff.py backend/tests/api/test_agent_teams_events_api.py backend/tests/runtime/test_hooks_cc_parity.py backend/tests/runtime/test_hooks.py backend/tests/agents/test_subagent_spawn_tool.py backend/tests/services/test_plan_mode_registry.py backend/tests/tools/test_exit_plan_mode_tool.py

source backend/.venv/bin/activate && ruff format --check backend/app/services/agent_session_continuation.py backend/app/services/plan_mode_agent_team_handoff.py backend/app/api/agent_teams.py backend/app/runtime/hooks.py backend/app/tools/handlers/subagent.py backend/app/tools/handlers/plan_mode.py backend/app/services/plan_mode_registry.py backend/tests/services/test_agent_session_continuation.py backend/tests/services/test_plan_mode_agent_team_handoff.py backend/tests/api/test_agent_teams_events_api.py backend/tests/runtime/test_hooks_cc_parity.py backend/tests/runtime/test_hooks.py backend/tests/agents/test_subagent_spawn_tool.py backend/tests/services/test_plan_mode_registry.py backend/tests/tools/test_exit_plan_mode_tool.py

npm --prefix frontend test -- --run src/api/domains/ccParity.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx --reporter=dot

npm --prefix frontend run build
```

结果：

```text
135 passed, 4 warnings
90 passed, 4 warnings
118 passed, 4 warnings
190 passed, 4 warnings
ruff check passed
ruff format --check passed
65 passed
frontend build passed
```

这证明当前 `agent-lifecycle-full-cc-parity-review` 覆盖的机制层仍然通过定向测试，同时本轮明确列出的 CCPlus follow-up 已完成 session/turn workbench/export、hook management contract、A2A continuation consumer、Agent Team runtime controls、Plan Mode approved-plan -> Agent Team session handoff composition、以及 JSON export/workbench 闭环。

## 9. 当前结论一句话

当前应这样表述：

```text
Agent lifecycle mechanism parity is aligned for the documented scope.
Plan Mode / Subagent / Hooks / Agent Team / Schedule / Goal details are aligned at the tested mechanism layer.
The listed CCPlus follow-up scope is implemented: session/turn workbench/export, hook management contract, A2A continuation controls, Agent Team runtime/workbench, Plan Mode approved-plan -> Agent Team session handoff composition, and JSON export.
```

因此，不应再用旧 review 的 P0/P1/P2 缺口描述当前代码状态；那些已经由 Implementation Closure / Turn Checkpoint Closure 覆盖。本轮列出的 CCPlus follow-up 也不能再反向说成“机制层没对齐”。
