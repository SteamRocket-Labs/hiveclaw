# Hive Engineering

Current snapshot: 2026-06-28
Product version: 1.7.0 (`backend/VERSION`, `frontend/VERSION`)
Stack: FastAPI, React 19, PostgreSQL, Redis

This document is the engineering map for Hive. It is organized around the real execution path of one agent session, because that is the easiest way to understand the system without getting lost in module lists.

Hive should be understood as an **AI-Native Organization OS**:

1. A self-evolving agent runtime with enterprise-grade access control.
2. A company control plane for creating, operating, auditing, and improving AI digital employees.

The runtime goal is not "make the model answer once." The runtime goal is to keep an agent's identity, session state, tool authority, memory, artifacts, and governance evidence coherent across turns, branches, workflows, subagents, channels, and restarts.

## 1. Core Invariants

These invariants are more important than any individual implementation detail.

| Invariant | Meaning |
|-----------|---------|
| Session is the runtime container | A user-facing conversation is not just chat history. It owns checkpoints, permission profile, runtime metadata, active runs, child sessions, branch lineage, and transcript evidence. |
| RuntimeTask is the active run handle | WebSocket connections, channel callbacks, workflows, and continuations should not be the source of truth for active work. Durable work is represented as `RuntimeTask`. |
| Kernel is stateless and injected | `AgentKernel` does not own database access. Runtime I/O enters through `KernelDependencies`. |
| Tools never bypass governance | All model-called tools must go through `ToolRuntimeService.execute()`. |
| Context is layered | Stable identity belongs in a frozen prefix; changing memory, skills, tools, retrieval, permissions, and runtime state belong in a dynamic suffix. |
| Memory writes are governed | Durable memory and skill evolution require evidence, review, and platform gates. |
| Governance constrains action, not intelligence | Policies decide what an agent may do. They must not replace model reasoning or silently starve context. |
| T0 evidence is replay substrate | Transcript and T0 events are the basis for replay, branch, rewind, compaction, memory, and audit. |

## 2. Top-Level Architecture

```text
Frontend
  React 19, Vite, React Router, TanStack Query, Zustand
  Agent detail, Session Workbench, Company Admin, Platform Admin

Backend
  FastAPI routers
  Web chat runtime
  Unified invoker
  Stateless kernel
  Governed tool runtime
  Memory / Skill / Workflow / Agent Team services

Persistence
  PostgreSQL: tenant-scoped product and runtime state
  Redis: cache, pubsub, coordination support
  Agent filesystem: soul.md, workspace, memory, skills, artifacts
```

Important entry paths:

| Layer | Primary files |
|-------|---------------|
| Chat/session API | `backend/app/api/chat_sessions.py` |
| WebSocket API | `backend/app/api/websocket.py` |
| Durable web chat runtime | `backend/app/services/web_chat_runtime.py` |
| Unified invocation | `backend/app/runtime/invoker.py` |
| Kernel loop | `backend/app/kernel/engine.py` |
| Tool runtime | `backend/app/tools/service.py` |
| Tool governance | `backend/app/tools/governance.py` |
| Agent context | `backend/app/services/agent_context.py` |
| Prompt assembly | `backend/app/runtime/prompt_builder.py` |
| Memory services | `backend/app/memory/`, `backend/app/services/memory_service.py` |
| Workspace skill tools | `backend/app/services/agent_tool_domains/workspace.py` |
| Workflow tools | `backend/app/tools/handlers/workflow.py` |
| Agent Team runtime | `backend/app/services/agent_team_runtime_service.py` |
| Session control projection | `backend/app/services/session_control_plane.py` |
| Frontend session UI | `frontend/src/pages/AgentDetail.tsx`, `frontend/src/pages/agent-detail/` |

## 3. One Session Lifecycle

This is the main loop.

```text
1. User chooses or creates a ChatSession
2. User submits a turn
3. Backend creates or reuses a RuntimeTask
4. Runtime loads session history and projection state
5. Invoker builds execution request
6. Context is assembled
7. AgentKernel runs the model/tool loop
8. Tools execute through ToolRuntimeService
9. Events, messages, artifacts, spans, and T0 evidence are persisted
10. RuntimeTask reaches terminal state or remains resumable
11. Session can branch, rewind, compact, resume, or continue
```

### 3.1 Session Selection

The product surface is `AgentDetail`, but the engineering object is `ChatSession`.

`ChatSession` carries:

- `agent_id`, `user_id`, source channel, title, timestamps.
- branch lineage: root session, parent session, branch metadata.
- active projection metadata for rewind or compaction.
- permission profile metadata.
- session kind for normal chat, team member, channel, or other runtime sources.

