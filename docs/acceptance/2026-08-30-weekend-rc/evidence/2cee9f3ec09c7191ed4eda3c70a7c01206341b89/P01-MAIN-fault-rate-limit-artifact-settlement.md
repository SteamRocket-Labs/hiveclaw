---
document_id: weekend-rc-2026-08-30-p01-main-fault-rate-limit-artifact-settlement
owner: Codex
status: active
authority: immutable-production-failure-evidence-not-nptcr-pass
last_reviewed: 2026-08-30
verification_status: reproduced-tool-artifact-settlement-p1
journey_id: P01-MAIN
pass: fault-rate-limit-artifact-settlement
environment: production
source_commit: 2cee9f3ec09c7191ed4eda3c70a7c01206341b89
deployed_commit: 2cee9f3ec09c7191ed4eda3c70a7c01206341b89
manifest_sha256: d320edceeb26cf68fa724e77502d811e5476fa04ee3c9128075cc8c79eb38117
deployment_ids: backend=e853645d-9442-4322-938e-c76d0752343a; backend-api=f9aa545c-c1ce-40c4-af0d-19b68000d967; frontend=aed89005-ea52-4e47-8df9-0143c544269a
persona_principal: authenticated lab platform-admin using EventPilot in the selected experimental tenant
data_version: P01-MAIN-P1-CEDAR-734-retry-2
started_at: 2026-08-30T23:13:25+08:00
ended_at: 2026-08-30T23:18:03+08:00
result: FAIL
fault_recovery_result: FAIL
negative_authority_result: BLOCKED_PRECONDITION
cleanup_result: BLOCKED_PRECONDITION
supersedes: none
---

# P01-MAIN rate-limit and tool-artifact-settlement fault

This is immutable failure evidence. It is not a pass file, does not enter NPTCR, and does not prove P01-MAIN on application `2cee9f3e…`.

## Input

- Signed-in normal AgentDetail Session entry for EventPilot in the experimental tenant, using the supported retry/edit action from the prior P01-MAIN failure.
- Branch Session `b3962147-07cd-4223-8f23-f00193d7735c`; RuntimeTask `76a32f8e-f5d8-5a63-b02a-e591598321e9`.
- Canonical round one bound the exact accepted retry input `1fd5cc5b-8378-5629-8cdc-98fd8250f27f`; the prompt retained the original synthetic marker and required plan, Work Ledger, governed write/read, and exact deliverable fields.

## Authority

- The signed-in principal remained the platform administrator in the selected lab tenant. The write target was the Agent-owned `workspace/WEEKEND-RC-P01-MAIN-PASS-1.md` path in the same Session/run authority frame.
- No cross-tenant access, external message, credential or account change, billing action, DDL, workflow, delegation, or destructive production action was attempted.
- The retry-input fix was therefore proven live at the authority/input boundary; this failure is a later effect-evidence settlement defect.

## Execution

- GLM-5.3 consumed the complete bound prompt and created the expected three Work Ledger todos.
- The governed `write_file` effect changed `workspace/WEEKEND-RC-P01-MAIN-PASS-1.md`, but canonical tool settlement failed while creating the chat artifact reference.
- The kernel then entered another provider round despite the missing canonical terminal tool receipt. The provider later returned typed HTTP 429 / Zhipu code `1302`; the RuntimeTask ended `failed` with `failure_code=rate_limited`, `delivery_state=rejected`, and `requires_user_decision=true`.
- No blind retry followed the typed rate limit.

## Evidence

- Canonical tool lifecycle: sequence `304` `tool_call.started`; sequence `305` `tool_call.progress` with `effect_started`; no matching `tool_call.completed` or `tool_result.completed`; provider round six was nevertheless prepared at sequence `308`.
- Production log at `2026-08-30T15:15:26Z`: `on_tool_call(done)` persistence failed with PostgreSQL `ForeignKeyViolationError` on `chat_artifacts_message_id_fkey`; synthetic message `32e6d45a-6bfd-5f9c-920b-14f7db5c98eb` did not exist in `chat_messages`.
- The external effect is independently visible: file-change sequence `311`; before size `1914`, SHA-256 prefix `52313b…`; after size `1508`, SHA-256 prefix `ffdb3f…`; exactly one changed path.
- Terminal facts: `result_commit.failed` sequence `309` records `retry_safe=true`, `error_class=rate_limited`, `delivery_state=rejected`; `runtime_failure` sequence `310` records `failure_code=rate_limited` and `requires_user_decision=true`.
- Signed-in normal UI after hard reload showed `失败`, the rate-limit guidance and retry action, no active run, zero delivered artifacts, and Work Ledger `1 completed / 2 open`.
- Current-checkout path proof: `_persist_tool_call()` inserted `ChatArtifact` against `uuid5(invocation.id, "tool-result-artifact-message")` before any `ChatMessage` with that ID existed; PostgreSQL rolled back settlement. Kernel callback handling logged and swallowed the exception, appended the result only to in-memory provider history, and continued the model loop.
- Finding: `TOOL-ARTIFACT-SETTLEMENT-001`. This is distinct from the typed provider rate limit and from the already-fixed `SESSION-RETRY-INPUT-001` input binding.

## Recovery

- Reload converged truthfully to the failed run, retained file change, and incomplete Work Ledger; it did not invent a completed tool receipt or artifact.
- Recovery is `FAIL` because a visible write effect existed without its canonical result/artifact receipt, while the model loop continued. A user retry cannot be declared safe until that effect is held/reconciled and duplicate execution is prevented.
- The later 429 is a separate typed provider blocker. It neither caused nor excuses the earlier canonical settlement failure.

## Consumption

- A normal signed-in user could see the terminal provider failure and retained Work Ledger/file state, but could not consume the written deliverable through the expected chat artifact path.
- Operator evidence exposed the exact missing terminal receipt and FK failure. No database or console mutation was used to repair or reclassify the run.

## Acceptance

- `result=FAIL` and `fault_recovery_result=FAIL`: Input binding succeeded, but Evidence/Recovery/Consumption broke after the write effect.
- The target file and one completed todo are intermediate effects, not a Journey pass. No model-authored final, readback completion, second pass, authority-negative result, or cleanup exists.
- No P01-MAIN pass file was created. NPTCR remains `0/96`.

## Cleanup

- The synthetic Session, RuntimeTask, Work Ledger and workspace file remain retained as failure evidence until a fixed exact commit completes supported production revalidation and cleanup.
- No deletion was attempted in this fault run.

## Not proven

- The local no-DDL fix candidate, full regression gate, exact-commit deployment, production tool-settlement recovery, clean P01-MAIN pass 1/pass 2, negative authority, or cleanup.
- P02-STREAM and every other frozen production journey.
