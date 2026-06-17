# Memory Intelligence Restoration Plan (2026-06-17)

> Status: code-level remediation implemented; production live trace still pending.
>
> Scope: Hive agent memory, learning brain, heartbeat, dream, evolution ledger, and Memory Control Plane boundaries.
>
> This document does not replace `docs/agent-memory-md-first-spec.md` or `docs/agent-memory-purity-spec.md`. It narrows one implementation failure: some current paths preserve governance but suppress model intelligence, which violates the AI-native design law.

> Code closure evidence:
> `pytest backend/tests/services/test_heartbeat_reflection_learning.py backend/tests/services/test_heartbeat_reflection_backfill.py backend/tests/architecture/test_memory_intelligence_boundaries.py backend/tests/services/test_fast_reflection_learning_brain.py backend/tests/runtime/test_fast_reflection_hook.py backend/tests/services/test_fast_reflection_candidate.py backend/tests/services/test_extract_agent.py backend/tests/services/test_heartbeat.py backend/tests/services/test_auto_dream.py backend/tests/runtime/test_dream_template.py backend/tests/runtime/test_heartbeat_template.py backend/tests/memory/test_metrics.py backend/tests/tools/test_workspace.py -q` -> `321 passed, 4 warnings`.
> Full memory/self-evolution regression:
> `pytest backend/tests/memory backend/tests/services/test_extract_agent.py backend/tests/services/test_auto_dream.py backend/tests/services/test_skill_distiller.py backend/tests/services/test_evolution_ledger.py backend/tests/runtime/test_fast_reflection_hook.py backend/tests/runtime/test_self_evolution_closed_loop.py backend/tests/tools/test_memory_handler.py backend/tests/tools/test_workspace.py -q` -> `576 passed, 5 skipped, 4 warnings`.
> Full backend regression:
> `pytest backend/tests -q` -> `4712 passed, 7 skipped, 4 warnings`.

## 0. Executive Verdict

The memory architecture is not fundamentally wrong. The T0 -> T2 -> T3 -> soul pyramid, Learning Brain, heartbeat, dream, candidate ledger, and Memory Control Plane are the right shape.

The current failure is a boundary inversion:

```text
What should happen:
  LLM performs semantic judgment
  platform records evidence, gates writes, audits, and rolls back

What some current paths drifted toward:
  platform mechanically records summaries and counters
  those mechanical records are treated like evolution memory
```

That drift makes the system safe but weak. A safe memory system that stores low-value mechanical text is not acceptable for Hive. Governance exists to protect useful intelligence, not to replace it.

The core boundary is:

```text
LLM 负责判断、提炼、反思、归纳、候选生成；
平台负责证据引用、权限、去重、回滚、审计、最终落盘。
```

The target is:

```text
Model-first memory intelligence
  + governed candidate/write boundaries
  + evidence, eval, rollback, tenant isolation, and audit
  = useful self-evolution that remains enterprise-safe
```

## 1. Non-Negotiable Design Rules

1. Any semantic decision must be LLM-primary.

   Extraction, reflection, strategy recognition, failure-pattern recognition, skill candidacy, workflow candidacy, memory merge, contradiction resolution, and soul promotion are intelligence tasks. They cannot be reduced to regexes, counters, or 80-character summaries on the primary path.

2. Governance wraps authority, not cognition.

   The platform may decide whether something is allowed to write, promote, expose, or execute. It must not degrade what the model can inspect or how deeply it can reason unless doing so is a principal/privacy boundary.

3. Direct writes to managed memory paths remain forbidden.

   Main agents must not directly write `memory/learnings/`, `evolution/`, `logs/`, or `soul.md`. This is still correct. The fix is not to reopen direct filesystem writes.

4. Distillers produce candidates; platform gates durable writes.

   The Learning Brain, Extractor, Heartbeat Curator, Dream Consolidator, SkillDistiller, and Workflow candidate paths may propose. The Memory Control Plane decides admission, sensitivity, source refs, promotion, retirement, rollback, and audit.