The frontend may show different surfaces such as "my conversations" and management views, but all current user turns must resolve to one concrete `ChatSession`.

### 3.2 User Turn Admission

HTTP start path:

```text
POST /agents/{agent_id}/sessions/{session_id}/runs
  -> chat_sessions.start_session_run()
  -> web_chat_runtime.start_web_chat_run()
```

`StartSessionRunIn` includes:

- user content and display content.
- attachments and message parts.
- permission mode.
- optional Plan Mode request.

`start_web_chat_run()` is responsible for the first durable boundary:

- reject expired agents.
- reject empty content.
- check whether another active run already exists for the session.
- queue mid-run user messages when a run is already active.
- create a `RuntimeTask`.
- persist the user message and transcript event when needed.
- capture a checkpoint workspace snapshot for the user turn.
- spawn `execute_web_chat_run()` as background work.

The WebSocket is not the run. It is a subscriber to this durable work.

### 3.3 RuntimeTask Shape

Normal web chat creates:

```text
RuntimeTask(
  task_type="web_chat_turn",
  status="running",
  parent_agent_id=agent.id,
  child_agent_id=agent.id,
  parent_session_id=session.id,
  child_session_id=session.id,
  trace_id="web_chat_turn:<run_id>",
  metadata_json={session_id, runtime_task_id, turn_id, intent_id, permission data, source}
)
```

Other executable chat task types include:

- `goal_continuation`
- `team_member`
- `advanced_plan`

Workflows use `task_type="workflow"` and have their own step/leaf journal semantics, but they still project into the session control plane when attached to a session.

### 3.4 History Projection

Before a run enters the model loop, runtime history can be projected.

Two important projections:

- `compact`: replaces old history with a compacted representation plus later tail messages.
- `rewind`: rebuilds the visible history up to a checkpoint event and keeps later projection tail when appropriate.

This is why clicking a checkpoint in the UI must not automatically execute rewind. Checkpoint selection is navigation. Rewind and Branch are explicit actions from that selected point.

### 3.5 Invocation Request

`execute_web_chat_run()` loads:

- `RuntimeTask`
- `Agent`
- `User`
- primary and fallback `LLMModel`
- `ChatMessage` history
- `ChatSession`

It then builds an `AgentInvocationRequest` for `invoke_agent()`.

Important fields include:

- `model`, `fallback_model`
- `messages`
- `agent_id`, `user_id`
- callbacks for chunk, thinking, tool call, and runtime event streaming
- `session_context`
- `memory_session_id`
- permission metadata
- max tool rounds
- selected tool lists and exclusions
- cancellation event

## 4. Context Assembly

Context assembly is split across stable and dynamic layers.

### 4.1 Frozen Prefix

Built through:

```text
runtime/invoker.py::_build_system_prompt()
  -> services/agent_context.py::build_agent_context()
  -> runtime/prompt_builder.py::build_frozen_prompt_prefix()
```

The frozen prefix is designed to be session-stable. It can include:

- agent identity and role.
- `soul.md`.
- operating contract.
- tone and style.
- subagent listing.
- company information.
- organization structure.
- configured channel notices.
- A2A collaborator context when applicable.

Important rule: canonical T3 memory is not directly loaded in `build_agent_context()`. It flows through the retrieval/memory pipeline and dynamic suffix. This avoids double-injecting the same semantic memory.

### 4.2 Dynamic Suffix

Built through `build_dynamic_prompt_suffix()`.

The dynamic suffix changes every turn and can include:

- memory snapshot from `build_memory_context()`.
- memory navigation.
- Truth Search / retrieval context.
- skill catalog.
- runtime metadata.
- permission context.
- active tool groups.
- available deferred tool names.
- system prompt suffixes from hooks or runtime attachments.
- channel and session state.

This split lets Hive cache stable identity while still refreshing memory, skills, permissions, retrieval, and tool availability every turn.

### 4.3 Context Budget

The invoker computes a `ContextBudget` using model window, query shape, message history, and active tool groups. The budget is stored in session metadata and reused by prompt assembly, memory, skills, runtime metadata, and compaction logic.

### 4.4 Subagent Context Isolation

When `standalone_system_prompt` is set, it replaces the host prompt. This is the CC-style subagent semantic: a spawned worker is a clean specialist, not the host agent plus a suffix. Host memory should not leak into this standalone prompt path.

## 5. Kernel Loop

All normal agent execution enters:

```text
runtime/invoker.py::invoke_agent()
  -> kernel/engine.py::AgentKernel.handle()
```

The invoker does admission and dependency wiring:

- normalize session context.
- check token quota.
- resolve smart model routing.
- build `KernelDependencies`.
- provide tool discovery callbacks.
- provide memory, retrieval, runtime metadata, prompt cache, provider, token, and span callbacks.

The kernel does the model loop:

- resolve runtime config.
- abort fail-closed if tenant resolution fails.
- resolve memory, retrieval, memory navigation, runtime metadata, permissions, and current user.
- assemble system prompt with frozen prefix and dynamic suffix.
- load tools.
- apply coordinator-mode filtering when needed.
- restore deferred tool schemas if recovered from session state.
- record prompt assembly manifest.
- call model.
- stream chunks and thinking.
- parse tool calls.
- execute tools, parallelizing safe batches.
- apply loop guard.
- compact when context pressure requires it.
- return terminal result.

### 5.1 Compaction

Compaction has multiple paths:

- request preflight compaction before model call when context is already too large.
- initial context compaction.
- mid-loop compaction at the configured threshold.
- reactive prompt-too-long retry when provider rejects the prompt.
- microcompaction for old tool results under context pressure.

Compaction emits lifecycle events so the UI and transcript can distinguish normal turns from context-management events.

### 5.2 Tool Result Pressure

Tool result management is part of context correctness:

- per-tool result char limits come from tool metadata.
- global inline result pressure is enforced.
- large tool results can be evicted to workspace-backed artifacts.
- old tool results are cleared only when pressure justifies it.

## 6. Tool Calling Layer

All governed model tool calls enter:

```text
tools/service.py::ToolRuntimeService.execute()
```

Execution order:

```text
Plan Mode read-only block
  -> Plan Mode action gate
  -> runtime context resolution
  -> tool lifecycle created
  -> runtime-owned argument injection
  -> pre-tool hook
  -> input validation
  -> L2 extension policy
  -> governance context
  -> capability / permission / MCP / session policy checks
  -> ActionPreflight
  -> timeout-wrapped execution backend
  -> activity log
  -> post-tool hook
  -> lifecycle completion or failure frame
```

### 6.1 Tool Families

Hive tools include:

- filesystem and workspace tools.
- web search and web fetch tools.
- Office/document tools.
- communication and channel tools.
- memory tools.
- skill tools: `load_skill`, `tool_search`, `save_skill`.
- MCP tools: import, inspect, list resources, read resources, call tools.
- workflow tools: `propose_dynamic_workflow`, `preview_workflow`, `start_workflow`.
- subagent and Agent Team tools.
- work ledger tools.
- triggers and task tools.

The important distinction is not the tool name. The important distinction is whether the tool is core, L2 extension, MCP-backed, deferred, session-local, external-visible, or destructive. Governance decides the actual authority.

### 6.2 Deferred Tools and ToolSearch

Core tools can be always visible. Heavy or optional tools should be deferred:

1. The model sees that more tools may exist.
2. It calls `tool_search`.
3. The runtime resolves matching schemas.
4. The session records active tool groups.
5. Future turns can recover or restore those schemas.

This keeps the base prompt smaller while still allowing broad tool capability.

### 6.3 MCP

MCP import and execution are not blind pass-through:

- imported tools are represented as Hive `Tool` rows.
- canonical names can follow the `mcp__server__tool` style.
- `list_mcp_tools` and `inspect_mcp_tool` expose imported tool state.
- protocol resources use list/read resource handlers.
- execution checks MCP mode: allow, approval, deny.
- token passthrough and unsafe cloud transport patterns are rejected by MCP authz.

## 7. Session Operations

Session operations must match backend semantics. The frontend serves the backend contract, not the reverse.

### 7.1 Checkpoint

A checkpoint is a navigation and state anchor.

When the user submits a turn, the runtime captures a checkpoint workspace snapshot tied to the user transcript event. The UI can show a Git-line style timeline, but clicking a checkpoint should only move the session viewport/focus to that point. It should not run rewind.

### 7.2 Rewind

Rewind means: project the current session back to the selected checkpoint state.

The backend projection uses the checkpoint transcript event, rebuilds history up to that event, and keeps projection metadata. Rewind is explicit. It is not the same thing as hovering or clicking a timeline marker.

### 7.3 Branch

Branch is non-destructive. It creates another `ChatSession` from an anchor event:

```text
POST /agents/{agent_id}/sessions/{session_id}/branches
  -> create_conversation_branch()
  -> optionally start_web_chat_run() in the new branch session
```

The new branch has its own session ID and lineage metadata. Future messages in that branch create checkpoints on that branch, not on the original line.

### 7.4 Compact

Compaction replaces part of the projected history with a smaller representation. It should be visible as a session/runtime event and must preserve enough identity, memory, work ledger, recent files, and recovery state to continue safely.

