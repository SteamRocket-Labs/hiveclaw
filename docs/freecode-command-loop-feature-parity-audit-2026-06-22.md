# FreeCode Command / Loop / Feature Parity Audit

日期：2026-06-22
状态：source-backed audit / appendix，canonical 总入口已迁移到 `docs/cc-codex-python-optimized-parity-master-plan-2026-06-22.md`
范围：FreeCode commands、tools、query loop、task/goal/worktree/team、hooks、Hive 当前 runtime/tool/API 对应面

## 0. 结论

Hive 当前不是“FreeCode 所有功能都有”。更准确的判断是：

1. **核心 agent runtime 能力大多已经有等价物或更强的 Hive 版本**：kernel tool loop、Plan Mode、Skill progressive disclosure、Subagent、Workflow、Work Ledger、MCP、web/file/search、trigger/schedule、RuntimeTask resume、memory/compaction/recovery。
2. **FreeCode 的 slash command / TUI command layer 还没有一一对齐**：Hive 有工具、API、前端页面和企业控制面，但没有统一的 `Command` registry、`local` / `local-jsx` / `prompt` 命令类型、remote-safe / bridge-safe allowlist、动态 skill/plugin/workflow command loader。
3. **Hook 面仍然不完全对齐**：上一轮已补 `UserPromptSubmit` / `Stop` / `StopFailure` / `SubagentStart` / `SubagentStop` 等核心 session-middle hooks，但 FreeCode 还有 PermissionRequest、TaskCreated/Completed、Elicitation、ConfigChange、InstructionsLoaded、WorktreeCreate/Remove、CwdChanged、FileChanged 等事件。
4. **FreeCode 的 Task / Team / Worktree 工具和 Hive 的 Work Ledger / RuntimeTask / org delegation 不是同构**：Hive 的 To-Do List / Work Ledger 已经对齐 FreeCode Task 的 agent-authored todo board 语义，但 FreeCode Task 还承担 Team 共享 task-list、background local task、TaskCreated/Completed hooks、TaskStop/TaskOutput 等命令层语义；这些不能混进 Work Ledger 本身。
5. **Loop / context management 是部分对齐，不是全量等价**：Hive 有 tool-result eviction、time-based microcompact、mid-loop compaction、PTL reactive retry、recovery manifest、LoopGuard；FreeCode 还有严格的 toolResultBudget -> snip -> microcompact -> contextCollapse -> autocompact -> blocking limit -> reactive compact 梯队、autocompact failure circuit breaker、read-time projection collapse、task_budget.remaining 等细节。

因此当前判定：

```text
runtime substrate: mostly covered / stronger in Hive where governance matters
command surface: partially missing
hook surface: partially missing after first core patch
task/team UX: Work Ledger half covered; Team member sessions still missing
worktree UX: deliberate non-goal for current organization-first scope
loop/context detail: partially covered, needs gap-driven parity pass
```

## 1. Evidence Sources

FreeCode baseline:

- `/Users/rocky243/vc-saas/free-code-main/src/commands.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/commands/`
- `/Users/rocky243/vc-saas/free-code-main/src/tools/`
- `/Users/rocky243/vc-saas/free-code-main/src/query.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/utils/hooks/hooksConfigManager.ts`
- `/Users/rocky243/vc-saas/free-code-main/docs/04-context-management.md`

Hive current checkout:

- `backend/app/services/agent_tools.py`
- `backend/app/tools/handlers/`
- `backend/app/tools/registry.py`
- `backend/app/tools/runtime_tool_groups.py`
- `backend/app/kernel/engine.py`
- `backend/app/kernel/loop_guard.py`
- `backend/app/runtime/hooks.py`
- `backend/app/api/`
- `backend/app/models/runtime_task.py`
- `backend/app/services/*runtime*`

Important FreeCode facts:

- Built-in commands are declared in `COMMANDS()` and include add-dir, branch, compact, context, cost, diff, doctor, files, hooks, mcp, memory, model, permissions, plan, plugin, resume, review, rewind, session, skills, stats, status, tasks, usage, workflows, ultraplan, etc. See `src/commands.ts:257-346`.
- Commands are dynamically assembled from bundled skills, builtin plugin skills, skill-dir commands, workflow commands, plugin commands, plugin skills, and built-ins. See `src/commands.ts:449-468`.
- Skill/model-invocable prompt commands are filtered by `getSkillToolCommands` and `getSlashCommandToolSkills`. See `src/commands.ts:561-607`.
- Remote and bridge command safety are explicit allowlists. See `src/commands.ts:619-685`.
- Query loop is an actual `while (true)` state machine with mutable state for compaction tracking, stop hook activity, max-output recovery, reactive compact guard, pending tool summaries, and turn count. See `src/query.ts:241-307`.
- Context shaping order is strict: `toolResultBudget -> snip -> microcompact -> contextCollapse -> autocompact -> blocking limit`; reactive compact handles real 413 fallback. See `docs/04-context-management.md:220-256`.
- FreeCode hook metadata includes PermissionRequest, TaskCreated/Completed, Elicitation, ConfigChange, InstructionsLoaded, WorktreeCreate/Remove, CwdChanged, FileChanged in addition to core session/tool hooks. See `src/utils/hooks/hooksConfigManager.ts:80-264`.
- FreeCode Task list id resolves to `CLAUDE_CODE_TASK_LIST_ID`, then in-process teammate team name, then process teammate team name, then leader team name, then session id; task files live under `<claude config home>/tasks/<taskListId>/`. See `src/utils/tasks.ts:193-230`.
- FreeCode Team metadata lives under `<claude config home>/teams/<team>/config.json`; the team file stores `leadSessionId` and members with `agentId`, `name`, `model`, `cwd`, `worktreePath?`, `sessionId?`, `subscriptions`, and backend type. See `src/utils/envUtils.ts:16-18` and `src/utils/swarm/teamHelpers.ts:68-181`.
- `TeamCreate` writes that team config, resets `<claude config home>/tasks/<team>/`, and sets the leader task-list id so the leader and teammates share one Task list. See `src/tools/TeamCreateTool/TeamCreateTool.ts:120-245`.
- Process teammates are launched as separate CLI/pane instances with `--agent-id`, `--agent-name`, `--team-name`, and `--parent-session-id`; in-process teammates run the same `runAgent()` loop under teammate context with a capped UI message mirror, while the full conversation follows the normal agent transcript path. See `src/utils/swarm/backends/PaneBackendExecutor.ts:100-160`, `src/tasks/InProcessTeammateTask/types.ts:19-120`, and `src/utils/swarm/inProcessRunner.ts:1160-1420`.
- Therefore Team is not just "a stronger Subagent". It is a persistent collaboration container: addressable agent instances, per-member session identity, shared task board, mailbox/permission bridge, and a pane/window/user-facing entry point.

Important Hive facts:

- Hive turn-1 core tools include memory, triggers, messaging/delegation, Plan Mode, tool_search, web_fetch/web_search, spawn_subagent/check_subagent, preview_workflow/start_workflow, track_todo/record_finding/read_ledger. See `backend/app/services/agent_tools.py:220-269`.
- Hive task tools are REST-facing only; agent board is intentionally Work Ledger only. See `backend/app/tools/handlers/tasks.py:1-11`.
- Hive hook enum now includes core CC-compatible session lifecycle events plus Hive-specific events. See `backend/app/runtime/hooks.py:18-75`.
- Hive kernel has LoopGuard, runtime reminders, microcompact, mid-loop compaction, pre/post compaction hooks, and compaction checkpoint persistence. See `backend/app/kernel/engine.py:2286-2290`, `backend/app/kernel/engine.py:3852-4028`.
- Hive has RuntimeTask resume for web chat, subagent, trigger, heartbeat, workflow and reconciliation at startup. See `backend/app/main.py:340-360`.
- Hive has conversation branch API (`POST /sessions/{session_id}/branches`) but no FreeCode-style `/rewind` command. See `backend/app/api/chat_sessions.py:465`.

## 2. FreeCode Feature Inventory

### 2.1 Command Layer

FreeCode command layer is not just a display list. It is a runtime surface with:

- built-in commands
- feature-gated commands
- internal-only commands
- dynamic skill commands
- plugin commands
- plugin-provided skills
- workflow commands
- MCP prompt skills
- availability / enabled filtering
- remote-safe filtering
- bridge-safe filtering
- command memoization and cache invalidation

Hive currently has no equivalent single command registry. Hive has:

- LLM tools
- frontend/API routes
- skills
- plugins
- MCP servers
- workflows
- local bridge / desktop APIs

But these are not unified as a slash-command command object with `name`, `aliases`, `type`, `loadedFrom`, `disableModelInvocation`, `remote_safe`, `bridge_safe`, and command-source provenance.

### 2.2 Query Loop / Context Loop

FreeCode query loop includes:

- mutable loop state
- query chain id/depth
- memory prefetch
- skill discovery prefetch
- tool result budget replacement
- snip
- microcompact
- contextCollapse read-time projection
- autocompact with failure circuit breaker
- hard blocking limit
- model fallback tombstone cleanup
- streaming tool execution
- withheld recoverable errors
- reactive compact on prompt-too-long / media size
- max-output-token escalation and continuation
- stop hook blocking continuation
- token budget continuation
- tool-use summary generation
- queued notifications / command attachments
- maxTurns handling

Hive currently covers:

- max tool rounds
- LoopGuard warn/abort
- runtime reminder scheduler
- tool-result eviction to artifact + preview
- time-based microcompact
- mid-loop compaction at threshold
- pre/post compaction hooks
- PTL retry with full compress and round-group fallback
- streamed callback failure tolerance
- prompt cache hints
- provider fallback/retry
- persisted checkpoint before exit
- Work Ledger restoration after compaction

Hive gaps:

- no confirmed read-time projection equivalent to FreeCode `contextCollapse`
- no full FreeCode-shaped `toolResultBudget -> snip -> microcompact -> collapse -> autocompact -> blocking` pipeline contract
- no command attachment queue equivalent for slash commands
- no exact `task_budget.remaining` cross-compaction contract
- autocompact failure circuit breaker exists for summary paths in `memory_service`, but kernel compaction/retry needs an explicit FreeCode-style parity audit

### 2.3 Tools

FreeCode has tool families:

- File: FileRead, FileWrite, FileEdit, Glob, Grep, NotebookEdit, LSP
- Shell: Bash, PowerShell, REPL
- Web: WebSearch, WebFetch
- MCP: MCPTool, ListMcpResources, ReadMcpResource, McpAuth
- Skill: SkillTool, ToolSearch
- Plan: EnterPlanMode, ExitPlanMode, AskUserQuestion, VerifyPlanExecution
- Agent/Subagent: AgentTool, built-in explore/general/plan/verification agents, fork/resume agent
- Task: TaskCreate, TaskGet, TaskList, TaskOutput, TaskStop, TaskUpdate
- Workflow: WorkflowTool
- Worktree: EnterWorktree, ExitWorktree
- Team: TeamCreate, TeamDelete
- Messaging/Schedule/Remote: SendMessage, ScheduleCron, RemoteTrigger, Sleep, Brief

Hive currently has LLM-callable tools:

- File/search/code: list/read/write/edit/delete, glob/grep, read_document, execute_code/run_command
- Web/search/crawl: web_search/web_fetch plus deferred Exa/Tavily/AnySearch/Firecrawl/XCrawl
- MCP: discover/import/list/inspect/call/read resources
- Skill: load_skill/save_skill/pin_skill/tool_search
- Plan: request_plan_mode, ask_user_question, exit_plan_mode
- Subagent/delegation: spawn_subagent, check_subagent, delegate_to_agent, send_message_to_agent
- Workflow: preview_workflow, start_workflow
- Work Ledger: track_todo, record_finding, read_ledger
- Runtime async tasks: check_async_task, cancel_async_task, list_async_tasks
- Triggers/schedules: set/update/cancel/list_trigger
- Memory: search/load/save/update/retire/T3 gate tools
- Channels: Feishu/email/office/plaza/channel delivery
- HR: create_digital_employee / preview_agent_blueprint

Hive missing or non-equivalent:

- no LSP tool equivalent
- no NotebookEdit equivalent
- no first-class Bash/PowerShell local task command model; `run_command` / `execute_code` are governed sandbox/code execution, not the same CC local shell task runtime
- no TaskCreate/Get/List/Output/Stop/Update LLM tool set; Work Ledger covers the todo-board half, while Team/shared/background task semantics are missing
- no EnterWorktree/ExitWorktree LLM tool pair
- no TeamCreate/TeamDelete equivalent; Hive has org agents/delegation but not FreeCode-style directly addressable teammate sessions
- no VerifyPlanExecution equivalent; FreeCode's current reconstructed tool is disabled, but the command surface still names the concept

### 2.4 Command Families

FreeCode commands by function family:

| Family | FreeCode examples | Hive current equivalent | Status |
|---|---|---|---|
| Session | resume, session, branch, rewind, rename, tag, export, clear, compact | chat_sessions history, branch API, T0, compaction, rename/tag not unified | Partial |
| Context visibility | context, files, cost, usage, stats, status | metrics/spans, files APIs, token tracking | Partial; no slash command UX |
| Setup/config | init, config, model, effort, fast, output-style, privacy-settings, theme, vim, keybindings, statusline, terminalSetup | settings/admin/UI/model config | Partial / product-shape mismatch |
| Hooks | hooks command | hook registry + docs/tests | Partial; event surface missing |
| Permission | permissions | Capability Gate, ActionPreflight, guard policies | Strong governance, no command parity |
| MCP/plugin/skill | mcp, plugin, reload-plugins, skills | MCP APIs, plugins APIs, skills APIs/tools | Partial; no command loader parity |
| Coding/Git | diff, commit, commit-push-pr, pr-comments, review, security-review | code execution/evals/review docs, no git command suite | Mostly missing |
| Background work | tasks | RuntimeTask + async task tools + Work Ledger | Partial; Work Ledger is todo-board only |
| Worktree | branch/worktree tools, add-dir | conversation branch API; no worktree tools/add-dir | Deliberate non-goal for current scope |
| Remote/mobile/desktop | session, mobile, desktop, chrome, remote-env, remote-control | local_bridge + desktop APIs | Partial, product-shape mismatch |
| Plan | plan, ultraplan | Plan Mode tools/API, plans API | Partial; no `/ultraplan` |
| Feedback/product | feedback, upgrade, release-notes, stickers, thinkback | not core Hive runtime | Not target unless product parity required |

## 3. Hive Coverage Matrix

| Capability | Hive has? | Judgment |
|---|---:|---|
| CC-style model/tool loop | Yes | Strong substrate, details not fully identical |
| Loop guard | Yes | Hive has deterministic warn-before-abort LoopGuard |
| Tool result eviction | Yes | Hive saves large results to artifact + preview |
| Microcompact | Yes | Time/pressure-based old tool result clearing |
| Autocompact / mid-loop compact | Yes | Exists, but FreeCode exact pipeline and circuit breaker need separate parity hardening |
| Reactive prompt-too-long recovery | Yes | PTL retry path exists |
| ContextCollapse read-time projection | No confirmed | Gap |
| UserPromptSubmit / Stop / Subagent hooks | Yes, first core patch | Core subset covered |
| Full FreeCode hook event surface | No | Gap |
| Plan Mode | Yes | Strong, with confirmable plan gate |
| Ask user question | Yes | Tool exists |
| Verify plan execution | No | Gap / FreeCode tool disabled in snapshot |
| Skill progressive disclosure | Yes | Strong, with Skill candidate/gate delta |
| Dynamic command-as-skill loader | No | Gap |
| Tool search / deferred tools | Yes | Strong, via runtime tool groups |
| MCP tools/resources | Yes | Strong, governed authz |
| Plugin management | Yes API | No command-layer parity |
| Subagent | Yes | Strong; command/team parity separate |
| AgentTool fork/resume sidechain | Partial | Hive has subagent RuntimeTask/T0; not same FreeCode sidechain command UX |
| Workflow | Yes | Stronger structured Hive delta |
| Task/Todo | Partial | Work Ledger yes for agent todo board; shared/background Task* semantics missing |
| Background local shell tasks | Partial | RuntimeTask/async tasks exist, but no CC local task shell runtime |
| Team/swarms | No for target semantics | Hive org/delegation exists; missing directly enterable teammate sessions |
| Worktree enter/exit | No | Non-goal unless coding pack requires it |
| Conversation branch | Yes API | Similar to branch, not rewind/worktree parity |
| Git diff/commit/PR/review commands | Mostly no | Gap if coding-command parity is required |
| Usage/cost/status/context commands | Partial | metrics/spans exist; no agent-facing command equivalents |
| Desktop/local bridge/remote | Partial | APIs exist; no FreeCode command surface |

