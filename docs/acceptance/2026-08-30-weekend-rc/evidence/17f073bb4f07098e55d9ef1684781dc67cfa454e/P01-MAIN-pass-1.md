---
document_id: weekend-rc-2026-09-07-p01-main-pass-1-17f073bb
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: clean-pass-1-negative-clean-cleanup-pending
journey_id: P01-MAIN
environment: production
source_commit: 17f073bb4f07098e55d9ef1684781dc67cfa454e
deployed_commit: 17f073bb4f07098e55d9ef1684781dc67cfa454e
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: P01-MAIN-PASS1-CEDAR-IRIS-20260907
started_at: 2026-09-07T10:28:14+08:00
ended_at: 2026-09-07T10:43:52+08:00
result: PASS
fault_recovery_result: PASS
negative_authority_result: PASS
cleanup_result: PENDING
---

# P01-MAIN production pass 1 on 17f073bb

## Input and product consumption

- Ordinary employee CEDAR R2 created fresh Session `b9eb604a-2214-4308-85a6-37e2c9f2188f` for Agent `4e5261a6-c182-5248-9ca1-669f9419d44f` and submitted exactly one marker `P01-MAIN-PASS1-CEDAR-IRIS-20260907`.
- The open task required a public plan, exactly six Work Ledger steps, an A/B maintenance-command decision across four trade-offs, one governed Markdown deliverable, readback, and nine externally checkable content classes. It prohibited external network, messages, other Agents, company knowledge, credentials, workflows, triggers, automations, and real business effects.
- The UI exposed the complete plan before any file effect, selected option A, showed a 6/6 Work Ledger with four findings, one final answer, and one artifact. The final reported 9/9 requested content classes after readback.
- The only deliverable was `workspace/P01-MAIN-PASS1-CEDAR-IRIS-20260907.md`. Product UI showed the marker, fixed review time, four-dimensional A/B table, explicit choice with three reasons, six-step handoff, two quantitative abort thresholds, RACI, and ten-item checklist.
- A hard reload after the required terminal receipt restored the exact input, one final, 6/6 Ledger, GLM-5.3 status, and the same artifact without intervention.

## Durable execution evidence

- RuntimeTask `9c66eb89-5703-5952-92c6-af8645f4223b` is one completed `web_chat_turn` with `attempt_count=1`; execution ran from `2026-09-07T02:28:16Z` to `02:35:58Z`.
- All 2,484 transcript events are `projected`, spanning sequence 1 through 2,484. There is exactly one accepted human input and one completed assistant final.
- Seven model rounds are all `round_committed` on provider `zhipu`, model `glm-5.3`. Every round contains the same 73 distinct authorized tools; the ordered surface digest is `b33e1a6c96810dbb6490fa0110b68ae9` in all seven rounds.
- Twenty-five tool invocations are all `effect_committed` with `permission_state=not_required`: `track_todo` 17, `record_finding` 4, and one each of `list_files`, `write_file`, `read_file`, and `read_ledger`.
- Exactly one artifact is bound to the Session and run: `workspace/P01-MAIN-PASS1-CEDAR-IRIS-20260907.md`, 8,366 bytes, MIME `text/markdown`, preview kind `markdown`, snapshot hash `520f3bd425be33e18e025bfd7dc93710d2594a0674006ed75fe3d0f081795cb6`.
- Required terminal outbox `9ad41655-b2f3-58c5-ba99-4f1ddf6ecb44` delivered naturally on attempt 1 with no error at `2026-09-07T02:43:52Z`. Its receipt binds terminal event `77ec73e6-8922-4e34-9ee6-f8075a3f2725` at sequence 2,484, T0 event `evt_967eaa449dc44838adf5d8b79a9ab72c` at sequence 2,485, response projection hash `ccbe0ab9e4b8f1a459d2470f3f038bc2cab4bf658917c28f96ccfcd9003840a1`, summary through 2,484, and six canonical source references. Delivery lagged RuntimeTask completion by about 7 minutes 53 seconds but required no retry, redrive, or intervention.

## Verdict

Pass 1 is clean for P01-MAIN on exact production application `17f073bb`: real employee persona, selected provider/model, complete authorized capability surface, governed effects, useful deliverable, natural terminal receipt, and hard-reload convergence all passed.

This pass now has a distinct clean pass 2 and fresh authority-negative on the same application. Supported cleanup of the seven old Sessions plus all three current-D P01 Sessions and files remains required. NPTCR stays 0/96.
