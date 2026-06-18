# T0 Append-Only Session Ledger Redesign

Date: 2026-06-18
Scope: T0 only
Status: Draft for implementation

Next-layer contract: T0 -> T2 packaging is defined separately in
`docs/t0-to-t2-segment-package-redesign-2026-06-18.md`. This document remains
T0-only.

## 1. Executive Summary

T0 is not memory intelligence. T0 is the raw, replayable evidence substrate.

The current Hive T0 layer writes post-hoc Markdown snapshots through idle/close
hooks. That is not strong enough for resume, replay, source refs, or any future
evidence verification.

T0 must be rebuilt on the same foundation used by Claude Code transcript and
Codex rollout:

- append-only session ledger
- event-first persistence
- explicit session identity
- typed event boundaries
- resumable from the ledger itself
- DB/index/summary are derived read models, never the source of truth

Hive may keep the upper representation MD-first. The semantic foundation,
however, must be an append-only session ledger. In Hive's final shape, that
means Markdown containers with XML event blocks, plus mechanical sidecars for
indexing and integrity.

## 2. Design Law

This layer follows the project memory law:

> LLM 负责判断、提炼、反思、归纳、候选生成；平台负责证据引用、权限、去重、回滚、审计、最终落盘。

For T0 specifically:

- The platform writes raw events.
- The platform may mask or block forbidden sensitive material before durable
  storage.
- The platform must not summarize, classify, score, promote, or rewrite meaning.
- LLM intelligence starts after this raw evidence layer.

T0 is allowed to be mechanical because it is a recorder, not a judge.

## 3. Source Alignment

### 3.1 Claude Code Transcript Principles

Claude Code stores each session as a JSONL transcript under a project-scoped
session file. Its important principles are:

- Persist accepted user messages before the model loop, so `/resume` works even
  if the process is killed before the API returns.
- Store message UUIDs and parent-chain links so the conversation can be
  reconstructed.
- Store compaction as boundary/replacement metadata, not as destructive history
  rewrite.
- Resume from the transcript, not from a summary.
- Treat sidechain/subagent transcripts as related but separately reconstructable
  chains.

### 3.2 Codex Rollout Principles

Codex stores session history as typed rollout JSONL. Its important principles
are:

- `SessionMeta` opens the session with stable metadata.
- `RolloutItem` variants record session meta, response items, compacted items,
  turn context, and event messages.
- Appends are flushed before DB/index metadata is considered current.
- Resume loads rollout items into `InitialHistory::Resumed`.
- Compaction and rollback/interruption are events in history, not silent
  destructive edits.

### 3.3 Hive Principle

Hive should not copy JSONL as the user-facing memory format. Hive should copy
the invariants.

Target statement:

> Claude Code transcript and Codex rollout are append-only session ledgers.
> Hive T0 must become an append-only session ledger too. Hive's durable semantic
> representation can remain Markdown/XML, but the persistence semantics must be
> transcript/rollout-grade.

## 4. Previous Hive T0 Mechanism

Previous code paths:

- `backend/app/services/t0_logger.py`
- `backend/app/runtime/hooks_setup.py`
- `backend/app/services/web_chat_runtime.py`
- `backend/app/models/chat_session.py`
- `backend/app/models/audit.py`

Previous storage layout:

```text
<AGENT_DATA_DIR>/<agent_id>/logs/YYYY-MM-DD/
  behavior/
    chat-{HHmm}-{id}.md
    trigger-{HHmm}-{id}.md
    delegation-{HHmm}-{id}.md
  system/
    heartbeat-{HHmm}-{id}.md
    dream-{HHmm}-{id}.md
  artifacts/
    <tool-result-artifact>.json
```

Previous chat T0 file shape:

- YAML frontmatter
- `## Turn N`
- `**User**`
- `**Agent**`
- `**Tools**`
- optional artifact references for large tool outputs

Previous write timing:

- `SESSION_IDLE` writes an incremental T0 snapshot.
- `SESSION_CLOSE` writes another incremental T0 snapshot.
- Cursor is process-local: `_t0_cursors["agent_id:session_id"] -> message index`.
- `RESPONSE_COMPLETE` separately triggers higher-layer extraction today, but
  that path is outside this T0 redesign.

## 5. Current Fault Lines

### 5.1 T0 Is Delayed

Accepted user messages are persisted in `ChatMessage`, but T0 itself waits for
idle/close hooks. If a process dies before idle/close, T0 evidence may be absent.

Claude Code solves this by writing the transcript immediately after the user
message is accepted.

### 5.2 Cursor Is Not Durable

`_t0_cursors` is in memory. A restart loses it.

This can cause:

- duplicate T0 chunks
- missing chunks after hook failures
- inability to prove what was already sealed

### 5.3 Filename Is Not Event-Safe

`chat-{HHmm}-{short_id}.md` is minute-granularity plus short id. Multiple writes
for the same session in the same minute can collide or become ambiguous.

### 5.4 ChatSession Is Not a Ledger Segment

`ChatSession` is a product conversation container. It can last minutes, days, or
months. A replayable ledger segment must be smaller, sealable, and recoverable.

Therefore:

- ChatSession should not equal T0 source packet.
- ChatSession should contain multiple T0 source packets.
- T0 source packets should be independently replayable and independently
  sealed.

### 5.5 Resume Is DB/Latest-Session Oriented

WebSocket currently accepts an optional `session_id`. If missing or invalid, it
falls back to the latest user+agent session. That is useful UX fallback, but it
is not a reliable transcript/resume foundation.

Resume must be explicit and ledger-backed.

### 5.6 Compaction Is Not a T0 Boundary

T0 currently does not represent compaction, resume, interruption, cancellation,
or rollback as first-class source events.

This makes later reconstruction and evidence audit fragile.

### 5.7 Summary Side Paths Cannot Be Source Truth

`ChatSession.summary` is generated on idle and used by retrieval paths. It can
remain as a read model, but it must not become semantic truth.

The append-only T0 ledger is the source truth for session reconstruction.

## 6. Target Model

### 6.1 Terms

| Term | Definition |
| --- | --- |
| Product ChatSession | User-facing conversation container. Stable across reconnects and explicit resume. |
| Session Ledger | Append-only T0 event ledger under one ChatSession. |
| T0 Source Packet / Segment | A sealed, bounded slice of a Session Ledger. This is the unit that can be replayed or resumed from. |
| T0 Event | One typed raw event: user message, assistant message, tool call, tool result, runtime boundary, etc. |
| Artifact | Large payload stored outside the event body with hash and reference. |
| Read Model | DB rows, indexes, summaries, projections, and UI views derived from the ledger. |

### 6.2 Relationship

```text
ChatSession
  -> Session Ledger
      -> T0 Segment 001
          -> T0 Event...
      -> T0 Segment 002
          -> T0 Event...
      -> T0 Segment 003
          -> T0 Event...
```

### 6.3 Segment Boundaries

A T0 segment should be sealed by any of these conditions:

- idle threshold reached
- context/token threshold reached
- explicit user creates a new conversation
- explicit resume creates a resume boundary
- compaction boundary
- runtime task completed
- cancellation/interruption
- system restart recovery
- channel-level conversation boundary, such as Feishu thread/new command

Important rule:

> Idle seals a segment. Idle does not necessarily close a ChatSession.

## 7. Target Storage Shape

Target durable T0 layout:

```text
<AGENT_DATA_DIR>/<agent_id>/memory/t0/sessions/<chat_session_id>/
  index.json
  segments/
    <segment_id>/
      source.md
```

Decision:

- Use the segmented layout for implementation.
- Each `source.md` is append-only until sealed.
- After seal, it is immutable except for explicitly audited repair records.
- `index.json` tracks active segment, next sequence, sealed segments, and
  legacy import idempotency.

`index.json` is a mechanical sidecar. It is not semantic memory truth. Oversized
runtime artifacts remain separately governed runtime/workspace artifacts unless
a future T0 artifact adapter is added.