## 4. Priority Gaps

### P0 — Build a Command Surface Registry

If the goal is **full FreeCode command parity**, Hive needs a command registry separate from LLM tools:

```text
Command {
  name
  aliases
  type: prompt | local | ui | api_action
  source: builtin | skill | plugin | workflow | mcp | hive
  loaded_from
  description
  availability
  enabled
  model_invocable
  remote_safe
  bridge_safe
  handler_ref
}
```

This does not mean copying the TS TUI. It means exposing the same semantic layer in Hive's web/API/desktop/control-plane form.

Minimum first pass:

1. Register builtin Hive commands corresponding to session/context/plan/hooks/skills/mcp/workflows/tasks.
2. Register skill prompt commands from installed skills.
3. Register workflow commands from workflow definitions.
4. Register plugin commands from installed plugins.
5. Add remote-safe / bridge-safe policy.
6. Add `GET /agents/{agent_id}/commands` and execution endpoint with governance.

### P0 — Complete Hook Event Surface

Add or explicitly map these FreeCode hook events:

- PermissionRequest
- PermissionDenied
- Notification
- Setup
- TeammateIdle
- TaskCreated
- TaskCompleted
- Elicitation
- ElicitationResult
- ConfigChange
- InstructionsLoaded
- WorktreeCreate (coding-pack optional)
- WorktreeRemove (coding-pack optional)
- CwdChanged
- FileChanged

Some should map to existing Hive governance surfaces rather than execute arbitrary local hook scripts. But the mapping must be explicit.

### P0 — Team / Teammate Session Runtime

Team should be implemented in Hive, but not by renaming Subagent.

Scope clarification:

1. This Team is a **single-agent session workspace** feature, not the same thing as organization-level A2A delegation.
2. A Team is created under one lead/current agent and one parent session. It opens multiple member windows, each with its own isolated context and addressable session.
3. The user may switch between member windows, talk to any member directly, let members talk through mailbox/events, then close the Team and merge the useful outputs back into the lead/main window.
4. A2A remains the company/org-level relationship between independently governed agents. Team members may later be backed by real org agents, but the default Team member is a session-local teammate/persona under the lead agent, not necessarily a separate employee agent.
5. In that sense Team is closest to an **interactive, windowed subagent swarm**: stronger than one-shot subagent because the user can enter each child context, but narrower than A2A because it is scoped to the current lead agent/session workspace.

FreeCode Team semantics:

1. `TeamCreate` persists team metadata at `<claude config home>/teams/<team>/config.json`.
2. A Team owns a shared task list at `<claude config home>/tasks/<team>/`.
3. Each member has an addressable identity: `agentId`, `name`, `teamName`, optional `sessionId`, model, cwd, backend.
4. Pane teammates are separate CLI/window instances; in-process teammates run the same agent loop under teammate context.
5. Members communicate through mailbox/inbox semantics; the leader does not automatically see every private transcript.

Hive target semantics:

1. Add a DB-backed Team container, not a file-only Team config:
   - `agent_teams`: tenant, owner/lead agent, lead chat session, description, status, created_by.
   - `agent_team_members`: team, member persona or optional backing agent, model, permission mode, status, current chat session, current runtime task, subscriptions.
2. Every Team member gets a first-class `ChatSession`, so the user can enter that member's window and talk directly.
3. Active member work is a `RuntimeTask(task_type="team_member")`, resumable through the same restart-safe runtime accounting used by web chat/subagent/workflow.
4. Team transcript truth should follow Hive's normal T0/session transcript path, not FreeCode's team `config.json`; the Team DB rows only index who belongs to the Team and which session/runtime task is active.
5. Add a Team-scoped Work Ledger view:
   - shared board: Team-level todos/tasks visible to lead and members.
   - member board: per-member Work Ledger still records that member's own open loop.
   - writing a Work Ledger todo still does not start execution.
