# CCPlus Session-Native Closure Gap Ledger

Date: 2026-06-25

Status: canonical ledger plus current implementation evidence

## 0. Purpose

This document defines the full session-native system Hive must reach before calling CCPlus complete.


```text
accepted input -> transcript/T0 append -> model loop -> hook loop -> permission loop -> tool loop
-> background/child work -> artifacts -> user confirmation/steering -> continuation/resume/export
```

Every runtime effect that matters to the user or the model must be visible, replayable, and continuable from the Session / T0 envelope. Workspace files, admin pages, approval queues, RuntimeTask rows, workflow journals, and audit tables may remain storage or control-plane surfaces, but they must not be the only place where the work is understandable.

## 1. Baseline Rule

CC / FreeCode is the semantic baseline. Codex contributes engineering control and observability.

Use this ordering:

1. CC session semantics define what must exist inside the session.
2. Codex-style typed thread / turn / notification / interrupt / export surfaces improve how Hive exposes and controls it.
3. Hive enterprise rules sit above the session substrate, but only one hard rule is active now: non-admin users cannot delete Agents.
4. Tenant-wide enterprise Hook governance, rule DSLs, assignment, and protected company assets are deferred until after the CC Hook substrate exists.

## 2. What "Session-Native" Means

A capability is session-native only when all of these are true:

1. The accepted user or agent input is durably appended before runtime execution.
2. The active run has `session_id`, `runtime_task_id`, and turn correlation.
3. Tool calls, hook decisions, permission prompts, and failures appear in the session timeline when they affect execution.
4. Background work returns through child sessions, task notifications, or replayable child segments.
5. User-facing artifacts are clickable in the session, even when the file lives in workspace storage.
6. Human confirmation / denial / steering happens inside the same session.
7. Resume, interrupt, branch, fork, compact, export, and close operate on the session, not on isolated task summaries.
8. The session JSON / T0 envelope can replay enough state to explain what happened.

If a feature can only be reconstructed from a workspace file, admin approval row, workflow journal, local event table, notification, or `RuntimeTask.result_summary`, it is not complete.

## 3. Current Code Facts

These facts were checked against current files before writing this ledger:

| Area | Current evidence | What it means |
| --- | --- | --- |
| Session read/export/steer | `backend/app/api/chat_sessions.py` exposes session export, active run, turn steering, and thread-style read aliases. | Codex-style read/steer/export substrate exists. |
| Session permission prompt | `resolve_session_permission` resolves `allow_once`, `allow_session`, and `deny` in the same session, persists `session_permission_decision`, executes the original tool when allowed, and starts a continuation run. | Core in-session permission loop exists. |
| PermissionRequest / PermissionDenied hooks | `run_tool_governance` emits `PermissionRequest` before session-local prompts, consumes hook allow/deny/updatedInput decisions, and emits `PermissionDenied` for hook, mode, and user-denied paths. | CC permission hook lifecycle is now wired into the session permission substrate. |
| Interactive pause detection | `web_chat_runtime.py` detects `session_permission_required`, `ask_user_question`, `request_plan_mode`, `exit_plan_mode`, and `create_digital_employee_success` as interactive pause / terminal states. | Some interactive control states are session-aware. |
| Artifact delivery | `chat_artifact_delivery.py` registers artifacts for file and Office writes and preserves `revision_id`, `action`, `tool_call_id`, and `diff_summary`. | File/Office artifact loop now carries revision metadata into session parts. |
| Sub-agent background | `spawn_subagent(run_in_background=true)` returns `run_id`, `child_session_id`, continuation address, transcript refs, and terminal child-session projection into the parent session. | Child-session completion is visible from the parent timeline. |
| Hook runtime | `runtime/hooks.py` owns the Hive Hook wire standard, including the 27 baseline event names and JSON/exit-code normalization into `HookResult`; `hook_runner.py` is only the deferred external runner that persists progress/attachments/summary events when enabled. | Hook event standard and output parser are implemented from a single runtime control module. |
| Goal / once / schedule / team commands | `/goal`, `/schedule`, `/once`, and `/team` command APIs append session-native events when invoked with a session. | Command-created background/control work is now projected back into the originating session. |
| Session native controls | `SessionNativeControls.tsx` exposes JSON export, hook control plane, session goal, advanced plan, and Agent Team operations. | Codex-style control panel exists but is still a mixed control surface; not all controls are tied to session timeline events. |

