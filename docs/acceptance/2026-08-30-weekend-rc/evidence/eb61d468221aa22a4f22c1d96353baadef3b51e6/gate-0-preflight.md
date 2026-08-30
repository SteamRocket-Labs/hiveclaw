---
document_id: weekend-rc-2026-08-30-gate0-eb61d468
owner: Codex
status: active
authority: immutable-production-observation-not-nptcr-pass
last_reviewed: 2026-08-30
verification_status: failed-session-continuity-probe
journey_id: GATE-0
pass: preflight
environment: production
source_commit: 56ec5dd0631ea3b27b796d086560b81f902e322b
deployed_commit: eb61d468221aa22a4f22c1d96353baadef3b51e6
manifest_sha256: d320edceeb26cf68fa724e77502d811e5476fa04ee3c9128075cc8c79eb38117
deployment_ids: backend=7cf21899-44e2-411c-bb15-e5b746e9b7e2; backend-api=e7b62bc9-4737-4ee6-8a0a-abc062345eb7; frontend=7c133bf2-a005-4371-8f44-c468a24ec221
persona_principal: authenticated lab super-admin in the selected experimental tenant
data_version: weekend-rc-gate0-synthetic-fixtures-v1
started_at: 2026-08-30T19:12:45+08:00
ended_at: 2026-08-30T19:17:49+08:00
result: FAIL
fault_recovery_result: BLOCKED_PRECONDITION
negative_authority_result: BLOCKED_PRECONDITION
cleanup_result: BLOCKED_PRECONDITION
supersedes: none
---

# Gate 0 production preflight and first runner probe

This file records Gate 0 facts and a failed runner probe. It is not a `P01-MAIN` or `P02-STREAM` pass, does not enter NPTCR, and does not prove production acceptance for application `eb61d468…`.

## Input

- Signed-in AgentDetail Session for `EventPilot` in the experimental tenant.
- First marker: `WEEKEND-RC-P01-MAIN-20260830-1912`; bounded reasoning task with no external tool effect.
- Second marker: `WEEKEND-RC-P02-EXISTING-20260830-1914`; same Session, asks the Agent to audit its immediately preceding answer.
- Session ID: `59257e7a-960b-459a-9652-2ff39be117ee`.

## Authority

- UI reported the signed-in principal as `超级管理员` in the selected experimental tenant.
- No credential, billing, DDL, cross-tenant, destructive, external-message, or irreversible action was attempted.
- The authentication secret and token are intentionally absent from this evidence.

## Execution

- AgentDetail showed primary `zhipu/glm-5.3`, fallback `minimax/MiniMax-M3`, and selectable `deepseek/deepseek-v4-flash`.
- GLM completed run `2fa2f887-b76e-556c-99c8-3a814c37f27b`, then completed same-Session run `58b222f2-b52b-5cb1-b5a1-f657ced4222a`.
- The second `POST .../sessions/{session_id}/runs` returned HTTP `201` in about `1.71s`; this is the server admission latency, not total run time.

## Evidence

- Railway fresh readback: backend, backend-api, and frontend were `SUCCESS` and their deployment messages identified the same application commit `eb61d468221aa22a4f22c1d96353baadef3b51e6`.
- Public backend health returned success and frontend root returned HTTP `200`; these prove reachability only.
- Canonical `GET .../transcript?limit=1000&schema_version=2` returned 635 ordered events (`1..635`) with two accepted human inputs, two assistant text snapshots, and two completed runs. The first prompt and full first answer were present in canonical payloads.
- Compatibility `GET .../messages` returned ten rows, all role `system`; user and assistant rows were absent.
- The UI presented both turns in one Session, but the second model-authored answer stated it had no previous answer and that this was the first message in the Session. Finding: `SESSION-CONTEXT-001`.

## Recovery

- No recovery action was attempted because the earliest wrong state was newly reproduced and must first receive a code-level fix.
- Hive Connect daemon was running, but product status returned `401 Invalid bridge token` and the UI showed zero linked Local Agents. Re-login/token replacement is outside this preflight authority.

## Consumption

- First response was readable, terminal, and semantically satisfied the bounded No-Go task.
- Same-Session UI continuity was visually convincing but semantically false for the second model call. This is a P1 trust failure, not a cosmetic issue.

## Acceptance

- Result: `FAIL` because Session continuity is a foundational Single Agent capability and failed on the real provider path.
- NPTCR remains `0/96`; no pass files were created.
- The run also observed a roughly 1.71-second existing-Session admission gap before the frontend could display server-confirmed queued state; this remains an observation, not yet a separate finding.

## Cleanup

- The uniquely marked synthetic Session remains retained while the P1 is diagnosed and later re-tested.
- No deletion was attempted because supported product cleanup is itself part of final acceptance and destructive removal requires a separate action-time decision.

## Not proven

- MiniMax and DeepSeek live provider success, Local Agent lifecycle, permission-negative paths, hard reload convergence, fault injection, tool use, artifact delivery, and any of the remaining 96 journeys.
- The exact final remediation; only the live breakpoint and current wiring mismatch are proven here.
