---
document_id: weekend-rc-2026-09-07-p01-main-fault-recovery-17f073bb
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: reproduced-session-worker-restart-round-001
journey_id: P01-MAIN
environment: production
source_commit: 17f073bb4f07098e55d9ef1684781dc67cfa454e
disclosure: public-redacted
---

# P01-MAIN disconnect and worker restart recovery

This record addresses the frozen requirement: disconnect and worker restart must converge without duplicate input, effect or final answer. The existing clean-path hard reloads and completed cleanup do not prove that requirement.

## Exact scope

- Tenant: `00000018-0000-4000-8000-000000000000`.
- Agent: `00000187-0000-4000-8000-000000000000`, CEDAR R2 employee's synthetic Worker R1.
- Session: `000002f8-0000-4000-8000-000000000000`.
- RuntimeTask: `00000301-0000-4000-8000-000000000000`.
- Marker: `P01-MAIN-RECOVERY-CEDAR-LINDEN-20260907`.
- Only file effect allowed: create `workspace/P01-MAIN-RECOVERY-CEDAR-LINDEN-20260907.md` once; preserve every existing file.
- Actual provider/model: `zhipu/glm-5.3`; one accepted input. Only the six stated plan/ledger/workspace tools are allowed by the task. No external messages, internet, credentials, company data, other Agents, workflows, triggers or automation.

## Preflight evidence

Production health reported exact source hash `8858ddcabb44fd055a0bb37f391b0a8e3127fc48228ae7a4780ccbfc430d51c9`, 1,058 files, runtime role and worker `4cdfb749e669:22`. Railway's current backend deployment is `000004be-0000-4000-8000-000000000000`, SUCCESS. The worker executes within the runtime backend process; a service restart is not a tenant-local operation.

Before starting the synthetic run, read-only counts showed no running RuntimeTask. One unrelated July 29 `goal_continuation` remained pending with no lease. A subsequent count-only join proved its budget status is `summary_only`, not `active`; the current claim predicate therefore excludes it. Its data is not modified. Recheck unrelated active work immediately before a restart.

The browser submitted exactly one input. Canonical transcript contains one `human_input.accepted`; RuntimeTask started with attempt 1 and the above worker. Legacy `chat_messages` does not materialize this canonical user input, so its zero user count is not the input-count oracle.

## Injected fault and pre-restart receipt

The live page publicly displayed its complete six-step plan, then wrote the file once and read it back. Immediately before restart, the RuntimeTask was still `running / attempt 1` under `4cdfb749e669:22`, with no unrelated running RuntimeTask.

- Write invocation: `000001a9-0000-4000-8000-000000000000`, `effect_committed`.
- Fence: `session-tool-effect:000001a9-0000-4000-8000-000000000000:generation:2`.
- Receipt: `tool-frame:8647b8105cb01719db4ae8085f519ac56c9588dd82ee495a2fee7b525d59d2e1`.
- Artifact: `000000c4-0000-4000-8000-000000000000`, 10,096 bytes, snapshot hash `8bb7fa046808ba8c53c904578fb6fdf979deb12681ec6aa6a84f2d274c0690bd`.

Codex closed its dedicated Chrome tab `907506435`, disconnecting that Session page. Then `railway restart --service backend --environment production --project 0000041c-0000-4000-8000-000000000000 --yes --json` returned the same deployment ID `000004be-0000-4000-8000-000000000000`, without rebuilding or changing source/configuration.

The first health probe returned 502 during restart. By 04:49:15 UTC the official startup logs had completed existing schema/readiness startup and reached uvicorn. The run still held its old lease through `2026-09-07T04:50:51.536144Z`; no manual lease/status mutation or input resubmission was used. Observed committed tools at that point: list_files 2, read_file 1, read_ledger 1, record_finding 2, track_todo 11, write_file 1; seven model rounds committed and one streaming.

## Observed recovery failure

Backend returned healthy on the same exact source and strict RLS. Daemon startup timestamps changed to approximately `2026-09-07T04:49:14Z`; counters reset. The same deployment restart reused hostname/PID label `4cdfb749e669:22`, so a changed label is not a valid mandatory oracle of process restart.

The dedicated browser reconnected to the same Session in tab `907506457` without sending another input. It restored the original input, plan, file and Ledger, then showed `恢复运行中`. Native lease reclaim produced attempt 2, `reclaimed_expired_claim=true`, `lease_reclaim_count=1`, `resumed_after_restart=true`, and `resumed_at=2026-09-07T04:50:58.688931Z`. The task nevertheless became `failed` at `2026-09-07T04:51:16.498403Z`, with `terminal_reason=provider_error`; UI said the model service failed and the run was not retryable. Previously emitted process text was repeated on the page. No retry was submitted.

The durable model rows changed from seven `round_committed` plus one `streaming` to:

- rounds 2–7: six unchanged `round_committed` rows;
- round 8: still `streaming`, old attempt owner;
- round 1: now `needs_reconciliation`, owner `session_model_round:ambiguous_prepare`.

The exact accepted input count remained 1 and the unique write invocation remained 1. Tool counts were unchanged after failure. Thus zero repeated file effects is established, but useful recovery is not.

Live source explains the observed collision: `_load_run_context` in `web_chat_runtime.py` restores `session_resume_round_index` only through permission-resume history; the ordinary reclaimed-worker path does not populate it. `web_chat_run_orchestrator.py` defaults `initial_round_index` to zero; the next prepare targets round 1 again. `prepare_model_request` then changes the existing committed result to `needs_reconciliation` and raises `model_round_provider_send_is_ambiguous`.

The failed run's terminal outbox naturally delivered on attempt 1 at `2026-09-07T04:53:27.014973Z`. Canonical counts: one accepted/queued/bound/applied input, one `runtime_failure.recorded`, seven original assistant-text completed/snapshot pairs, and 18 tool-result completions. Do not equate the repeated rendered text with newly committed duplicate model outputs before tracing the projection path.

### Repeated-text attribution

At approximately 05:04 UTC, a tenant-pinned `app_rls` read-only query separated public `assistant_text` snapshots and deltas; no private-reasoning payload was read. The page's last partial text belongs to item `0000039b-0000-4000-8000-000000000000`, round 8: exactly two durable deltas created at `04:48:14.440951Z` and `04:48:15.635139Z`, before the restart. Their ordered contents reproduce the displayed partial text, including the overlap with round 7. Round 7's snapshot at sequence 1962 and round 6's at 1902 remain separate authoritative items. The frontend projects these items by identity; snapshots replace an item's content and deltas append within that item.

This rules out a reconnect-created duplicate for the observed fragment. Do not add semantic text deduplication or label it a new frontend defect: the overlapping text was already emitted and persisted before the injected fault. The unresolved defect remains useful runtime recovery and its misleading provider-error attribution.

Finding: `SESSION-WORKER-RESTART-ROUND-001`. Keep this fixture and its receipts intact while correcting the shared runtime path; no direct production state repair, request replay or deletion has been performed after this failure. After a reviewed fix and new exact deployment, repeat the frozen current-D proof and cleanup. This attempt is not PASS.
