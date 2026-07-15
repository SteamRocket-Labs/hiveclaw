# Sub-agent / Agent Team CC Parity Audit

Date: 2026-07-03
Scope: source-level audit of FreeCode/CC Sub-agent and Agent Team flows versus current Hive implementation.

## Verdict

Hive is not yet fully CC-equivalent for Sub-agent and Agent Team.

Hive has already aligned several important contracts:

- `team_create` is container-only.
- Teammates are created through the `spawn_subagent` / AgentTool branch with `team_name + name`.
- Persistent subagent definitions exist and are exposed to the parent model.
- Background subagents use durable `RuntimeTask` + child `ChatSession` projections instead of volatile UI-only task state.

But there are still semantic gaps:

- CC exposes built-in, plugin, user, project, flag, and managed agents in one active `subagent_type` namespace. Hive exposes built-ins through `subagent_type`, but custom definitions require `definition_name`.
- CC `agent.md` frontmatter has many fields Hive does not parse or honor.
- Hive background restart currently reconstructs only a partial `SubagentSpec`; custom definition prompt/tool/model/memory/isolation details are not fully persisted for replay.
- CC Agent Team teammates are long-lived idle loops with mailbox/task-list coordination. Hive teammates are enterable child chat sessions with `RuntimeTask` per message.
- CC Team has a shared team task list as first-class coordination. Hive has team/member DB events and Work Ledger nearby, but not the same Team = TaskList runtime contract.

## CC Sub-agent Baseline

Primary source: `/Users/rocky243/vc-saas/free-code-main`.

### Agent Discovery And Selection

CC loads active agents through `getAgentDefinitionsWithOverrides(cwd)` in `src/tools/AgentTool/loadAgentsDir.ts`.

Flow:

1. Load built-ins.
2. Load plugin agents.
3. Load markdown files from `agents/` under user/project/settings scopes.
4. Parse `agent.md` files through `parseAgentFromMarkdown`.
5. Merge by `agentType` using source precedence: built-in, plugin, user, project, flag, managed.
6. Filter by MCP requirements and permission rules before showing agents to the model.

Important point for "50 agent.md files":

CC does not run a separate vector search or router first. It surfaces the active agent list to the parent model through the Agent tool prompt or an attachment. The model chooses an exact `subagent_type` by reading the agent names, descriptions, and available tools. The tool then validates that exact type.

Relevant CC files:

- `src/tools/AgentTool/loadAgentsDir.ts`
- `src/tools/AgentTool/prompt.ts`
- `src/tools/AgentTool/AgentTool.tsx`

### CC Agent Definition Schema

CC markdown agents require:

- `name`
- `description`
- body as the full agent system prompt

CC also supports:

- `tools`
- `disallowedTools`
- `model`, including `inherit`
- `effort`
- `permissionMode`
- `mcpServers`
- `hooks`
- `maxTurns`
- `skills`
- `initialPrompt`
- `memory`
- `background`
- `isolation`
- `color`

These fields affect runtime behavior, not just display.

### Fresh Sub-agent Versus Fork

CC semantics:

- If `subagent_type` is specified, the child starts fresh and needs a self-contained prompt.
- If fork mode is enabled and `subagent_type` is omitted, the child inherits the parent conversation context.
- Worktree isolation is available via `isolation: "worktree"` in supported paths.

Relevant CC files:

- `src/tools/AgentTool/prompt.ts`
- `src/tools/AgentTool/AgentTool.tsx`
- `src/tools/AgentTool/runAgent.ts`

### Result And Continuation

CC synchronous subagent:

- Runs through `runAgent`.
- Returns a final `AgentToolResult`.
- The result is passed back to the parent as a tool result; the parent summarizes to the user.

CC background subagent:

- Registers a `LocalAgentTask`.
- Symlinks task output to the sidechain transcript.
- Returns `agentId` and `outputFile`.
- On completion, enqueues a `<task-notification>` back into the parent conversation.

CC continuation:

- `SendMessage(to=agentNameOrId, message=...)` routes to a running subagent if present.
- If running, it queues a pending message for the next tool round.
- If stopped or evicted, it resumes from sidechain transcript and metadata with `resumeAgentBackground`.

Relevant CC files:

- `src/tasks/LocalAgentTask/LocalAgentTask.tsx`
- `src/tools/SendMessageTool/SendMessageTool.ts`
- `src/tools/AgentTool/resumeAgent.ts`
- `src/tools/AgentTool/runAgent.ts`