## 8. Source Markdown Format

T0 remains MD-first, but every semantic block inside a source file uses XML.

Example:

```markdown
# T0 Session Ledger
schema_version: t0.session-ledger.v1
agent_id: <agent_id>
session_id: <session_id>
segment_id: <segment_id>
created_at: 2026-06-18T00:00:00+00:00

<t0_event id="evt_x" seq="1" event_type="user_message" role="user" created_at="2026-06-18T00:00:01+00:00" message_id="<chat_message_id>" actor_id="<user_id>" tenant_id="<tenant_id>" runtime_task_id="<runtime_task_id>" source="web" sensitivity="PL1_public">
  <content>用户原文</content>
  <metadata>{"source":"web"}</metadata>
</t0_event>

<t0_event id="evt_y" seq="2" event_type="assistant_message" role="assistant" created_at="2026-06-18T00:00:10+00:00" message_id="<chat_message_id>" actor_id="<agent_id>" tenant_id="<tenant_id>" runtime_task_id="<runtime_task_id>" source="web" sensitivity="PL1_public">
  <content>助手回复</content>
  <metadata>{"source":"web"}</metadata>
</t0_event>

<t0_event id="evt_z" seq="3" event_type="segment_boundary" role="system" created_at="2026-06-18T00:30:00+00:00" message_id="" actor_id="" tenant_id="" runtime_task_id="" source="t0_ledger" sensitivity="PL1_public">
  <content>session_idle</content>
  <metadata>{"reason":"session_idle"}</metadata>
</t0_event>
```

## 9. T0 Event Types

Current implemented event types:

| Event type | Purpose |
| --- | --- |
| `user_message` | Raw user input as accepted by runtime. |
| `assistant_message` | Final assistant content. |
| `tool_result` | Tool call/result payload as accepted by runtime. |
| `trigger_run` | Completed trigger run evidence. |
| `delegation_run` | Completed delegation run evidence. |
| `heartbeat_tick` | Completed heartbeat tick evidence. |
| `dream_run` | Completed dream run evidence. |
| `legacy_import` | Imported legacy t0_logger file, idempotent by path + digest. |
| `segment_boundary` | Idle/close/task-complete boundary; seals the active segment. |

Optional later event types:

- `session_started`
- `session_resumed`
- `turn_started`
- `assistant_delta`
- `tool_call`
- `runtime_event`
- `compaction_boundary`
- `content_replacement`
- `error`
- `interrupt`
- `rollback_marker`
- `file_snapshot_ref`
- `attribution_snapshot_ref`
- `approval_request`
- `approval_result`
- `policy_preflight`

Those can be added after the core session ledger is stable.

## 10. Hard Invariants

### 10.1 Append Before Execution

When a user message is accepted:

1. Persist `ChatMessage`.
2. Append `user_message` to T0.
3. Flush T0.
4. Create or continue `RuntimeTask`.

If T0 append fails, the runtime must fail closed or mark the turn as
unrecoverable. It must not silently continue with no ledger entry.

### 10.2 T0 Before Read Models

DB summaries, session indexes, vector indexes, and UI projections cannot be
ahead of T0.

Codex's rule applies:

> The index must never get ahead of the append-only source ledger.

### 10.3 Explicit Resume

Resume must use explicit `chat_session_id` or a verified channel conversation
mapping. Latest-session fallback is allowed only as a UX convenience and must
be recoverable from the session ledger or verified DB-to-ledger reconciliation.

### 10.4 Compaction Is a Boundary, Not Deletion

Compaction must never destroy or rewrite prior raw T0 events. A dedicated
`compaction_boundary` event may be added later, but current T0 integrity does
not depend on compaction rewriting raw events.

### 10.5 Segment Is the Replay Unit

A sealed segment is the smallest durable replay unit.

Replay/read-model derivation must not consume:

- open segments
- unflushed events
- ChatSession summaries as source truth
- legacy behavior logs without import metadata