5. Mechanical summaries are audit, not memory.

   `evolution/scorecard.md` and `evolution/lineage.md` may remain platform-managed audit ledgers. They must not be treated as the primary substrate for memory or self-evolution.

6. Evidence must be full-fidelity enough for the LLM to learn.

   Where a model-authored reflection, tool chain, or outcome is important, the downstream learning pass must see the full relevant text or a model-produced structured decision with source refs. A truncated mechanical summary is not enough.

7. No hidden MVP path.

   The remediation must close the whole loop: prompts, hooks, extraction, candidate routing, dream input, tests, backfill/audit strategy, and production observability.

## 2. Current State Facts

The following facts were verified against the current repository before writing this plan.

### 2.1 Correct Existing Contracts

- `docs/agent-memory-md-first-spec.md` already states the intended rule: distillers produce candidates; the Memory Control Plane decides writes, activation, promotion, retirement, and audit.
- `docs/hive-sota-master-goal.md` already states the AI-native rule: intelligent steps must be LLM-primary, while governance must not degrade model thinking.
- `backend/app/services/fast_reflection_learning_brain.py` already has the right shape: a post-turn learning brain reads full context and emits a governed classification without writing durable memory directly.
- `backend/app/services/session_learning.py` already has next-turn session projection, which is useful for fast learning without polluting durable memory.
- `backend/app/memory/t2_store.py` already routes T2 writes through a governed write gate and source-weighted admission.
- `backend/app/memory/t3_store.py` already routes durable memory writes through `append_t3_memory_candidate`.

### 2.2 Current Breakpoints

1. Heartbeat reflection is preserved but excluded from learning.

   `heartbeat.py` saves the full assistant reply to `ChatMessage` and emits `HEARTBEAT_TICK_END` with `reasoning`, but `t0_logger.py` stores heartbeat under `logs/.../system/`, which is explicitly audit-only. `extract_agent.extract()` also returns early for `source == "heartbeat"`.

   Result: high-value model reflection can exist in storage but never enter the main T2/T3/dream learning lane.

2. `evolution/lineage.md` is doing too much conceptually.

   `_update_evolution_files()` appends `source`, `outcome`, `score`, and a short summary. This is useful as audit and counters, but insufficient as memory. Treating it as the source of self-evolution collapses the model's reflection into mechanical bookkeeping.

3. Dream reads T3 and soul, not the full reflection/candidate substrate.

   Dream is LLM-first over T3, but if important heartbeat/self-reflection material never reaches T3 or candidate ledgers, dream cannot recover it.

4. Some prompts still imply `evolution/` is the evolution substrate.

   Recent fixes removed direct-write instructions, but the conceptual framing still needs cleanup: `evolution/` is an audit/evidence ledger, not the semantic memory body.

5. Fallbacks are allowed to exist, but some paths have become fallback-shaped by default.

   Counters, regexes, summaries, and static categories are useful for observability and failover. They are not acceptable as primary semantic adjudication.

## 3. Target Architecture

### 3.1 Container Roles

```text
T0 behavior logs
  Raw or near-raw behavior substrate. Full enough for replay and extraction.

T2 learnings
  LLM-extracted atom candidates. Self-contained, evidence-bearing, not yet durable truth.

T3 memory
  Durable semantic memory. Governed, deduped, source-referenced, lifecycle-managed.

soul.md
  Stable identity and behavior invariants. Promoted only through strict gates.

evolution/
  Audit and candidate ledger. It records what was proposed, evaluated, held, promoted, rejected, or rolled back.
  It is not where the main agent stores semantic memory by hand.
```

### 3.2 Actor Roles