## CC Agent Team Baseline

### Team Creation

`TeamCreate` only creates the team container:

- Team config file under `~/.claude/teams/{team}/config.json`
- Lead member record
- Shared team task list under `~/.claude/tasks/{team}/`
- AppState team context

It does not spawn members inline.

Relevant CC files:

- `src/tools/TeamCreateTool/TeamCreateTool.ts`
- `src/tools/TeamCreateTool/prompt.ts`

### Teammate Creation

Teammates are spawned through the Agent tool with:

- `team_name`
- `name`
- `prompt`
- optional `subagent_type`

This routes to `spawnTeammate`, not the normal one-shot subagent path.

Relevant CC files:

- `src/tools/AgentTool/AgentTool.tsx`
- `src/tools/shared/spawnMultiAgent.ts`
- `src/utils/swarm/spawnInProcess.ts`
- `src/utils/swarm/backends/InProcessBackend.ts`

### Teammate Runtime

CC teammates are long-lived.

The in-process teammate loop:

- Builds a teammate system prompt from the normal main prompt plus teammate-specific addendum.
- Runs the same `runAgent()` core loop as AgentTool.
- Accumulates conversation state across iterations.
- Goes idle after each turn.
- Sends idle notifications.
- Waits for mailbox messages, shutdown requests, or task-list work.
- Exits only on abort or model-approved shutdown.

Relevant CC files:

- `src/tasks/InProcessTeammateTask/InProcessTeammateTask.tsx`
- `src/utils/swarm/inProcessRunner.ts`
- `src/utils/teammateMailbox.ts`
- `src/utils/teammateContext.ts`

## Hive Current Sub-agent Implementation

Primary source: `/Users/rocky243/vc-saas/hiveclaw-main`.

### Tool Entry

Hive exposes `spawn_subagent` in `backend/app/tools/handlers/subagent.py`.

It supports:

- `prompt` / `task`
- `subagent_type`
- alias `type`
- `definition_name`
- `name`
- `team_name`
- `model`
- `isolation`
- `run_in_background`
- `permission_profile`
- `ledger_todo_id`

Important current behavior:

- Built-ins are an enum: `general-purpose`, `explorer`, `critic`.
- Custom definitions are selected through `definition_name`, not `subagent_type`.
- If no type/definition is supplied on foreground spawn, Hive defaults to `isolation="all"` to approximate CC fork behavior.
- If `team_name + name` is present, Hive routes to Agent Team teammate creation.
- If an active Agent Team exists and a plain worker is requested, Hive blocks the silent downgrade and asks the model to use `team_create` then `spawn_subagent(team_name + name)`.

### Definition Loading

Hive persistent definitions live in:

- agent scope: `<AGENT_DATA_DIR>/<agent_id>/subagents/<name>.md`
- tenant scope: `<AGENT_DATA_DIR>/_tenants/<tenant_id>/subagents/definitions/<name>.md`

Resolution order:

1. agent scope
2. tenant scope
3. built-in template rows in listing surfaces

Relevant Hive files:

- `backend/app/agents/subagent_definition.py`
- `backend/app/runtime/prompt_sections/subagent_listing.py`

### Hive Definition Schema

Hive supports:

- `name`
- `description`
- `type`
- `allowed_tools`
- `excluded_tools`
- `model`
- `max_tool_rounds`
- `isolation`
- `memory`
- body as `system_prompt`

This is not the full CC schema.

### Runtime Execution

Hive `spawn_subagent` builds a `SubagentSpec`, resolves allowed/excluded tools, builds the standalone system prompt, appends child T0 events, emits subagent lifecycle hooks, and calls `invoke_agent` with:

- `standalone_system_prompt`
- child `SessionContext`
- `allowed_tool_names`
- `excluded_tool_names`
- `max_tool_rounds`

Relevant Hive file:

- `backend/app/agents/subagent.py`

### Background Runs And Recovery

Hive background runs:

- Create a `RuntimeTask(task_type="subagent")`.
- Create a child `ChatSession(session_kind="subagent")`.
- Return `run_id` and `child_session_id`.
- On completion, append terminal child-session event and wake the parent through a CC-style task notification.

Recovery currently:

- Scans `pending`, `running`, and `needs_reconciliation` subagent runtime tasks.
- Refuses to replay unsafe child tool frames.
- Can replay if the subagent type is read-only or the restart journal proves spawn intent and known child frames are replay-safe.
- Reconstructs only `name`, `type`, and `max_tool_rounds` into `SubagentSpec`.