### 10.6 T0 Does Not Judge

T0 must not perform:

- semantic classification
- memory promotion
- scoring
- tag generation
- reflection
- summary

Allowed mechanical operations:

- schema validation
- sequence assignment
- event id assignment
- privacy gate / PL4 blocking
- artifact spilling
- hashing
- flush / fsync
- index update after ledger flush

## 11. Implementation Impact

### 11.1 Implemented Modules

```text
backend/app/memory/t0/
  __init__.py
  ledger.py
```

Responsibilities:

| Module | Responsibility |
| --- | --- |
| `ledger.py` | Typed append result/event records, XML event block append + fsync, segment open/seal, replay, and idempotent legacy import. |

### 11.2 Modified Modules

| Current module | Change |
| --- | --- |
| `services/web_chat_runtime.py` | Append and flush T0 immediately after user message accepted; append assistant/tool/runtime events at finalization points. |
| `services/task_executor.py` | Append one-off background task user/tool/assistant events to the task reflection session ledger, then seal on completion. |
| `runtime/hooks_setup.py` | Stop using `_t0_cursors` as source of truth. Idle/close should seal segments, not create primary T0 evidence. |
| `services/t0_logger.py` | Retired for runtime T0 truth; retained only for legacy import/manual compatibility. `backfill_recent_chat_logs()` writes the new ledger. |
| `api/websocket.py` | Use explicit resume/session contract; latest fallback emits a source event and is not treated as canonical resume. |
| `services/memory_service.py` | `ChatSession.summary` becomes read model/cache only. |

### 11.3 Database Considerations

Minimum code-first path can avoid a new table by using files plus sidecars.

Recommended durable state table later:

```text
t0_segments
  id
  tenant_id
  agent_id
  chat_session_id
  segment_id
  state
  source_path
  first_event_id
  last_event_id
  event_count
  opened_at
  sealed_at
  sha256
  prev_segment_id
  created_at
  updated_at
```

This table is an index/read model over files. It is not the semantic source.

## 12. Source References

Stable source refs should be URI-like:

```text
t0://agent/<agent_id>/session/<chat_session_id>/segment/<segment_id>
t0://agent/<agent_id>/session/<chat_session_id>/segment/<segment_id>#event/<event_id>
t0://agent/<agent_id>/session/<chat_session_id>/segment/<segment_id>#message/<chat_message_id>
t0://agent/<agent_id>/session/<chat_session_id>/segment/<segment_id>#artifact/<artifact_id>
```

Higher layers can cite these refs later, but T0 implementation is complete when
the refs are stable, resolvable, and replayable.

## 13. Migration Strategy

### 13.1 Preserve Legacy Logs

Do not rewrite or delete existing `logs/YYYY-MM-DD/behavior/*.md` by default.

Legacy logs remain evidence, but they are marked as legacy source.

### 13.2 Import Legacy Logs

Add an importer:

```text
legacy behavior T0 MD
  -> parse frontmatter/body
  -> create imported T0 segment
  -> preserve original_path
  -> preserve artifact refs
  -> write import boundary event
```

Imported segment frontmatter should include:

```yaml
imported_from: logs/YYYY-MM-DD/behavior/chat-....md
imported_at: <timestamp>
legacy_schema: t0_logger.chat_snapshot.v1
```

### 13.3 Backfill Cursor Fix

Current backfill cursor groups by `session_id`. That is unsafe when one
ChatSession has multiple T0 chunks.

New cursor must be segment/event based:

```json
{
  "schema": "hive.t0.backfill_cursor.v2",
  "processed_segments": {
    "<segment_id>": {
      "last_event_id": "<event_id>",
      "sha256": "<segment_hash>"
    }
  }
}
```

### 13.4 Cutover

Cutover order:

1. Add T0 writer and replay tests.
2. Wire web chat user-message append before RuntimeTask creation.
3. Wire assistant/tool/runtime append at existing finalization points.
4. Change idle/close hooks to segment seal only.
5. Add legacy importer.
6. Mark old `t0_logger.write_t0_log` path as legacy compatibility only.

