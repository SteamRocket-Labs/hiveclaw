# Conversation Experience Parity Plan: Claude Code / Codex

Date: 2026-06-22

## Goal

Hive web chat should match Claude Code / Codex conversation ergonomics for every non-memory behavior:

- resume a conversation
- start a new conversation while another run is still active
- fork/branch from any prior message or transcript event
- edit a prior user message and rerun from that point
- insert a user message before/after an existing event and continue
- regenerate from an assistant response boundary
- reply to a specific message with an explicit anchor
- keep active run state visible across sessions
- keep attachments/images semantically intact, including messages queued during an active run

Hive memory remains Hive-specific and governed. Conversation branching must not mutate durable memory directly.

## Current Source-Backed State

### Existing substrate

- `backend/app/models/chat_session.py` already has `parent_session_id`, `root_session_id`, `runtime_task_id`, and `transcript_metadata_json`.
- `backend/app/models/chat_transcript_event.py` already has `parent_event_id`, `root_session_id`, `parent_session_id`, `message_id`, `parts_json`, and `metadata_json`.
- `backend/app/services/chat_transcript.py` already defines `append_session_event(...)` as the single event writer and states `ChatMessage` is a read model.
- `backend/app/api/chat_sessions.py` already exposes session list/create/delete, run start/active/cancel, transcript read, and legacy message read.
- `backend/app/services/web_chat_runtime.py` enforces one active web-chat run per session, not per agent.
- `frontend/src/pages/AgentDetail.tsx` already tracks active run state by `agentId:sessionId`.

### Current hard gaps

- There is no chat API for branch/fork/edit/insert/reply/regenerate.
- `frontend/src/api/domains/chat.ts` exposes only list/create/delete/get transcript/start active run/cancel.
- `frontend/src/pages/agent-detail/AgentChatSection.tsx` only exposes copy as a message action.
- `frontend/src/pages/agent-detail/chatRuntime.ts` replays transcript as one linear sequence; it ignores parent event/session lineage in the UI projection.
- `backend/app/models/audit.py::ChatMessage` is flat: no parent message, revision, insertion anchor, or branch edge. This is acceptable only if `ChatMessage` remains a read model.
- Mid-run queued messages are stored as plain text in `RuntimeTask.metadata_json.pending_user_messages`; file/image semantics are collapsed before the kernel drains them.

## Architectural Decision

Make `chat_transcript_events` the canonical conversation event stream and treat each visible conversation branch as a `ChatSession`.

Do not destructively edit old messages. Every "edit", "insert", "fork", or "regenerate" creates a new branch session from a checkpoint event.

This gives CC/Codex-style ergonomics while preserving Hive auditability:

- old branch remains replayable
- new branch has explicit provenance
- memory extraction can cite the actual branch/event source
- rollback and comparison are possible
- active runs remain isolated per session

## Backend Contract

Add a `ConversationBranchService` with these operations:

- `fork_from_event(source_session_id, from_event_id, title?)`
- `edit_from_event(source_session_id, target_event_id, replacement_content, display_content?, attachments?)`
- `insert_after_event(source_session_id, after_event_id, content, display_content?, attachments?)`
- `insert_before_event(source_session_id, before_event_id, content, display_content?, attachments?)`
- `reply_to_event(source_session_id, parent_event_id, content, display_content?, attachments?)`
- `regenerate_from_event(source_session_id, assistant_event_id)`

All operations should:

- authorize through the same session access path as `chat_sessions.py`
- create a new `ChatSession`
- set `parent_session_id` to the source session
- set `root_session_id` to the source root or source session
- record branch metadata in `transcript_metadata_json`
- copy or reference the prefix transcript up to the checkpoint
- append the synthetic user operation event when applicable
- start a new web chat run when the operation implies rerun/continue

Recommended endpoints:

```text
POST /api/agents/{agent_id}/sessions/{session_id}/branches
GET  /api/agents/{agent_id}/sessions/{session_id}/branches
GET  /api/agents/{agent_id}/sessions/{session_id}/lineage
```

Request shape:

```json
{
  "mode": "fork|edit|insert_before|insert_after|reply|regenerate",
  "anchor_event_id": "uuid",
  "content": "string",
  "display_content": "string",
  "attachments": [],
  "title": "optional string",
  "start_run": true
}
```

Response shape:

```json
{
  "session": {},
  "branch": {
    "mode": "edit",
    "source_session_id": "uuid",
    "root_session_id": "uuid",
    "anchor_event_id": "uuid",
    "created_event_ids": ["uuid"]
  },
  "run": {}
}
```

## Runtime Contract

Active runs stay per session. Starting a new session/branch must not cancel another session's active run.

The runtime must support branch checkpoint replay:

- build LLM conversation from the selected branch transcript, not from flat `ChatMessage` rows
- preserve assistant thinking signatures where available
- preserve tool result visibility metadata
- treat branch prefix events as immutable context
- keep active run/cancel scoped to the current session only

Mid-run user messages must become structured queue items:

```json
{
  "id": "uuid",
  "display_content": "visible text",
  "llm_content": "provider-ready text or multimodal payload",
  "attachments": [],
  "parts": [],
  "created_at": "iso timestamp"
}
```