That last point is a CC parity gap: a custom definition's system prompt, allowed/excluded tools, model override, memory scope, definition source, and isolation are not fully snapshotted and replayed.

Relevant Hive files:

- `backend/app/services/subagent_run_service.py`
- `backend/app/services/agent_session_continuation.py`
- `backend/app/services/subagent_wake_consumer.py`

## Hive Current Agent Team Implementation

### Team Create

Hive `team_create` is container-only and calls `create_agent_team_from_tool_request`.

Relevant Hive files:

- `backend/app/tools/handlers/command_parity.py`
- `backend/app/services/agent_team_runtime_service.py`
- `backend/app/services/agent_team_contract.py`

### Team Storage

Hive persists:

- `AgentTeam`
- `AgentTeamMember`
- `AgentTeamEvent`

Relevant Hive file:

- `backend/app/models/agent_team.py`

### Teammate Spawn

Hive teammate creation uses the `spawn_subagent(team_name + name)` branch:

- creates `AgentTeamMember`
- creates member `ChatSession(session_kind="team_member")`
- appends a parent session event
- sends the initial prompt through `message_agent_team_members_runtime`
- starts or queues a `RuntimeTask(task_type="team_member")`

Relevant Hive file:

- `backend/app/services/agent_team_runtime_service.py`

### Teammate Messaging

Hive `send_agent_session_message` can address:

- a child session id
- a team id + member name
- a team name + `to`
- `*` broadcast

It appends to the transcript mailbox and uses `continue_agent_session_from_mailbox` to either queue into an active run or start a new durable turn.

Relevant Hive files:

- `backend/app/tools/handlers/subagent.py`
- `backend/app/services/agent_session_continuation.py`
- `backend/app/services/agent_team_runtime_service.py`

## Gap Matrix

| Area | CC baseline | Hive current | Gap |
| --- | --- | --- | --- |
| Agent namespace | All active agents are selected by `subagent_type` | Built-ins use `subagent_type`; custom definitions use `definition_name` | Not CC-compatible for custom `agent.md` selection |
| Agent discovery | Built-in + plugin + user/project/flag/managed merged into active list | Agent + tenant definitions plus built-in rows | Plugin/settings/policy source model not equivalent |
| 50 agent.md selection | Model sees active agent descriptions and chooses exact type | Model sees custom definition listing but must use a different parameter | Similar concept, different call contract |
| Frontmatter | Rich CC schema | Smaller Hive schema | Missing fields: `tools`, `disallowedTools`, `background`, `permissionMode`, `maxTurns`, `skills`, `initialPrompt`, `mcpServers`, `hooks`, `color`, `effort`, `worktree` |
| Built-in names | `general-purpose`, `Explore`, `verification`, plus gated built-ins | `general-purpose`, `explorer`, `critic` | Needs aliases or exact naming compatibility |
| Tool pool | General-purpose can access all filtered tools by default | General-purpose is hard-limited to a small preset | Governance may justify this, but it is not CC exact |
| Fresh/fork | `subagent_type` present = fresh; omitted = fork under fork gate | `isolation` controls `none/all`, omitted foreground defaults `all` | Approximation, not same API semantics |
| Background result | `agentId`, `outputFile`, task notification | `run_id`, `child_session_id`, parent wake | Product-equivalent but not exact output-file model |
| Resume | Sidechain transcript + metadata; exact agent type resolved from active definitions | RuntimeTask + ChatSession; partial spec reconstruction | Custom definitions are not replayed exactly |
| Running continuation | `SendMessage` queues pending message to running task | `continue_agent_session_from_mailbox` queues/schedules durable turn | Similar, but different execution loop |
| Team create | container only + shared task list | container only in DB | Container semantics aligned |
| Teammate spawn | AgentTool `team_name + name` branch | `spawn_subagent(team_name + name)` branch | Entry semantics aligned |
| Teammate lifecycle | Long-lived idle loop | ChatSession + RuntimeTask per message | Not equivalent |
| Team task list | Team = TaskList under `~/.claude/tasks/{team}` | Team DB events; Work Ledger nearby but not same team task-list contract | Missing or not mapped |
| Mailbox | File mailbox + auto delivery + idle notifications | Transcript mailbox + parent wake | Similar goal, different loop semantics |

## Direct Answers

### Are Hive Sub-agents And CC Sub-agents The Same?

No. They are similar in intent and some surfaces, but not fully the same.