6. Add mailbox/events:
   - direct user -> member message
   - member -> lead message
   - member -> member message
   - idle/blocked/needs-approval events
   - all audited through invocation spans/runtime events.
7. Add Team close/consolidation:
   - stop/settle active member runs
   - collect each member's final summary, artifacts, Work Ledger deltas, and T0 refs
   - synthesize a lead/main-window handoff
   - mark member sessions archived/closed, while preserving their raw transcripts
8. Add command/API/tool surface:
   - `create_team`
   - `delete_team`
   - `add_team_member`
   - `send_team_message`
   - `list_team_members`
   - `enter_team_member_session`
   - `close_team_and_merge`
   - optional model-invocable aliases mapped through the future Command registry.
9. Add frontend:
   - Team panel on Agent Detail
   - member tabs/windows
   - per-member transcript
   - shared task board
   - member status, idle, blocked, approval-needed indicators.

This makes Team a higher-level collaboration runtime built on `ChatSession`, `RuntimeTask`, Work Ledger, invocation spans, and governance. Subagent remains the lightweight worker/delegation primitive: useful for isolated execution and returning a distilled result, but not directly user-enterable.

Why DB-backed:

1. The DB is the **control index**, not the transcript truth source and not a replacement for A2A. Team rows answer: which lead agent/session owns this Team, who can see it, which member windows exist, which member session is current, which runtime task is running, and whether the member is idle/blocked/needs approval.
2. Raw conversation evidence still belongs to Hive's normal transcript/T0 path. Team DB rows should point to `ChatSession`, `RuntimeTask`, transcript events, T0 refs, and invocation spans; they should not become a second conversation log.
3. A user-facing Team needs cross-process discovery. After backend restart, another worker or frontend tab must be able to list Teams, enter a member session, reconnect to active member work, and show status without relying on one process's memory.
4. A company-facing Team needs governance joins: tenant, user, owner agent, member agent, permission mode, capability policy, audit, budget, and invocation spans. File-only config cannot safely enforce or query those boundaries in Hive's multi-tenant control plane.
5. The DB also prevents duplicate active runs and provides atomic state changes. `RuntimeTask` already uses DB state for active run uniqueness/resume; Team member runtime should use the same pattern instead of inventing a separate file lock/process-local registry.
6. `RuntimeTask` here is not "DB Task" in the business-task sense. It is a persisted run handle for one active member execution. Work Ledger remains the To-Do List; A2A remains org-level delegation; Team RuntimeTask only tracks the lifecycle of a member window/run.

### P1 — Task / Work Ledger / RuntimeTask Reconciliation

Current Hive rule says `track_todo` is cognitive bookkeeping and never starts execution. That is correct for governance. But FreeCode also has TaskCreate/Get/List/Output/Stop/Update as a session task system.

Decision:

1. Keep Work Ledger as the canonical agent-authored To-Do List / task board.
2. Do not turn Work Ledger into an execution queue.
3. Add Task* parity only as command/API semantics over Team/shared/background work, with explicit pointers to `RuntimeTask` where actual execution exists.

Mapping:

- FreeCode Task `subject/description/status/blocks/blockedBy/owner` maps to Work Ledger todo fields where it is cognitive work.
- FreeCode Task `TaskOutput/TaskStop/background process` maps to `RuntimeTask` or async task handles where it is executable work.
- FreeCode Team task-list maps to Team-scoped Work Ledger plus Team runtime events.
- `TaskCreated` / `TaskCompleted` hooks should fire from both Work Ledger writes and RuntimeTask terminal transitions, with metadata saying which surface produced the event.

### P2 — Worktree / Branch / Rewind Parity

Hive has conversation branch API, but does not have:

- `EnterWorktree`
- `ExitWorktree`
- `/rewind` equivalent
- `/add-dir` multi-root session workspace command

Current decision: Worktree is not needed for the core organization-facing Hive target because Hive is not primarily a coding agent. Keep Worktree as an optional future coding capability pack. Do not block Team/Task parity on Worktree.