```text
Main Agent
  Solves the task. May call save_memory only for explicit/high-confidence memory escape hatches.
  Cannot write managed memory/evolution/log/soul files directly.

Learning Brain
  Post-turn LLM judge. Decides what the turn taught the agent.
  Emits session projection, memory candidate, skill candidate, workflow candidate, or low_signal.

Extractor
  Converts raw/full-enough traces into T2 atom candidates.
  Must be LLM-primary; pattern extraction is only fallback.

Heartbeat Curator
  LLM agent that reviews T2/T3 context and promotes suitable T2 candidates into T3 through save_memory/governed APIs.
  Its own reflections are also learning evidence when non-noop.

Dream Consolidator
  LLM consolidator over T3 plus candidate evidence.
  Proposes merges, retirement, soul promotion, and stable behavior updates.

SkillDistiller / Workflow Candidate Lane
  LLM proposes reusable methods or deterministic workflows from repeated evidence.
  Promotion requires manifests, evals, and rollback refs.

Memory Control Plane
  Enforces principal, sensitivity, admission, lifecycle, source refs, audit, and rollback.
  It does not replace semantic judgment.
```

### 3.3 Correct Data Flow

```text
User / trigger / workflow / delegation turn
  -> ChatMessage + invocation spans + tool artifacts
  -> RESPONSE_COMPLETE
  -> Learning Brain full-context classification
  -> session_learning_projection and/or evolution candidate
  -> Extractor writes T2 atom candidates
  -> Heartbeat Curator reads T2 + T3 + relevant candidate digest
  -> governed T3 write through save_memory / append_t3_memory_candidate
  -> Dream reads T3 + candidate evidence + source refs
  -> promotion/hold/reject in evolution ledger
  -> soul / skill / workflow promotion only after gates
```

Heartbeat adds an internal branch:

```text
Heartbeat LLM reflection
  -> ChatMessage + HEARTBEAT_TICK_END
  -> if outcome is noop: audit only
  -> if outcome is curated/action_taken/failure:
       Learning Brain / Extractor sees the full reflection and tool chain
       may emit T2/candidate evidence
  -> evolution/scorecard and lineage record audit counters only
```

## 4. Remediation Scope

This must be delivered as one complete pass. Do not ship only one patch that makes a single activity log cleaner.

### R1. Reframe `evolution/`

- Update prompts, docs, and tool hints so `evolution/` is consistently described as audit/candidate ledger.
- Keep direct write refusal.
- Remove wording that suggests `lineage.md` is the semantic memory substrate.
- Keep `_update_evolution_files()` for metrics and audit, but do not rely on its summary as memory.

### R2. Restore Heartbeat Reflection Learning

- Introduce a distinct source such as `heartbeat_reflection`.
- Do not process idle/noop heartbeat ticks.
- Feed non-noop heartbeat final replies and relevant tool context into Learning Brain / Extractor.
- Preserve full model-authored reflection up to a reasonable LLM input budget. Any trimming must be observable and should preserve conclusion plus evidence.
- Ensure heartbeat reflection can produce:
  - T2 memory candidates
  - blocked-pattern candidates
  - strategy candidates
  - skill/workflow candidate hints
  - low_signal when it is routine bookkeeping

### R3. Make Dream Candidate-Aware

- Dream must read not only T3 files but also recent candidate evidence summaries:
  - fast reflection candidates
  - memory promotion candidates
  - heartbeat reflection candidates
  - held/rejected decisions with reasons
- Candidate evidence must include source refs back to ChatMessage, T0, invocation spans, or artifacts.
- Dream still cannot silently promote inferred or thin evidence to soul.

### R4. Strengthen Learning Brain as the Semantic Router

- Learning Brain should become the first semantic judge for post-turn learning, including heartbeat reflection.
- It should return container intent and boundary checks, not write durable stores.
- It must classify low-signal routine telemetry as low_signal.
- It must distinguish:
  - immediate session-only lesson
  - durable memory candidate
  - skill candidate
  - workflow candidate
  - soul candidate
  - audit-only/no learning

### R5. Keep Mechanical Paths as Observable Fallbacks