The kernel drain path must accept both string content and structured multimodal content. Until this is done, enabling file/image upload during active run is unsafe because the queue can silently degrade attachments.

## Frontend Contract

Add a per-message action menu:

- Copy
- Reply
- Edit and rerun
- Insert before
- Insert after
- Fork from here
- Regenerate from here

UI state requirements:

- message actions must use transcript `event.id` as the anchor, not array index
- session sidebar shows active-running badges per session
- branch sessions show provenance/breadcrumbs
- switching sessions must not clear another session's active run state
- queued mid-run messages are visibly marked as queued until drained
- attachments can be attached during an active run only after structured queue support lands

API adapter additions in `frontend/src/api/domains/chat.ts`:

```ts
branchSession(agentId, sessionId, input)
listSessionBranches(agentId, sessionId)
getSessionLineage(agentId, sessionId)
```

## Test Plan

Backend tests:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/api/test_chat_session_branches.py tests/services/test_conversation_branch_service.py tests/services/test_web_chat_runtime.py -q
```

Required backend cases:

- fork creates a new session with parent/root lineage
- edit creates a new branch and does not mutate the source transcript
- insert before/after places the synthetic user event at the correct checkpoint
- regenerate anchors to the assistant event and reruns from the preceding context
- unauthorized user cannot branch another user's session
- two sessions can have active runs concurrently
- same session still allows only one active run
- queued mid-run attachments survive kernel drain with full semantics

Frontend tests:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/chatRuntime.test.ts
npm run build
```

Required frontend cases:

- message action menu renders only valid actions for each role/event type
- edit/fork/insert/regenerate call the branch API with transcript event id
- selecting a new session while an old one runs keeps the old run badge
- queued messages render as queued and later reconcile when transcript events arrive
- transcript replay is stable for branch metadata and does not duplicate events

## Implementation Order

This is one complete feature, but it should land as tight, tested slices:

1. Backend branch service + API + tests.
2. Runtime transcript replay from branch checkpoints + structured mid-run queue + tests.
3. Frontend API adapter + per-message action menu + branch/sidebar UX + tests.
4. Migration/backfill/observability pass for production sessions.
5. Full backend/frontend verification and Railway deploy only after local proof.

## Production Rollout Checks

Before deploy:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
alembic heads
pytest tests/api/test_chat_session_branches.py tests/services/test_conversation_branch_service.py tests/services/test_web_chat_runtime.py -q
ruff check app/api/chat_sessions.py app/services/web_chat_runtime.py app/services/chat_transcript.py

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/chatRuntime.test.ts
npm run build
```

After deploy:

- create a long-running session
- open a new session while the old run continues
- fork from a previous user message
- edit and rerun from a previous user message
- insert a message into the middle and continue
- regenerate from an assistant message
- queue a text message during active run
- queue a file/image during active run only after structured queue support is live

## Code-Level Closure, 2026-06-22

Implemented locally, not yet deployed:

- Transcript-anchored branch API: `fork`, `edit`, `insert_before`, `insert_after`, `reply`, and `regenerate` create new `ChatSession` branches with parent/root lineage instead of mutating source history.
- Branch lineage API and frontend branch tree are live in the chat sidebar.
- Message actions no longer use browser prompts. `edit`, `insert_before`, `insert_after`, and `reply` open an in-app branch compose panel; `fork` and `regenerate` execute directly.
- Active-run composer and upload remain usable. Mid-run queued messages preserve `llm_content`, display content, structured parts, and attachment metadata until kernel drain.
- `ask_user_question` answers are durably acknowledged by marking the latest pending clarification transcript event with `answered`, `answered_by_event_id`, `answer_text`, and `answered_at`. Transcript replay reads this metadata so refresh does not resurrect an already answered clarification card.
- T0/T2 boundary is protected: copied branch prefixes are transcript projections only (`projection_only=true`, `semantic_memory_eligible=false`, `bridge_to_t0=false`) and do not become new T0 evidence; T2 source bundle construction defensively excludes projection-only/non-semantic events if a future caller accidentally writes them to T0. `regenerate` carries `semantic_source_refs` to the original user prompt instead of duplicating that prompt into the branch T0 ledger.

Local verification:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_chat_transcript.py tests/services/test_conversation_branch_service.py tests/api/test_chat_session_branches.py tests/services/test_conversation_interaction_service.py tests/services/test_web_chat_runtime.py tests/kernel/test_engine.py tests/memory/test_t2_segment_package_builder.py -q
# 122 passed, 4 warnings

ruff check app/services/chat_transcript.py app/services/conversation_branch_service.py app/memory/t2/segment_package.py tests/services/test_chat_transcript.py tests/services/test_conversation_branch_service.py tests/memory/test_t2_segment_package_builder.py
# All checks passed

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run src/api/domains/chat.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/chatRuntime.test.ts src/pages/agent-detail/AskUserQuestionCard.test.tsx
# 4 files passed, 95 tests passed

npm run build
# passed
```

Remaining boundary: production deployment and live session verification only.