### 7.5 Permission Resume

Pending tool permission is represented as a runtime frame. User resolution can resume the same session/run path through the session permission continuation flow. The resolved permission profile is carried in session/runtime metadata.

## 8. Memory

Hive memory is a governed evidence pipeline.

```text
Runtime event / transcript
  -> T0 raw evidence
  -> T2 segment package
  -> optional T2 episode stitching
  -> T3 accepted semantic memory
  -> soul.md / skill candidate / prompt activation
```

### 8.1 T0

T0 is the raw session evidence layer. It stores what happened: user, assistant, tools, hooks, runtime events, task events, channel events, workflow events, and boundaries.

T0 matters because it is the replay and audit substrate. Later summaries must point back to evidence; they cannot become ungrounded platform-authored memory.

### 8.2 T2

T2 segment packages summarize and label bounded pieces of T0. They carry source refs, review metadata, and enough evidence to support later T3 decisions.

### 8.3 T3

T3 is accepted semantic memory. It should contain durable user, worker, episode, and capability knowledge. T3 is not a scratchpad and not a vector-store dump.

### 8.4 Memory Gate and Platform Gate

The LLM can judge, summarize, extract, and propose. The platform enforces:

- evidence refs.
- sensitivity classification.
- permission and principal boundaries.
- dedupe and lifecycle metadata.
- rollback and audit.
- exact commit paths.

This keeps memory AI-native without letting the model bypass governance.

## 9. Skill System

Skills are progressive capability capsules.

A skill can contain:

- `SKILL.md` instructions.
- references.
- templates.
- scripts.
- evals.
- workflow definitions.
- subagent definitions.

Loading a skill adds guidance and context. It does not magically grant execution authority. Executable parts still run through governed tools, workflow, subagent, delegation, or sandbox/code execution.

### 9.1 Active Skills

Active skills live in the agent workspace and are discoverable by `load_skill`. The skill catalog is injected dynamically so updated skills do not invalidate the frozen prompt prefix.

### 9.2 Skill Candidates

Direct active skill mutation is retired. `save_skill` creates inactive candidate packages. Promotion requires guard checks and verification before activation.

### 9.3 External Skills

Imported skills must pass the installer/guard path before becoming active. Imported files are materialized into controlled workspace paths and should fail closed on unsafe package shape.

## 10. Workflow, Subagent, and Agent Team

These three concepts are related but not equivalent.

### 10.1 Subagent

`spawn_subagent` creates a session-local worker with isolated context. It is appropriate for parallel investigation, verification, critique, or specialist work inside the current session.

### 10.2 Agent Team

Agent Team is a session-local team container.

Current semantic:

```text
team_create -> creates the Team container only
spawn_subagent(team_name + name) -> creates a teammate child session
enter_agent_team_member -> returns or opens the member ChatSession
```

Agent Team members are addressable sessions. The UI should allow switching into those sessions, not just viewing a summary.

### 10.3 Dynamic Workflow

Dynamic Workflow is a structured run:

```text
propose_dynamic_workflow
  -> preview_workflow
  -> start_workflow
  -> RuntimeTask(task_type="workflow")
  -> workflow steps / leaves
  -> status projection
```

Workflow leaf execution commonly uses subagent-style workers, but a workflow run is not the same as Agent Team. The UI should primarily show workflow phase, step, leaf status, prompt, token/tool usage, result, and error. Only leaf nodes with a real child session should support session entry.

### 10.4 A2A Collaboration

A2A-style collaboration is the cross-agent layer. It should be understood separately from session-local subagents and deterministic workflows:

- relationship and authorization decide who may collaborate.
- message/delegation surfaces carry the work.
- process graph and artifacts can be built on top.

## 11. Governance Architecture

Hive governance has several layers.

### 11.1 Identity and Tenant Boundary

- User, tenant, owner, and company are explicit runtime facts.
- PostgreSQL RLS enforces tenant isolation.
- Runtime paths must fail closed when tenant resolution fails.
- Agent lifecycle state can block invocation.

### 11.2 Session Permission

Each session can carry a permission profile:

- mode.
- allowed tools.
- writable roots.
- pending permission frames.
- allow once / allow session / deny resolution.

This is the UI-level expression of runtime authority.

### 11.3 Capability and Pack Policy

Tools are not allowed only because they exist. Capability and pack policy decide whether the tenant and agent may use them.

### 11.4 Action Preflight

External-visible, sensitive, irreversible, or company-boundary actions require preflight. Preflight can allow, block, or require confirmation.

### 11.5 Hooks

Hooks surround runtime events:

- prompt submit.
- pre-tool.
- post-tool.
- tool failure.
- session idle.
- session close.
- permission request.
- compaction lifecycle.

Hooks can add context, block, modify arguments, rewrite output, or emit audit events depending on their type.

### 11.6 Audit and Trace

Important evidence surfaces:

- `ChatTranscriptEvent`
- `ChatMessage`
- `RuntimeTask`
- `invocation_spans`
- tool lifecycle records.
- tool execution frames.
- session control projection.
- T0 memory events.

## 12. Frontend Session Workbench

The frontend should express backend runtime truth.

Fixed high-level layout:

```text
Left navigation      Center session workbench        Right runtime/workspace rail
existing app nav     active ChatSession view         docs, runtime, agents, workflows
```

Important constraints:

- Do not move the existing left navigation unless product direction changes.
- The center panel is the current session.
- The right rail is for session-native supporting state, not unrelated management widgets.
- Left and right sidebars can resize; the chat/workbench must adapt.
- The bottom composer keeps its core frame and should sit close enough to the session content to avoid dead space.

### 12.1 Checkpoint Timeline

The Git-line style checkpoint UI is navigation first:

- hover shows checkpoint summary.
- click focuses the session at that checkpoint.
- explicit actions at that point can run Rewind or Branch.
- branch lines should be visually restrained: enough color/marking to identify lineage, not a fake full Git client.

### 12.2 Agent Team UI

Agent Team is session switching:

- running member list can live in the right runtime panel.
- clicking a member switches the center panel into that member session.
- active tab color should show which session is currently focused.
- member status, tool count, tokens, and elapsed time should be visible.

### 12.3 Dynamic Workflow UI

Workflow is status inspection first:

- root node opens workflow run overview.
- phase/step/leaf list shows progress.
- leaf detail shows prompt, status, tokens, tool count, result, sources, error.
- enter child session only when `child_session_id` exists.

### 12.4 Command UI

Slash commands and command menus should map to backend command semantics:

- `load_skill` starts a skill-guided turn.
- `tool_search` discovers deferred tools.
- `workflow` opens workflow preview/start path.
- Agent Team commands create containers or members according to backend rules.
- `rewind` and `branch` operate on selected checkpoint anchors, not arbitrary message buttons.

## 13. Startup and Background Work

Backend startup is responsible for:

- loading settings and validating production secrets.
- initializing secrets provider.
- creating or migrating database structures.
- seeding tools, skills, default agents, and default company records.
- running compatibility and hygiene repair where configured.
- resuming active web chat runs after restart.
- starting trigger, channel, heartbeat, dream, evolution, and workflow-related daemons.

Runtime recovery matters because long-running agent work should survive browser disconnects and process restarts.

## 14. Development Commands

Backend:

```bash
cd backend
source .venv/bin/activate
ruff check app/ --fix && ruff format app/
pytest
alembic heads
alembic upgrade head
```

Frontend:

```bash
cd frontend
npm run dev
npm run build
npm test
```

Full local stack:

```bash
bash restart.sh
```

Docker:

```bash
docker compose up -d --build
```

## 15. How to Read or Modify the System

For a session/runtime bug, follow this order:

1. Start with `backend/app/api/chat_sessions.py` or `backend/app/api/websocket.py`.
2. Trace to `backend/app/services/web_chat_runtime.py`.
3. Check `RuntimeTask` metadata and `ChatSession` metadata.
4. Trace `AgentInvocationRequest` into `backend/app/runtime/invoker.py`.
5. Check context assembly in `agent_context.py` and `prompt_builder.py`.
6. Trace model/tool behavior in `kernel/engine.py`.
7. Trace tool authority in `tools/service.py` and `tools/governance.py`.
8. Confirm transcript, T0, spans, and runtime events.
9. Only then change frontend projection.

For a governance bug, start at the call site but prove the result at the governance layer:

```text
frontend action / model tool call
  -> ToolRuntimeService
  -> ToolGovernanceContext
  -> capability / session permission / MCP / preflight
  -> lifecycle frame / transcript event / audit span
```

For a memory or skill bug, never fix only the prompt. Verify the evidence path:

```text
T0 evidence
  -> candidate package
  -> review/gate
  -> accepted file or rejected candidate
  -> prompt activation
```

## 16. Documentation Boundary

The local `docs/` directory is intentionally ignored by Git. Remote-facing engineering documentation should live in:

- `README.md`
- `README.zh-CN.md`
- `ENGINEERING.md`
- `AGENTS.md`
- `CLAUDE.md`

Use local `docs/` for planning, audits, and working notes. Do not link README or ENGINEERING to ignored docs as canonical remote documentation.