## 4. Full CC Session Capability Map

| Capability | CC session behavior | Hive target |
| --- | --- | --- |
| User prompt submit | Append user input before model loop; hook can inspect / inject context. | `USER_PROMPT_SUBMIT` event after durable append, before invocation. |
| Session start/end | Session has explicit lifecycle and transcript truth. | `SESSION_START` / `SESSION_END` events in T0 and JSON export. |
| Tool loop | Tool call, permission, result, failure, and hook effects flow through transcript. | Tool call cards, tool results, failures, and hook effects are session events. |
| Permission prompt | Ask/allow/deny happens in session; hooks may affect permission. | Session permission card with allow once / allow session / deny, plus continuation. |
| Hook loop | 27 lifecycle events; hooks can observe, block, inject context, rewrite input/output, or run async. | Complete CC Hook contract plus durable invocation records and session projection. |
| Plan Mode | Planning and approval are session controls, not global settings. | Plan request, plan card, approval/rejection, and post-plan execution handoff inside session. |
| Elicitation / clarification | Tool or runtime can ask user for structured input and continue. | User clarification / MCP elicitation cards with typed response and continuation. |
| Sub-agent / team | Child work can run separately but rejoins via task notification / sidechain transcript. | Child sessions with parent timeline card, wake, read/send/wait/interrupt/close operations. |
| Workflow | Deterministic work can run in steps but must report state and outputs back to session. | Workflow run tree, step events, gate/wait states, final artifacts, and export in session. |
| Workspace files | Disk is storage; session shows file output/path/preview. | Workspace remains storage; session owns artifact delivery and revision cards. |
| Office/docs | Editor is storage/editing surface; created/modified document appears in session. | Office artifacts in session with open/preview/download and revision history. |
| Tasks/goals/schedules | Background work may run later but each execution has session evidence. | Goal/once/scheduled fire creates or binds a session and posts completion there. |
| Memory/evolution | Runtime can produce learning/candidates, but evidence remains tied to session. | Session-originated memory/evolution candidates and gate outcomes appear in session. |
| Compact/resume/fork/branch | Transcript operations preserve replay and continuation. | Codex-style thread/read/fork/steer/interrupt/export over Hive sessions. |

## 5. Gap Ledger

### P0 - Session Runtime Backbone

| Gap | Current state | Required closure | Codex-style improvement |
| --- | --- | --- | --- |
| Turn state machine | Active run and steering exist, but hook/wait/child/wake states are not fully typed. | Standard states: `running`, `waiting_for_permission`, `waiting_for_user`, `blocked_by_hook`, `waiting_for_child`, `waiting_for_workflow`, `completed`, `failed`, `cancelled`. | Typed turn notifications and inspector badges. |
| Export completeness | Session JSON export exists. | Export must include hook invocations, child sessions refs, workflow tree, artifacts, approvals, compactions, memory candidates, and branch edges. | Stable export schema with versioning and schema tests. |

### P0 - Hook Contract

