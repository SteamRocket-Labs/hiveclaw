---
document_id: weekend-rc-2026-09-07-p01-main-cleanup-17f073bb
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: cleanup-verified-fault-recovery-pending
journey_id: P01-MAIN
environment: production
source_commit: 17f073bb4f07098e55d9ef1684781dc67cfa454e
---

# P01-MAIN cleanup — 2026-09-07

- Application baseline: `17f073bb4f07098e55d9ef1684781dc67cfa454e`.
- Authority: owner explicitly confirmed this synthetic batch and subsequently authorized deletion/modification of task-created synthetic data without repeated questions; formal data remains excluded.
- Scope: Agent `4e5261a6-c182-5248-9ca1-669f9419d44f`, tenant `0430e023-de03-4e8c-a3dc-b2a63e751427`; no Agent or fixture deletion.

## Exact Session targets

```text
3351fede-216a-44d5-9dfc-21cce7313356
a556482a-32e5-4c87-93d3-481ce239a07a
2043d7d5-21c5-4eba-a58c-4ee6645c1765
95702b70-f23c-4b59-8ea8-06c5d85ef2de
23dd3d47-0fe4-4372-839f-423b55c3aa64
ba59a6e3-30a1-48bd-8b64-3402b70d1a7a
5b162001-05da-423f-8b02-f4352791f79e
b9eb604a-2214-4308-85a6-37e2c9f2188f
a0a78e0e-7419-4a92-828b-2df499b9ee3f
dac0efb1-0ce5-43d6-874a-5d824078aa07
```

The first browser-extension confirmation attempt timed out and did not establish deletion. Native Chrome subsequently exposed the real KELP confirmation; after confirmation the UI selected JUNIPER and no longer listed KELP. The next JUNIPER confirmation was followed by IRIS selection and JUNIPER removal. Browser control then changed; the remaining Session deletions were not independently performed or witnessed by Codex. Do not attribute the whole batch to autonomous Codex UI execution. Independent frontend HTTP logs subsequently proved all ten exact product DELETE requests returned 204:

| Session prefix | UTC | Request ID |
|---|---|---|
| `dac0efb1` | 04:24:18 | `Upc5oBUWRvmnRQ8SVOLIQQ` |
| `a0a78e0e` | 04:25:05 | `rVL79CQFR--N-h4zVOLIQQ` |
| `b9eb604a` | 04:25:16 | `obfYs2KtQki09c9ToB_USg` |
| `5b162001` | 04:25:22 | `9R45EFifQY2z8Klk0ubPiw` |
| `ba59a6e3` | 04:25:25 | `5vZFFvoQSMOPA9BjVOLIQQ` |
| `95702b70` | 04:25:29 | `8Gew1XiESl6bBqFzAQeqjw` |
| `3351fede` | 04:25:32 | `olk35U4mQWSULsrq0ubPiw` |
| `a556482a` | 04:25:34 | `h1ed5uwpSSqUo3Qx0ubPiw` |
| `2043d7d5` | 04:25:36 | `rfrYzTyHQ76NYwWUJH0Vcg` |
| `23dd3d47` | 04:25:40 | `vnzKDEr-RGaC90ad0ubPiw` |

The same logs show three later duplicate DELETE requests for already-removed targets returning 404, not partial failure of the original 204 transactions. These retries were not initiated by Codex. No DELETE 500 occurred in this batch. Evidence command: `railway logs --service frontend --environment production --http --method DELETE --since 2026-09-07T04:18:00Z --lines 40 --json`, projecting only timestamp/path/status/request ID/duration.

A subsequent Railway Postgres **read-only transaction**, using `SET LOCAL ROLE app_rls` and the exact tenant, reconciled all ten IDs. No direct database deletion or RLS bypass was performed. Exact results:

| Relation / selector | Before | After |
|---|---:|---:|
| `chat_sessions.id` | 10 | 0 |
| `chat_artifacts.session_id` | 10 | 0 |
| `chat_transcript_events.session_id` | 16,109 | 0 |
| `chat_messages.conversation_id` | 230 | 0 |
| `runtime_tasks.root_session_id` | 10 | 10 |
| `runtime_terminal_boundary_outbox.session_id` | 10 | 10 |

Two preliminary read-only queries failed on UUID/text comparison and the incorrect message-column name; both aborted without mutation. The corrected query used `id::text`/`session_id::text`, `conversation_id` and `root_session_id`, and completed with `ROLLBACK`.

## Workspace cleanup

In a separate signed-in production tab, Codex opened the same Agent's **Documents & Workspace** page and deleted only these three files through their individual product confirmation dialogs:

- `workspace/P01-MAIN-NEGATIVE-CEDAR-KELP-20260907.md`
- `workspace/P01-MAIN-PASS1-CEDAR-IRIS-20260907.md`
- `workspace/P01-MAIN-PASS2-CEDAR-JUNIPER-20260907.md`

The three file DELETE requests returned HTTP 200 at 04:31:39, 04:31:58, and 04:32:24 UTC (request IDs `Z3XCZpeoRl6USiqSAXC71g`, `JXlqjvz3R-28tigAacI7Nw`, `rQxb8hfWQxKw9c7fV7rehQ`). Hard reload showed no conversations and only `first_task_boot_report.md` in the workspace. Execution-service read-only filesystem reconciliation returned:

```json
{"boot_report_workspace_exists": true, "p01_file_count": 0}
```

These product deletions have no normal undo. The existing evidence documents and runtime/outbox receipts are retained; they do not restore the removed Sessions or files. Cleanup does not by itself establish all 96 journeys, rollback acceptance, or a full RC release verdict.