If a future coding pack needs it, implement it as governed workspace/session isolation rather than raw git worktree from the agent.

### P1 — Context Pipeline Parity

Hive should write a focused context-loop parity spec:

```text
FreeCode:
toolResultBudget -> snip -> microcompact -> contextCollapse -> autocompact -> blocking -> reactive compact

Hive current:
large result artifact preview -> time/pressure microcompact -> mid-loop compact -> PTL retry -> recovery manifest
```

Required decisions:

1. Whether Hive needs read-time projection collapse.
2. Whether kernel compaction needs a per-invocation autocompact failure circuit breaker matching FreeCode.
3. Whether `task_budget.remaining` has a Hive equivalent for post-compaction budget correctness.
4. Whether snip-like zombie message removal is needed or should remain a non-goal.

### P2 — Coding Command Suite

FreeCode has coding-product commands:

- diff
- commit
- commit-push-pr
- pr-comments
- review
- ultrareview
- security-review
- doctor
- context/files

Hive has enough generic tools to perform many of these manually, and evals include review scenarios, but it does not have first-class command parity.

Because Hive is domain-neutral, these should be a **coding capability pack / plugin**, not core runtime.

### P2 — Status / Usage / Cost / Diagnostics Commands

Hive has invocation spans, metrics, token tracking, admin dashboards, traces, and runtime events. It lacks FreeCode-style user-facing commands:

- `/cost`
- `/usage`
- `/stats`
- `/status`
- `/context`
- `/doctor`

These should map to read-only command surface entries over existing telemetry.

## 5. What We Should Not Copy Literally

Do not blindly copy these as core Hive runtime:

1. TUI-specific commands: theme, color, vim, keybindings, statusline, terminalSetup.
2. Claude/Anthropic account/product commands: login/logout/upgrade/extra-usage/rate-limit-options/stickers/thinkback.
3. Raw local shell/worktree behavior without Hive governance.
4. Coding-only commands as global agent primitives.

Correct Hive shape:

- core runtime parity for lifecycle semantics
- command surface parity as web/API/desktop control surface
- coding commands as installable capability pack
- local shell/worktree only through governed workspace runtime, and only inside an optional coding pack unless organization workflows require it
- Memory/Iter remains Hive-native delta

## 6. Recommended Next Implementation Order

1. **Command Registry substrate**: define `CommandDefinition`, sources, availability, safety classes, and read API.
2. **Team runtime**: implement DB-backed Team + member ChatSessions + `RuntimeTask(task_type="team_member")` + mailbox/events + frontend switcher.
3. **Hook event completion**: add explicit mappings for remaining FreeCode hook events, starting with PermissionRequest, TaskCreated/Completed, Elicitation, InstructionsLoaded.
4. **Task model encoding**: keep Work Ledger cognitive-only; add Team/shared/background Task* command semantics as adapters over Work Ledger + RuntimeTask.
5. **Context pipeline parity spec + tests**: compare FreeCode context ladder to Hive kernel path and decide collapse/snip/breaker/budget deltas.
6. **Telemetry commands**: cost/usage/status/context/doctor over invocation spans and runtime metadata.
7. **Coding command pack**: diff/review/security-review/PR/comment/worktree commands as optional pack.

## 7. Current Answer to the User Question

Question: “FreeCode 里面所有目前所实现的一些功能，我们现在是不是都有？”

Answer: **没有全都有。**

Hive 已有并且更强的是组织化 agent runtime、Memory/Iter、governance、Workflow/Subagent/Skill substrate。FreeCode 更完整的是本地 CLI/TUI command layer、会话级任务/团队命令、完整 hook event surface、以及若干 coding/product commands。Worktree 是 FreeCode 的重要 coding 能力，但不是当前 Hive organization-first scope 的核心目标。

If the target is “Cloud Code Python evolution version”, then next gap is no longer only Skill/Subagent/Workflow/Hooks. The next gap is:

```text
FreeCode command surface + loop/context detail + Team/member-session mechanics + Task command adapters
```

These need their own parity pass.

Implementation target has been split out to:

- `docs/freecode-non-coding-feature-implementation-plan-2026-06-22.md`