## 14. Redline Tests

T0 is the foundation. The first implementation must include these tests.

### 14.1 Accepted User Message Is Ledgered Before Runtime

Given a new user web turn:

- `ChatMessage(role=user)` is stored.
- T0 has `user_message`.
- T0 is flushed before `RuntimeTask(status=running)` is visible.

Failure expectation:

- If T0 append fails, the turn does not silently proceed.

### 14.2 Process Restart Does Not Duplicate T0

Given a session with existing T0 events:

- Restart clears process memory.
- New event append continues sequence from durable ledger/index.
- No duplicate event is written.

### 14.3 Idle Seals Segment, Does Not Close ChatSession

Given a long-running ChatSession:

- Idle threshold writes `segment_boundary` and seals the active segment.
- Same ChatSession can later append a new segment.
- Replay can choose the sealed segment or continue from the next segment.

### 14.4 Explicit Resume Is Ledgered

Given a request with explicit `session_id`:

- New accepted events append under the existing session ledger.
- Replayed messages come from T0 ledger or verified DB-to-ledger reconciliation.

### 14.5 Latest Fallback Is Audited

Given no explicit `session_id` and fallback selects latest session:

- The selected session id must be explicit before appending new T0 events.
- Fallback selection should be audited by caller metadata until a dedicated
  `session_selected_by_fallback` event is implemented.

### 14.6 Compaction Does Not Destroy Raw Events

Given a compacted session:

- Raw user/assistant/tool events remain readable.
- T0 source events are not deleted or rewritten.
- Replay can choose compacted context without deleting source events.

### 14.7 Tool Artifact Integrity

Given a tool result above inline threshold:

- Current T0 records the accepted tool result payload. Oversized runtime
  artifacts remain separately governed runtime/workspace artifacts unless a
  future T0 artifact adapter adds `artifact_ref` and hashes.

### 14.8 Open Segment Is Not Replay-Stable

Given an open segment:

- Replay recovery can read it only as live tail state.
- Durable resumed history uses the last flushed event.
- After a `segment_boundary` seals the current segment, that segment becomes immutable replay history.

### 14.9 Legacy Import Is Idempotent

Given an old `logs/YYYY-MM-DD/behavior/*.md` file:

- First import creates one imported segment.
- Second import creates no duplicate.
- `original_path` is preserved.

### 14.10 PL4 Never Enters Durable T0

Given raw input containing credential material:

- Privacy gate masks or blocks according to policy.
- Durable T0 does not contain the original credential.
- Event records sensitivity decision metadata.

## 15. What Is Not In T0 Scope

Do not solve these in T0 implementation:

- Higher-layer summary prompt structure
- Higher-layer tag taxonomy
- T3 convergence files
- Learning Brain tagging logic
- Memory Governance review agents
- Dream writing `soul.md`
- Skill/capability capsule generation
- Workflow design
- Vector/graph derived indexes

T0 only creates the trustworthy source ledger that those layers can consume.

## 16. Implementation Acceptance Criteria

T0 is considered complete when:

1. Web chat accepted user turns append to T0 before runtime execution.
2. Assistant final messages, tool calls/results, runtime boundaries append to T0.
3. Idle/close no longer depend on in-memory cursor for primary evidence.
4. Segments can be sealed and replayed.
5. Resume can reconstruct conversation from T0 ledger or verified reconciliation.
6. Legacy logs can be imported idempotently.
7. Redline tests in section 14 pass.
8. Existing `ChatMessage` and `RuntimeTask` paths remain compatible during cutover.
9. No T0 code path performs semantic summary, scoring, promotion, or tag judgment.

## 17. One-Line Target

T0 must become Hive's Claude Code transcript / Codex rollout equivalent:

> A durable append-only session ledger, rendered in Hive's MD/XML form, from
> which sessions can be resumed, evidence can be cited, and higher memory layers
> can safely distill.
