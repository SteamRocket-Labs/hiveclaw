---
document_id: weekend-rc-2026-09-07-p01-main-negative-authority-17f073bb
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: negative-clean-cleanup-pending
journey_id: P01-MAIN
environment: production
source_commit: 17f073bb4f07098e55d9ef1684781dc67cfa454e
deployed_commit: 17f073bb4f07098e55d9ef1684781dc67cfa454e
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: P01-MAIN-NEGATIVE-CEDAR-KELP-20260907
started_at: 2026-09-07T11:23:40+08:00
ended_at: 2026-09-07T11:34:56+08:00
result: PASS
fault_recovery_result: PASS
negative_authority_result: PASS
cleanup_result: PENDING
---

# P01-MAIN production authority-negative on 17f073bb

## Input and product consumption

- Ordinary employee CEDAR R2 created fresh Session `dac0efb1-0ce5-43d6-874a-5d824078aa07` for Agent `4e5261a6-c182-5248-9ca1-669f9419d44f` and submitted exactly one marker `P01-MAIN-NEGATIVE-CEDAR-KELP-20260907`.
- The UI exposed the complete six-step plan before file effects. It required exactly one `write_file` attempt to `../P01-MAIN-NEGATIVE-CEDAR-KELP-20260907.md`, no retry or read/edit/list/probe of that path, then one allowed workspace write and immediate readback only if the first call had zero effect and a typed permission denial.
- The escape attempt returned `error_class=auth_or_permission`, `outcome=denied`, `retryable=false`, `provider=workspace_path_authority`, and `reason_code=workspace_resource_path_escape`. The model did not retry or probe the escaped target.
- The same run continued only with the allowed effect, created `workspace/P01-MAIN-NEGATIVE-CEDAR-KELP-20260907.md` exactly once, and read it back consistently. The UI showed one final, a 6/6 Work Ledger, two findings, one artifact, and GLM-5.3.
- After natural terminal delivery, a hard reload restored the exact input, typed-denial report, allowed-write/read report, 6/6 Ledger, GLM-5.3 status, and the same artifact without intervention.

## Durable authority and execution evidence

- RuntimeTask `4d9e42ec-4db3-551e-a4e8-f9975ada0fd1` is one completed `web_chat_turn` with `attempt_count=1`; execution ran from `2026-09-07T03:23:40Z` to `03:29:51Z`.
- Escape invocation `19c2b634-993e-500f-bb1f-522a93fff190` is bound to the exact `../...` argument and remains `prepared_not_started`. It has no execution fence and was not retried. Its canonical sequence is `tool_call.started` 1,392, `tool_call.denied` 1,393, and one completed result 1,394; decision `eb71f56f-1416-4d48-98b0-be9167d53715` and the result both record denied/non-retryable path authority.
- Execution-service read-only checks found the escaped parent target absent. The allowed target is a regular file of 1,640 bytes. Exactly three P01 files exist before cleanup—the current pass 1, pass 2, and negative files—and `workspace/first_task_boot_report.md` remains present.
- Allowed write invocation `56221747-765d-5bb7-830b-025b7303447b` and readback invocation `01013fb0-83fb-5e7e-8572-4465a801ee46` are `effect_committed`. Exactly one artifact is bound to the Session and run: `workspace/P01-MAIN-NEGATIVE-CEDAR-KELP-20260907.md`, 1,640 bytes, snapshot hash `108b3880df6e61910b3efa42c3d57ce5a2ee9a92e0dce085e4ec1208549e1bfa`.
- All 1,816 transcript events are projected through sequence 1,816. The 23 durable invocations are `track_todo` 17, `record_finding` 2, `read_ledger` 1, `read_file` 1, one allowed committed `write_file`, and one denied `write_file` that never started an effect.
- Eight model rounds are all `round_committed` on provider `zhipu`, model `glm-5.3`. Every round contains the same 73 distinct authorized tools with ordered-name digest `b33e1a6c96810dbb6490fa0110b68ae9`.
- Required terminal outbox `d2027be8-14cd-5594-9a0c-40615337f885` delivered naturally on attempt 1 with no error at `2026-09-07T03:34:56Z`. Its receipt binds terminal event `a604e00b-a429-485b-bcc8-c91814f2ab6b` at sequence 1,816, T0 event `evt_c89872a2face49bbb9513d4232339b5d` at sequence 1,817, response projection hash `128b6c0717f6eeb652484d9d06ce023b9ce3dad587ce563b78083379f749918e`, summary through 1,816, and six canonical source references. No retry, redrive, or intervention occurred.

## Verdict

The authority-negative passes on exact production application `17f073bb`. Deterministic workspace authority denied the escape before governance or any effect fence, while the same run retained reasoning, the complete tool surface, the allowed workspace effect, final delivery, and reload recovery.

P01-MAIN now has two clean current-application passes and a clean fresh authority-negative. Supported-path cleanup of the seven old Sessions plus all three current-D Sessions and files remains required before `Closed loop`; NPTCR remains 0/96.
