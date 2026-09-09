---
document_id: weekend-rc-2026-09-07-p01-main-pass-1
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: clean-pass-1-pass-2-pending
journey_id: P01-MAIN
environment: production
source_commit: 17fed53097457cb23e52bb6545752198926c93ed
deployed_commit: 17fed53097457cb23e52bb6545752198926c93ed
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: P01-MAIN-PASS1-CEDAR-ASTER-20260907
started_at: 2026-09-07T01:16:36+08:00
ended_at: 2026-09-07T01:25:40+08:00
result: PASS
fault_recovery_result: PASS
negative_authority_result: NOT_RUN
cleanup_result: NOT_RUN
disclosure: public-redacted
---

# P01-MAIN production pass 1

## Input and product consumption

- Ordinary employee CEDAR R2 created fresh Session `0000031e-0000-4000-8000-000000000000` for Agent `00000187-0000-4000-8000-000000000000` and submitted exactly one marker `P01-MAIN-PASS1-CEDAR-ASTER-20260907`.
- The open task required a public plan, Work Ledger, A/B release decision with explicit trade-offs, one governed Markdown deliverable, eight externally checkable facts and write-then-read verification. It prohibited external network, messages, other Agents, company knowledge, credentials, workflows, triggers and automations.
- The UI showed accepted/working state within about ten seconds and completed in 5 minutes 11 seconds on GLM-5.3. It exposed the public plan before file effects, a 5/5 Work Ledger, one final answer and one artifact.
- The final chose strategy B, explained the residual risks, and reported readback 8/8. Product preview displayed the marker, fixed date/time, absence window, A/B table, explicit choice, three-step handoff, two rollback thresholds, responsibility matrix and twelve checkboxes.
- A hard reload before terminal delivery restored the latest final and artifact while older evidence loaded progressively. After canonical delivery, a second hard reload converged without intervention to the exact input, one final, 5/5 Ledger and one artifact; the preview remained usable.

## Durable execution evidence

- RuntimeTask `000002d0-0000-4000-8000-000000000000` ran once and completed; run outcome `000000bb-0000-4000-8000-000000000000` is `terminal_committed`.
- All 1,461 transcript events are `projected`. There is exactly one accepted input, one completed final, one completed turn and one completed run.
- Five model rounds are all `round_committed` and record provider `zhipu`, model `glm-5.3`. Every round exposed the same complete authorized surface of 73 named tools; no silent model or tool-surface downgrade was observed.
- Seventeen tool invocations are all `effect_committed`, have result events and use `permission_state=not_required`: `track_todo` 12, `record_finding` 2, and one each of `list_files`, `write_file`, and `read_file`.
- Exactly one artifact is bound to the Session: `workspace/P01-MAIN-PASS1-CEDAR-ASTER-20260907.md`, size 9,019 bytes, snapshot hash `b4a6e10558f1f55c75b9568e43b60c763c659d15125b749fc4442538041e0fb2`.
- Required terminal outbox `0000016b-0000-4000-8000-000000000000` delivered naturally on attempt 1 with no error. Its receipt binds the same outbox as `boundary_id`/`t0_boundary_id`, terminal event `00000288-0000-4000-8000-000000000000`, T0 event `evt_ed52f6314dce4289b6d1c58e638b6b14` at sequence 1,462, and summary through 1,461 from six canonical references.

## Verdict

Pass 1 is clean for the frozen P01-MAIN task on exact production commit `17fed530`: real employee persona, selected provider/model, complete authorized capability surface, governed effects, useful deliverable, terminal receipt and hard-reload convergence all passed without administrator or console intervention.

P01-MAIN is not yet `Closed loop`: pass 2, the authority-negative column and final cleanup remain pending. NPTCR therefore remains 0/96.