- Pattern extraction may run only when LLM extraction fails or is unavailable.
- Regex safety checks may block credentials, path escapes, and prompt-injection-shaped writes.
- Counters and scorecards may trigger review/alerts, not semantic promotion.
- Every fallback must stamp metadata such as `method=regex_fallback` or `method=mechanical_audit`.

### R6. Backfill Without Polluting Memory

- Provide a dry-run backfill tool for recent heartbeat reflections:
  - read recent heartbeat ChatMessages and system T0 logs
  - run Learning Brain / Extractor
  - show candidate count by category and confidence
  - require explicit apply confirmation
- The backfill must not import `lineage.md` summaries as durable memory.
- It must skip `HEARTBEAT_OK`, noop ticks, and operational counter-only entries.

### R7. Observability

- Add metrics for:
  - heartbeat_reflection processed
  - heartbeat_reflection skipped_low_signal
  - heartbeat_reflection extracted_to_t2
  - heartbeat_reflection candidate_created
  - llm_primary_success/fallback for each memory lane
- Activity logs should distinguish:
  - audit ledger write
  - LLM reflection extracted
  - candidate held/rejected/promoted

## 5. Explicit Non-Goals

- Do not let the main Agent directly edit `evolution/`, `memory/learnings/`, `logs/`, or `soul.md`.
- Do not create a fifth memory layer.
- Do not turn `lineage.md` into a raw transcript.
- Do not promote heartbeat reflections directly to T3/soul without the same gates as other evidence.
- Do not treat LLM self-score as sufficient verification.
- Do not use regex/counters as the primary semantic judge.
- Do not collapse Heartbeat and Dream only to reduce the number of moving parts.

## 6. Redline Test Checklist

These tests define the required behavior before implementation. Names are suggested; exact filenames may be adjusted to match existing test organization.

### 6.1 Architecture Guard Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/architecture/test_memory_intelligence_boundaries.py -q
```

Required red tests:

- `test_evolution_directory_is_audit_not_semantic_memory_source`
  - Assert prompt/tool docs do not describe `evolution/lineage.md` as the primary memory substrate.
  - Assert direct writes to `evolution/` remain refused.

- `test_semantic_memory_lanes_are_llm_primary`
  - Assert Extractor, Learning Brain, Heartbeat Curator, and Dream have LLM-primary paths.
  - Assert mechanical fallback paths are explicitly marked as fallback in metadata or code contract.

- `test_no_model_reflection_is_reduced_to_scorecard_only`
  - Assert non-noop model-authored reflection has a route into Learning Brain / candidate extraction, not only scorecard/lineage counters.

### 6.2 Heartbeat Reflection Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_heartbeat_reflection_learning.py -q
```

Required red tests:

- `test_non_noop_heartbeat_reflection_schedules_learning_brain`
  - Given a heartbeat reply with reusable reflection and `[OUTCOME:action_taken] [SCORE:7]`, assert a `heartbeat_reflection` learning pass is scheduled.

- `test_noop_heartbeat_does_not_pollute_learning`
  - Given `HEARTBEAT_OK [OUTCOME:noop] [SCORE:0]`, assert no T2/candidate extraction is scheduled.

- `test_heartbeat_failure_reflection_can_create_blocked_pattern_candidate`
  - Given a heartbeat failure explaining a repeated tool/workflow failure, assert the learning output can become a blocked-pattern or strategy candidate.

- `test_heartbeat_reflection_uses_full_reply_not_lineage_summary`
  - Given a long reflection where the useful lesson occurs after the first 80 chars, assert the learning pass receives the useful lesson.

- `test_heartbeat_reflection_preserves_source_refs`
  - Assert emitted candidates contain source refs to heartbeat session id, ChatMessage, invocation span, T0 log, or runtime task.

