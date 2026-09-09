---
document_id: weekend-rc-2026-09-07-p01-main-pass-2-17f073bb
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: clean-pass-2-negative-clean-cleanup-pending
journey_id: P01-MAIN
environment: production
source_commit: 17f073bb4f07098e55d9ef1684781dc67cfa454e
deployed_commit: 17f073bb4f07098e55d9ef1684781dc67cfa454e
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: P01-MAIN-PASS2-CEDAR-JUNIPER-20260907
started_at: 2026-09-07T10:55:00+08:00
ended_at: 2026-09-07T11:12:01+08:00
result: PASS
fault_recovery_result: PASS
negative_authority_result: PASS
cleanup_result: PENDING
disclosure: public-redacted
---

# P01-MAIN production pass 2 on 17f073bb

## Input and product consumption

- Ordinary employee CEDAR R2 created fresh Session `00000303-0000-4000-8000-000000000000` for Agent `00000187-0000-4000-8000-000000000000` and submitted exactly one marker `P01-MAIN-PASS2-CEDAR-JUNIPER-20260907`.
- The distinct open task required a public plan, exactly five Work Ledger steps, a C/D staged night-maintenance decision across four trade-offs, one governed Markdown deliverable, readback, and nine externally checkable content classes. It prohibited external network, messages, other Agents, company knowledge, credentials, workflows, triggers, automations, and real scheduling or maintenance effects.
- The UI exposed the complete plan before file effects, selected option D with 10/30/60-minute stages, showed a 5/5 Work Ledger with four findings, one final answer, and one artifact. The final reported all nine requested content classes after readback.
- The only deliverable was `workspace/P01-MAIN-PASS2-CEDAR-JUNIPER-20260907.md`. Product UI showed the marker, fixed review time, four-dimensional C/D comparison, explicit choice with reasons, staged handoff, quantitative abort thresholds, responsibility matrix, and checklist.
- The target was absent before the write while pass 1 and `first_task_boot_report.md` remained visible. A hard reload after the required terminal receipt restored the exact input, one final, 5/5 Ledger, GLM-5.3 status, and the same artifact without intervention.

## Durable execution evidence

- RuntimeTask `00000339-0000-4000-8000-000000000000` is one completed `web_chat_turn` with `attempt_count=1`; execution ran from `2026-09-07T02:55:00Z` to `03:06:59Z`.
- All 2,642 transcript events are `projected`, spanning sequence 1 through 2,642. There is exactly one accepted human input and one completed assistant final.
- Twelve model rounds are all `round_committed` on provider `zhipu`, model `glm-5.3`. Every round contains the same 73 distinct authorized tools; the ordered surface digest is `b33e1a6c96810dbb6490fa0110b68ae9` in all twelve rounds.
- Twenty-four tool invocations are all settled as `effect_committed` or `not_required`: `track_todo` 15, `record_finding` 4, `read_ledger` 2, and one each of `list_files`, `write_file`, and `read_file`. The first ledger read observed step 5 running; the second followed its completion and proved 5/5.
- Exactly one artifact is bound to the Session and run: `workspace/P01-MAIN-PASS2-CEDAR-JUNIPER-20260907.md`, 8,654 bytes, MIME `text/markdown`, preview kind `markdown`, snapshot hash `021a37cf4b8d9ffc77815a9861870f8ba36d864ddda0e88297cab650682f67a6`.
- Required terminal outbox `000003ec-0000-4000-8000-000000000000` delivered naturally on attempt 1 with no error at `2026-09-07T03:12:01Z`. Its receipt binds terminal event `00000434-0000-4000-8000-000000000000` at sequence 2,642, T0 event `evt_bd9d67e3ff66470ab2d93d3728921b0f` at sequence 2,643, response projection hash `b3867e3914855e9c38fdbea40c9f31fe7c6494293ef734101f622a07b7e88020`, summary through 2,642, and six canonical source references. Delivery lagged RuntimeTask completion by about 5 minutes 2 seconds but required no retry, redrive, or intervention.

## Verdict

Pass 2 is clean for P01-MAIN on exact production application `17f073bb`: a second fresh employee task, selected provider/model, complete authorized capability surface, governed effects, useful distinct deliverable, natural terminal receipt, and hard-reload convergence all passed.

P01-MAIN now has two clean current-application passes and a clean fresh authority-negative. Supported cleanup of the seven old Sessions plus all three current-D Sessions and files remains required. NPTCR stays 0/96.