Hive currently has a session-local worker system with persistent definitions and durable background runs. CC has an AgentTool system where all active definitions are selected in one `subagent_type` namespace, run through `runAgent`, and resume from sidechain transcript/metadata. Hive needs more compatibility work before we can call it CC-equivalent.

### How Should A Main Agent Pick From 50 `agent.md` Files In CC?

CC loads the 50 files into active agent definitions, injects a list of names, descriptions, and tools into the model context, and the main agent chooses the exact `subagent_type`.

Hive should match that by letting custom definitions be selected as `subagent_type`, while keeping `definition_name` as a backward-compatible alias.

### Is The Screenshot's `needs_reconciliation` A Sub-agent Total Failure?

No. In current Hive code it means a persisted background subagent was interrupted by runtime restart and the recovery logic decided automatic replay might be unsafe. That is not "the model refused" and not "UI compaction." It is a replay-safety decision in `subagent_run_service`.

But the fact that custom definition replay does not persist/reconstruct the full spec is a real bug relative to CC recovery semantics.

### Is Agent Team Fully Aligned?

No.

Hive is aligned on the container-only `team_create` and teammate spawn branch. It is not aligned on the long-lived teammate idle loop and shared task-list coordination.

## Recommended Fix Order

1. Unify active subagent definitions into a CC-compatible `subagent_type` namespace.
   - Allow `subagent_type` to be any active built-in or custom definition name.
   - Keep `definition_name` as an alias.
   - Remove the fixed enum from the live schema or make it non-exhaustive.

2. Add CC frontmatter compatibility.
   - Parse `tools` as alias of `allowed_tools`.
   - Parse `disallowedTools` as alias of `excluded_tools`.
   - Parse and either implement or explicitly reject with warnings: `background`, `permissionMode`, `maxTurns`, `skills`, `initialPrompt`, `mcpServers`, `hooks`, `color`, `effort`, `isolation: worktree`.

3. Persist full subagent spec snapshots for background runs.
   - Store definition name, scope, file hash, system prompt, allowed/excluded tools, model, max rounds, isolation, memory scope, and definition body.
   - Resume from the snapshot first, then optionally validate against current definition drift.

4. Align built-in names and aliases.
   - Add CC-compatible aliases for `Explore` and `verification`.
   - Decide whether Hive's restricted `general-purpose` tool pool is a governance delta or a bug. If exact parity is required, default to all tools filtered by governance instead of a small allowlist.

5. Rework Agent Team teammate lifecycle.
   - Either implement a long-lived idle loop for team members, or explicitly document DB-backed `RuntimeTask` per message as an intentional Codex/Hive engineering delta.
   - If "真真正正对齐" is the goal, implement idle loop behavior: member remains active/idle, polls mailbox, consumes task-list work, sends idle notifications, exits only on shutdown.

6. Add Team task-list parity.
   - Map CC Team = TaskList into Hive's Work Ledger or create a Team task-list projection with owner, blocked/unblocked, claim, complete, and discovery semantics.

7. Add regression tests before changing behavior.
   - 50 custom definitions are rendered and selectable by `subagent_type`.
   - `tools`/`disallowedTools` CC frontmatter round-trips.
   - Background custom definition resumes with identical prompt/tool/model spec.
   - Unsafe child tool frame stays `needs_reconciliation`; replay-safe frame resumes.
   - `team_create` rejects inline members.
   - `spawn_subagent(team_name + name)` creates a member session.
   - Team member can receive a follow-up by name and parent receives completion wake.
   - Long-lived idle behavior, if implemented, survives multiple messages.

## Verification Performed

This was a source audit only. No runtime logic was changed by this document.

Commands used:

```bash
rg -n "getAgentDefinitionsWithOverrides|parseAgentFromMarkdown|function\\s+AgentTool|TeamCreate|resumeAgentBackground|registerAsyncAgent|SendMessage|TaskOutput|spawnTeammate|runAgent|InProcessTeammateTask|teammateMailbox|startInProcessTeammate" /Users/rocky243/vc-saas/free-code-main/src/tools /Users/rocky243/vc-saas/free-code-main/src/tasks /Users/rocky243/vc-saas/free-code-main/src/utils -S
rg -n "build_subagent_listing_section|Session Worker Types|Custom Session Worker Definitions|spawn_subagent|team_create|AgentTeam|resume_persisted_subagent_runs|start_subagent_run|definition_name|subagent_type" backend/app backend/tests -S
```

No tests were run because this artifact is analysis/documentation-only.