### 6.3 Extractor / Learning Brain Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_extract_agent.py tests/runtime/test_fast_reflection_hook.py tests/services/test_fast_reflection_candidate.py -q
```

Required red tests:

- `test_extract_agent_accepts_heartbeat_reflection_source`
  - `source="heartbeat_reflection"` must not hit the old `source == "heartbeat"` skip branch.

- `test_extract_agent_still_skips_raw_heartbeat_audit_source`
  - `source="heartbeat"` may remain audit-only if used for raw scorecard events.

- `test_learning_brain_classifies_routine_heartbeat_as_low_signal`
  - Routine maintenance text must not become durable memory.

- `test_learning_brain_routes_reusable_reflection_to_memory_candidate`
  - A reusable self-correction becomes `memory_candidate` or `session_learning` with confidence and refs.

- `test_learning_brain_routes_repeated_procedure_to_skill_candidate`
  - A repeated successful procedure becomes `skill_candidate`, not immediate skill write.

- `test_learning_brain_routes_stateful_repeat_to_workflow_candidate`
  - A repeated gated/stateful process becomes `workflow_candidate`, not immediate workflow registration.

### 6.4 T2 / T3 Admission Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/memory/test_t2_store.py tests/memory/test_t3_store.py tests/memory/test_t3_lane_gate.py -q
```

Required red tests:

- `test_heartbeat_reflection_t2_entries_use_autonomous_or_system_weight_not_human_weight`
  - Heartbeat reflections must not get direct-owner/human weight by default.

- `test_heartbeat_reflection_candidate_can_cross_threshold_when_repeated_or_high_confidence`
  - Strong/repeated model reflection may become T3 via governed curation.

- `test_mechanical_lineage_summary_cannot_be_promoted_as_durable_memory`
  - A bare `lineage.md` counter/summary line must be rejected or low-signal if treated as input.

- `test_t3_write_from_heartbeat_reflection_uses_write_gate`
  - Durable promotion must go through `append_t3_memory_candidate` / write gate, not direct file mutation.

### 6.5 Dream Candidate-Awareness Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_auto_dream.py tests/runtime/test_dream_template.py -q
```

Required red tests:

- `test_dream_prompt_includes_candidate_evidence_digest`
  - Dream consolidation input includes recent candidate evidence summaries in addition to T3 and soul.

- `test_dream_does_not_use_lineage_summary_as_primary_evidence`
  - Dream must not promote from `lineage.md` summary alone.

- `test_dream_can_promote_repeated_heartbeat_reflection_after_governed_evidence`
  - Repeated high-confidence heartbeat reflection can contribute to soul/T3 promotion only after candidate evidence and gates.

- `test_dream_holds_inferred_heartbeat_reflection_without_external_evidence`
  - Inferred self-reflection cannot directly promote to soul.

### 6.6 Skill / Workflow Candidate Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_skill_distiller.py tests/services/test_skill_flywheel.py tests/services/test_evolution_ledger.py -q
```

Required red tests:

- `test_heartbeat_reflection_skill_candidate_records_manifest`
  - Skill candidate from heartbeat reflection gets manifest, source refs, and validation plan.

- `test_skill_candidate_does_not_mutate_active_skill_without_gate`
  - Candidate creation cannot directly modify active `skills/`.

- `test_workflow_candidate_from_reflection_requires_stateful_process_evidence`
  - Workflow candidate requires repeated state/gate/replay evidence, not a one-off suggestion.

### 6.7 Backfill / Migration Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_heartbeat_reflection_backfill.py tests/memory/test_memory_hygiene.py -q
```

Required red tests:

- `test_backfill_dry_run_does_not_write_memory`
  - Dry-run reports candidate counts without writing T2/T3/evolution ledger entries.

- `test_backfill_skips_noop_and_heartbeat_ok`
  - Old routine heartbeat logs are not imported.

- `test_backfill_uses_llm_learning_brain_not_regex_primary`
  - LLM is primary; regex fallback is stamped and observable.

- `test_backfill_apply_requires_confirm`
  - Apply mode must require explicit confirmation.

### 6.8 Production Observability Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/memory/test_metrics.py tests/api/test_health_liveness.py tests/services/test_activity_logger.py -q
```

