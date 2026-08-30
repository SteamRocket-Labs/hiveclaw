---
document_id: weekend-rc-2026-08-30-p01-main-fault-retry-input-loss
owner: Codex
status: active
authority: immutable-production-failure-evidence-not-nptcr-pass
last_reviewed: 2026-08-30
verification_status: reproduced-session-retry-input-p1
journey_id: P01-MAIN
pass: fault-provider-overload-retry-input-loss
environment: production
source_commit: d0c9fffd1ca4995ddea6d367e04e206e973560d5
deployed_commit: d0c9fffd1ca4995ddea6d367e04e206e973560d5
manifest_sha256: d320edceeb26cf68fa724e77502d811e5476fa04ee3c9128075cc8c79eb38117
deployment_ids: backend=ce0bdbf4-c8b6-4cd3-bbe2-77e74a75ca2e; backend-api=ef4f7c81-b8cb-44d8-bbd7-37499e1765fb; frontend=f6932ba1-9f7e-4b61-8b38-54ae709ba278
persona_principal: authenticated lab platform-admin using EventPilot in the selected experimental tenant
data_version: P01-MAIN-P1-CEDAR-734
started_at: 2026-08-30T22:30:14+08:00
ended_at: 2026-08-30T22:43:08+08:00
result: FAIL
fault_recovery_result: FAIL
negative_authority_result: BLOCKED_PRECONDITION
cleanup_result: BLOCKED_PRECONDITION
supersedes: none
---

# P01-MAIN provider-overload and retry-input-loss fault

This is immutable failure evidence. It is not a pass file, does not enter NPTCR, and does not prove P01-MAIN on application `d0c9fffd…`.

## Input

- Signed-in AgentDetail Session entry for EventPilot in the experimental tenant.
- Synthetic task marker: `P01-MAIN-P1-CEDAR-734`.
- The prompt required a public three-step plan, at least three Work Ledger todos, governed `write_file` plus `read_file`, and exact acceptance fields in `workspace/WEEKEND-RC-P01-MAIN-PASS-1.md`.
- Source Session: `d1a2c63f-7082-424d-a9f3-a3330398e371`; source RuntimeTask: `ff9536bd-39fa-5bf3-bd02-f07aa6fb0e81`.

## Authority

- UI reported the signed-in principal as platform administrator in the selected lab tenant; the Session permission mode was `default` / request approval.
- Session-local writes under `workspace/` were allowed by the product policy. No external message, cross-tenant read, credential change, billing action, DDL, trigger, workflow, delegation, or destructive action was attempted.
- Authentication secrets and tokens are intentionally absent from this evidence.

## Execution

- GLM-5.3 authored the public plan, created three todos, recovered from a read of the initially absent target, wrote the file, and read it back through governed tools.
- Before the model-authored final, the provider returned overload/busy. The product terminalized the source RuntimeTask as `failed` with `result_summary=provider_error` and exposed a typed retry action.
- One click on `重试本轮` created edit branch Session `ef9d6498-f4dc-49c1-a566-6446e220f0ef` and RuntimeTask `03419d5f-6166-479d-ad02-d929759c57df`.
- The retry RuntimeTask made one real `zhipu/glm-5.3` provider call, invoked no tools, and returned an unrelated final claiming the user message contained only `1`. The product then terminalized that run as `completed`.

## Evidence

- The source Workspace preview contained the exact title, marker, three-row agenda, `TOTAL_MINUTES=90`, two-row risk table, `RISK_ROWS=2`, and owner/timing/fallback/final-handoff checklist. Work Ledger remained two completed plus one in progress because the source run failed before finalization.
- Retry lineage: `branch_mode=edit`, parent/root Session `d1a2c63f-7082-424d-a9f3-a3330398e371`, anchor event `b0004973-3aa5-4833-9a5c-c7cd92fe719e`, and `copied_event_ids=[]`.
- Retry canonical transcript sequence `1` is `human_input.accepted` with the complete original prompt and marker. The signed-in operator workbench reports a truthful typed-empty semantic history for this fresh edit branch.
- Retry `result_commit.prepared` sequence `4` records `bound_input_ids=[]`; Work Ledger is empty, tool calls are empty, artifact refs are empty, and the assistant snapshot/final contains the unrelated `1` response.
- Current-checkout path proof found the earliest wiring error: normal Session create/start routes use `submit_live_human_input()` and Session V2 admission/dispatch, while the branch route directly called legacy `start_web_chat_run()`. The kernel obtains ordinary current user messages from `round_input_bind()`, not from a bare RuntimeTask prompt.
- Finding: `SESSION-RETRY-INPUT-001`. `SESSION-CONTEXT-001` remains separately `Verified`; this fault is current-run input loss on a retry branch, not prior-turn semantic-history loss.

## Recovery

- The source provider failure was represented truthfully and offered the supported retry action.
- The retry did not duplicate the existing file write because it invoked no tools, but it lost the current input, produced the wrong final, and reported false success. Therefore fault recovery is `FAIL`.
- No blind second retry was attempted after the earliest erroneous state became proven.

## Consumption

- A normal signed-in user could inspect the source failure, retry button, source Workspace file, Work Ledger state, and retry final.
- The user-facing retry branch appeared completed even though its answer did not address the retained task. Operator evidence exposed the empty input binding; no console or DB mutation was used to change state.

## Acceptance

- `result=FAIL` and `fault_recovery_result=FAIL` because retry must preserve the exact accepted user task and cannot mark an unrelated answer as successful recovery.
- Attempt 1 remains failed even though its intermediate file content met external criteria; a terminal model-authored final and completed todos were absent.
- No P01-MAIN pass file was created. NPTCR remains `0/96`.

## Cleanup

- The synthetic source/branch Sessions and workspace file remain retained as immutable failure evidence until the fixed exact commit completes production revalidation and supported cleanup.
- No deletion was attempted in this fault run.

## Not proven

- The candidate local fix, exact-commit deployment, fresh retry recovery, clean P01-MAIN pass 1/pass 2, permission-negative behavior, hard reload convergence, or final cleanup.
- P02-STREAM and every other frozen production journey.