| Gap | Current state | Required closure | Codex-style improvement |
| --- | --- | --- | --- |
| Event set mismatch | Closed for Hive Hook wire standard: `HOOK_WIRE_EVENTS` matches the 27 FreeCode baseline events exactly; Hive internal enum names remain internal. | Keep the single standard mapping tested when new Hive events are added. | Typed enum version, migration-safe aliases. |
| Hook output semantics | Closed for command/JSON parser: wire fields `continue`, `decision`, `hookSpecificOutput`, exit code `2`, nonzero diagnostics, `async`, and permission payloads normalize into `HookResult`. | Keep adding event-specific consumers as runtime paths need them. | Schema validation and detailed rejection errors. |
| Invocation storage | Hook config exists; durable invocation record is not the canonical evidence surface. | Add `HookInvocation` or equivalent append-only event records with IDs, input hash, output, status, duration, correlation IDs. | Inspector and export use the same records. |
| Blocking projection | Hook block can affect execution, but session projection is not complete. | Any blocking hook must create a session-visible card / event with reason and continuation state. | `blocked_by_hook` typed turn state. |
| Async hook | Contract not complete. | Support `async: true` with background execution, timeout, trace, and optional session notification. | Non-blocking hook progress channel. |
| Hook sources | Agent-scoped runtime config exists. | Add settings/plugin/skill/internal hook source model before tenant governance. | Namespaced sources and dedupe. |

### P0 - Permission And Approval

| Gap | Current state | Required closure | Codex-style improvement |
| --- | --- | --- | --- |
| Session permission | Core allow/deny loop exists; allow once/session and deny are resolved in the originating session. | Add stricter pending request store/expiry/stale response handling; add path/domain/command-family scoped session allowances. | Typed permission request IDs and resumable wait state. |
| Permission hook race | Hook side is implemented: `PermissionRequest` can allow/deny/updatedInput before UI prompt; human deny emits `PermissionDenied`. | Add deterministic first-wins race cancellation between long-running async hooks and user UI prompts. | Deterministic race resolution with event audit. |
| Enterprise approval projection | Backend approval rows are outside session. | If a session triggers enterprise approval, session shows pending/resolved and wake/continue. | Session card links to admin approval but preserves origin state. |
| Permission modes | Composer exposes `auto/default/bypassPermissions`; compatibility modes hidden. | Keep `dontAsk/plan/acceptEdits` backend-compatible; do not expose in composer. | Store permission snapshot per turn. |

### P0 - Work Product Delivery

| Gap | Current state | Required closure | Codex-style improvement |
| --- | --- | --- | --- |
| Artifact revision | Backend artifact parts preserve `revision_id`, `action`, `tool_call_id`, and `diff_summary`. | Render created/updated/finalized revision timeline in session UI. | Diff-aware cards and stable artifact IDs. |
| Workspace bridge | Workspace can be used as separate browser/editor. | Session-initiated workspace changes append artifact/update events back to session. | Inspector shows storage path and session provenance. |
| Final response delivery | Assistant may still point to workspace. | Final answer must include clickable artifact part for delivered files. | "deliverable" field in session export. |


| Gap | Current state | Required closure | Codex-style improvement |
| --- | --- | --- | --- |
| Workflow export | Workflow run/step events now enter session transcript. | Ensure export folds in full workflow journal and leaf artifacts, not just projected events. | Typed workflow timeline cell model. |
| Research final handoff | Final report may be a workspace file. | Final report must render in session as clickable artifact with preview. | Artifact plus source-map inspector. |

### P0 - Sub-agent / Team / A2A

| Gap | Current state | Required closure | Codex-style improvement |
| --- | --- | --- | --- |
| Background subagent | Returns `run_id`, `child_session_id`, continuation address, and projects terminal child-session status into parent timeline. | Add full parent card controls: wake, result, send-followup, wait, interrupt/close where supported. | `read_agent_session`, `send_agent_session_message`, `wait_agent_sessions`. |
| Inline subagent | Returns digest only. | At least replayable child segment/event, even if unlisted. | Inspector can open source refs without cluttering main timeline. |
| Agent Team | Workbench controls exist. | Team creation, member enter/idle/close/consolidation must be timeline events and exportable. | Team member sessions as child threads. |
| A2A delegation | Partially session-backed in design/code. | Peer delegation must be session-first, not task-result-summary-first. | Same child session operation API as subagent/team. |

### P0 - Goals, Once, Schedule