Required red tests:

- `test_memory_metrics_track_heartbeat_reflection_learning`
  - Metrics expose processed/skipped/extracted/candidate counts.

- `test_activity_log_distinguishes_audit_write_from_learning_candidate`
  - UI/logs must not make scorecard write look like semantic learning.

- `test_health_or_admin_surface_reports_learning_brain_fallback_rate`
  - Operators can see when memory lanes are falling back from LLM to mechanical paths.

## 7. Acceptance Bar

The remediation is complete only when all of these are true:

1. A non-noop heartbeat reflection can become a governed learning candidate.
2. A noop heartbeat cannot pollute T2/T3.
3. `evolution/lineage.md` is no longer treated as semantic memory.
4. Dream can see candidate evidence, not just T3 and mechanical lineage.
5. Direct managed-path writes remain blocked.
6. All semantic memory lanes are LLM-primary with observable fallback.
7. Candidate evidence carries source refs and tenant/principal context.
8. Durable promotion still requires Memory Control Plane gates, eval/verification when applicable, and rollback refs.
9. Existing T0/T2/T3/soul purity and lifecycle tests remain green.
10. A production run can prove the chain with trace ids or activity ids:

```text
heartbeat reply
  -> learning brain decision
  -> T2/candidate
  -> heartbeat/dream curation
  -> promotion/hold/reject ledger
```

## 8. Implementation Order for the Future Code Pass

Implementation status as of 2026-06-17: items 1-9 are code-level closed for the local repository. Item 10 remains production/eval live verification work.

This is an internal execution order, not a phased delivery boundary.

1. Add redline tests from Section 6.
2. Reframe prompts/docs/tool hints around `evolution/` as audit/candidate ledger.
3. Add `heartbeat_reflection` learning path.
4. Route non-noop heartbeat reflections through Learning Brain / Extractor.
5. Make Dream candidate-aware.
6. Add backfill dry-run/apply tooling.
7. Add observability and admin/health surfaces.
8. Run focused memory/self-evolution suites.
9. Run full backend tests.
10. Deploy production and eval backend together, then verify live traces.

## 9. Expected Test Commands

Focused design closure:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/architecture/test_memory_intelligence_boundaries.py \
  tests/services/test_heartbeat_reflection_learning.py \
  tests/services/test_extract_agent.py \
  tests/runtime/test_fast_reflection_hook.py \
  tests/services/test_fast_reflection_candidate.py \
  tests/memory/test_t2_store.py \
  tests/memory/test_t3_store.py \
  tests/services/test_auto_dream.py \
  tests/services/test_skill_distiller.py \
  tests/services/test_evolution_ledger.py -q
```

Full memory/self-evolution regression:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/memory \
  tests/services/test_extract_agent.py \
  tests/services/test_auto_dream.py \
  tests/services/test_skill_distiller.py \
  tests/services/test_evolution_ledger.py \
  tests/runtime/test_fast_reflection_hook.py \
  tests/runtime/test_self_evolution_closed_loop.py \
  tests/tools/test_memory_handler.py \
  tests/tools/test_workspace.py -q
```

Production verification after implementation:

```bash
railway logs --service backend --environment production --deployment
railway logs --service backend --environment eval --deployment
```

Production/eval checks must show:

- no direct `write_file/edit_file` attempts under `evolution/`
- non-noop heartbeat reflection learning events
- low fallback rate for memory semantic lanes
- candidate ledger entries with source refs
- no increase in low-signal T2/T3 churn

## 10. Decision

Proceed with this as a single remediation pass when implementation starts. Do not continue with isolated patches such as "add one Reflection field to lineage" or "let the model write a proxy markdown file." Both approaches preserve the wrong mental model.

The correct mental model is:

```text
Model intelligence is the primary memory engine.
Managed files are governed storage and audit surfaces.
The platform protects and verifies learning; it does not replace learning.
```