| Gap | Current state | Required closure | Codex-style improvement |
| --- | --- | --- | --- |
| Session goal | `/goal start/update/stop` append `goal` events into the session when invoked from a session. | Add live progress/budget/completion projection for continuation runs. | Goal panel bound to session export. |
| One-off task | `/once` persists one-shot schedule draft and appends `once` event into the session. | Ensure the eventual one-shot fire creates/binds a run session and posts result/artifacts there. | Separate command wrappers but same session evidence model. |
| Scheduled task | `/schedule` persists schedule draft and appends `schedule` event; workflow_ref trigger fires create/bind `trigger_run` sessions. | Ensure all schedule fire branches, including non-workflow ReAct and workflow paths, post final artifacts/results in session. | Schedule run history links to sessions. |

### P1 - Memory / Knowledge / Evolution

| Gap | Current state | Required closure | Codex-style improvement |
| --- | --- | --- | --- |
| Memory candidates | Memory write system exists outside this doc. | Session-originated memory/evolution candidates and gate outcomes appear in session. | Memory candidate cards with evidence refs. |
| Skill evolution | Skill library/control plane exists separately. | Skill candidate creation/promotion requests from a session appear in session. | Candidate artifact + eval result links. |
| Knowledge writes | Knowledge/admin surfaces are outside session. | Session-triggered knowledge ingestion/write displays status and resulting artifact/ref in session. | Knowledge source refs in session inspector. |

### P1 - UI / Composer / Commands

| Gap | Current state | Required closure | Codex-style improvement |
| --- | --- | --- | --- |
| Composer command surface | Slash commands and plus menu exist, but command collapse still needs product discipline. | User commands expose small wrappers; backend raw capabilities stay hidden. | Command palette inserts plain command + prompt, not raw JSON templates unless required. |
| Permission selector | Composer menu exists. | It must remain session-local and show current mode state consistently across refresh/session switch. | Permission snapshot per turn in inspector. |
| Session timeline | Active-run cell exists. | Add timeline cells for hooks, workflow tree, child sessions, schedule fires, memory candidates, artifact revisions. | Typed timeline model, no ad hoc message parsing. |

## 6. Implementation Order

Do not implement this as disconnected UI fixes. The order should be:

1. Canonical event contracts:
   - CC 27 Hook events.
   - Session timeline event types.
   - Artifact revision event types.
   - Child session edge schema.

2. Backend substrate:
   - Hook invocation records and CC-compatible schemas.
   - Session topology API.
   - Artifact registry and revision metadata.
   - Schedule/once/goal session binding.

3. Frontend session workbench:
   - Inspector panels from typed APIs.
   - Composer and command surface cleanup.

4. Verification:
   - Unit tests for contracts and serializers.
   - Backend tests for each session-native closure path.
   - Frontend tests for each timeline cell and composer flow.
   - Export tests proving the session/T0 envelope contains the relevant evidence.

## 7. Acceptance Criteria

Hive can call this layer complete only when all are true:

- The canonical Hook event set matches CC's 27 events.
- Every blocking hook, permission prompt, clarification, Plan Mode card, workflow wait, subagent wake, and schedule fire is visible in the originating session.
- Every produced or modified user-facing artifact is clickable from session.
- Workflow state is readable from session, not only from workflow journals.
- Background subagent/team/A2A work is continuable through child sessions.
- Goal/once/schedule execution creates or binds a session and delivers result there.
- Session export includes enough event data to replay the lifecycle without consulting admin pages as the primary truth.
- Enterprise governance beyond "non-admin cannot delete Agent" remains explicitly deferred and does not block CC parity.

## 8. Non-Goals For This Pass

- Do not design tenant-wide enterprise Hook policy, rule DSL, assignment, or custom governance UI.
- Do not make every Skill/Sub-agent/Workflow immutable; only preserve the future promotion boundary.
- Do not move admin pages into chat.
- Do not expose raw backend functions as user slash commands.
- Do not run user Python or command hooks inside the API process.
