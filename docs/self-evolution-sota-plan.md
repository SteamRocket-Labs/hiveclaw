# Hive 自我进化 Agent 基石计划（Canonical）

> Canonical status: this file supersedes the previous two research notes and the old v4 optimization plan.
>
> Scope: Module 1 only, the single-agent self-evolution kernel. Enterprise control plane and agent-to-agent work
> are deferred until this foundation is measurable and better than `/Users/rocky243/vc-saas/hermes-agent` on core
> self-improvement experience.
>
> North Star: Hive is an enterprise-grade self-evolving agent infrastructure and control plane. Every code path
> must serve agent intelligence/self-evolution or enterprise governance.

---

## Phase Status

| Phase | Status | Evidence |
|---|---|---|
| P0: Canonical Baseline, Trace, Eval, Manifest | Completed 2026-05-23 | `backend/app/services/evolution_manifest.py`; `record_evolution_candidate()` now attaches `hive_evolution_manifest.v1`; `validate_evolution_ledger()` rejects candidates without a valid manifest; tests: `pytest tests/services/test_evolution_manifest.py tests/architecture/test_h5_evolution_ledger_contract.py tests/evals/test_bakeoff_runtime.py -q` -> 18 passed; regression: `pytest tests/services/test_evolution_ledger.py tests/services/test_evolution_validation.py tests/services/test_harness_canary.py tests/services/test_harness_validation_report.py -q` -> 15 passed; ruff passed for changed P0 files. |
| P1: Per-turn Fast Reflection Candidate | Completed 2026-05-23 | `backend/app/services/fast_reflection_service.py`; `RESPONSE_COMPLETE` now registers `memory.response_complete.fast_reflection`; strong user correction/test failure/repeated workflow signals create ledger-only `fast_reflection_candidate.v1` candidates with manifests; low-signal chatter is skipped; no T2/T3/soul/skill writes. Tests: `pytest tests/services/test_fast_reflection_candidate.py tests/runtime/test_fast_reflection_hook.py tests/test_memory_integration.py::TestHooksIntegration -q` -> 12 passed; regression: `pytest tests/runtime/test_hooks.py tests/services/test_evolution_ledger.py tests/services/test_evolution_validation.py -q` -> 44 passed; ruff passed for changed P1 files. |
| P2: Session-visible Learning Projection | Completed 2026-05-23 | `backend/app/services/session_learning.py`; fast reflection candidates now write `session_learning_projection.v1`; active projections are injected through dynamic memory context as `session_learning_projection` context blocks and can also render through `build_dynamic_prompt_suffix(session_learning_projection=...)`; expired/rejected projections disappear; frozen prefix still excludes session learning. Tests: `pytest tests/services/test_session_learning.py tests/runtime/test_session_learning_projection.py tests/runtime/test_prompt_cache.py -q` -> 10 passed; regression: `pytest tests/services/test_fast_reflection_candidate.py tests/runtime/test_fast_reflection_hook.py tests/runtime/test_prompt_builder.py tests/runtime/test_prompt_sections.py -q` -> 81 passed; ruff passed for changed P2 files. |
| P3: Verification-gated Promotion | Completed 2026-05-23 | `backend/app/services/evolution_verification.py`; supports deterministic command, state check, tool-call check, LLM rubric shape, and human confirmation; verification reports become eval runs through `record_verification_eval()`; `decide_verified_promotion()` holds without evidence, rejects failed verification, and promotes passing verification; evolution validation now accepts rejected decisions only with reason. Tests: `pytest tests/services/test_evolution_verification.py tests/services/test_evolution_validation.py -q` -> 9 passed; regression: `pytest tests/services/test_evolution_verification.py tests/services/test_evolution_ledger.py tests/services/test_evolution_validation.py tests/services/test_fast_reflection_candidate.py tests/services/test_session_learning.py tests/architecture/test_h5_evolution_ledger_contract.py -q` -> 21 passed; ruff passed for changed P3 files. |
| P4: Skill Flywheel v2 | Not started | Pending |
| P5: Prompt Cache and Hot-path Diet | Not started | Pending |
| P6: Harness Contract and Artifact Refs | Not started | Pending |
| P7: Hive vs Hermes Bakeoff | Not started | Pending |

---

## 0. Executive Verdict

Hive already has a serious self-evolution substrate:

- unified runtime through `invoke_agent()` and `AgentKernel`
- frozen prompt prefix + dynamic suffix
- T0/T2/T3/soul memory pyramid
- durable extraction queue + startup replay
- activation scoring with sensitivity stripping
- loop guard
- reflection artifacts
- memory promotion ledger
- heartbeat/dream consolidation
- `ToolRuntimeService`, action preflight, capability policy, checkpoint, decision trace

But Hive is not yet a SOTA self-improving agent in the way Hermes feels like one. The gap is not another memory layer. The gap is the missing fast, verified, session-visible improvement loop.

**Correct target state:**

```text
Hermes-like next-turn learning speed
+ Hive-grade evidence, eval, tenant boundary, rollback, and governance
= enterprise SOTA self-evolving agent infrastructure
```

The most important improvement to the previous plan is this: **eval/trace/manifest must move earlier**. Fast reflection without hard evidence becomes memory/skill pollution. Verification-gated promotion is not a later nice-to-have; it is part of the first viable loop.

---

## 1. Source Synthesis

### 1.1 What the two research notes contributed

The research notes converged on the same engineering conclusion:

- Self-improvement is a loop, not a feature.
- The practical leverage is harness-level adaptation before weight-level training.
- The harness includes tools, filesystem, sandbox, traces, tests, memory, skills, docs, permissions, CI, evals, rollback, and human review.
- A self-improving system must separate generator/evaluator/debugger/evolver/gatekeeper roles, even if they are implemented as services rather than always as separate agents.
- Every harness change needs a manifest: failure evidence, root cause, targeted fix, predicted impact, regression risks, eval suite, rollback plan.
- Do not chase weight-level training until traces, evals, sandbox, reward, rollback, and data governance are stable.

High-value ideas absorbed into this canonical plan:

- L1/L2/L3 framing:
  - L1 inference-time correction: self-refine, Reflexion, verifier-guided retries.
  - L3 harness-level evolution: skills, memory, tools, middleware, prompts, routing, trace policy.
  - L2 weight-level improvement: optional future path only after stable reward data.
- Filesystem-based skills should include `SKILL.md`, `references/`, `templates/`, `scripts/`, and tests.
- Tools are agent UX, not ordinary APIs. Tool boundaries and return shapes matter as much as model choice.
- Production self-improvement requires regression suites and canary/rollback, because agents are better at predicting fixes than regressions.

### 1.2 What the old optimization plan contributed

The old v4 plan was valuable, but it is now a completed substrate plan rather than the current roadmap. It established these durable invariants:

- evidence-tagged memory writes
- minimum sufficient source refs
- memory promotion candidate before durable write
- rollback refs for T3/soul promotion
- loop guard as memory hygiene, not only token saving
- reflection artifact separated from distilled memory projection
- P0 direct memory plus P1/P2 index-first shadow path

These are retained as invariants in this file. The old plan should not remain as a parallel roadmap because it creates duplicate phase numbering and makes future agents misread completed substrate work as current target work.

### 1.3 What the current SOTA references imply

Primary engineering references:

- Anthropic, [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents):
  agent quality comes from tool/environment feedback loops, not one large prompt.
- Anthropic, [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents):
  long tasks need initializer artifacts, feature lists, progress logs, git history, incremental sessions, and
  end-to-end testing.
- Anthropic, [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps):
  harness design can improve agent performance well above a baseline before hitting model-level limits.
- Anthropic, [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents):
  tool design must be evaluated like UX; tool responses should be compact and purpose-built.
- Anthropic, [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):
  agent evals need deterministic graders when possible, LLM graders when necessary, and transcript/tool/state checks.
- OpenAI, [Harness engineering](https://openai.com/index/harness-engineering/):
  human engineers steer; agents execute; repo knowledge, docs, CI, tests, observability, and local review loops become
  the product surface.
- OpenAI, [Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/):
  the harness is the reusable loop behind different agent surfaces.

Representative research references:

- [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)
- [Voyager: An Open-Ended Embodied Agent with Large Language Models](https://arxiv.org/abs/2305.16291)
- [Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442)
- [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560)

The shared lesson across these papers: memory and skills only work when tied to trajectory feedback, retrieval
discipline, and verifiable reuse.

Practical implication for Hive:

```text
No eval -> no trustworthy self-improvement.
No trace -> no learnable experience.
No manifest -> no reviewable change.
No rollback -> no enterprise autonomy.
No session-visible projection -> no user-perceived learning.
```

---

## 2. Current Hive Facts

These are current code facts verified against the repository when this document was written.

### 2.1 Runtime and prompt

- `backend/app/runtime/invoker.py:160-203` defaults `max_tool_rounds` to 200.
- `backend/app/kernel/engine.py:1141` defines `AgentKernel`; `engine.py:1169` is `handle()`.
- `backend/app/kernel/engine.py:551-600` implements frozen prompt prefix cache key, fingerprint, store, and reuse.
- `backend/app/runtime/invoker.py:275-338` keeps memory in the dynamic suffix, outside the frozen prefix.
- `backend/app/kernel/engine.py:21,1330,1982,2055` wires `LoopGuard` into text/tool/result observation.
- `backend/app/kernel/engine.py:1024-1077` evicts large tool results to files; `engine.py:2292-2352` microcompacts older tool results.

### 2.2 Memory and evolution substrate

- `backend/app/runtime/hooks_setup.py:75-96` triggers `RESPONSE_COMPLETE` extraction.
- `backend/app/services/extract_agent.py:688-776` writes extraction payloads to durable queue before scheduling.
- `backend/app/services/extract_queue.py` and `backend/app/services/extract_queue_replay.py` implement failed/in-flight extraction replay.
- `backend/app/main.py:319-340` replays pending extraction work on startup.
- `backend/app/services/evolution_ledger.py:49-235` records candidates, eval runs, promotion decisions, memory promotion decisions, and rollback refs.
- `backend/app/services/reflection_service.py:23-61` writes reportable reflection artifacts and distilled T2 projections.
- `backend/app/memory/activation.py:43-74` scores memory with sensitivity, goal, owner, company, open-loop, retention, and confidence signals.
- `backend/app/memory/retriever.py:191-241` combines working, T3, understandings, episodic, semantic backend, and external memory.
- `backend/app/memory/retriever.py:359-477` keeps P0 memory direct and has an index-first shadow path for P1/P2.
- `backend/app/services/memory_service.py:102-106,622-625` makes LLM rerank tenant-configured, not unconditional.

### 2.3 Heartbeat, dream, and skill loop

- `backend/app/config.py:118` default heartbeat interval is 45 minutes.
- `backend/app/services/auto_dream.py:65-66,737,1187-1197` gates dream by 4 hours plus 3 sessions or 2 heartbeat ticks.
- `backend/app/services/heartbeat.py:1470-1500` runs heartbeat through `invoke_agent()`.
- `backend/app/services/auto_dream.py:380-483` still uses direct LLM calls for dream consolidation, with audit but not full runtime governance.
- `backend/app/services/skill_distiller.py:529-572` also uses direct LLM calls before later ledger recording.
- `backend/app/kernel/contracts.py:76` defaults `skill_candidate_loop_enabled=False`; `backend/app/runtime/invoker.py:193` enables it via feature flag.

### 2.4 Governance and tool boundary

- `backend/app/tools/service.py:82-110` centralizes tool execution in `ToolRuntimeService`.
- `backend/app/tools/service.py:138-140` runs action preflight before tool execution.
- `backend/app/tools/service.py:388-435` can create checkpoint, decision trace, audit log, and block execution.
- `backend/app/services/capability_gate.py:299-390` supports agent-specific policy, tenant default policy, and approval escalation.

### 2.5 Existing test base

High-signal tests already exist:

- kernel: `backend/tests/kernel/test_loop_guard.py`, `test_prompt_cache_integration.py`, `test_microcompact_gap.py`
- memory: `backend/tests/memory/test_activation_scoring.py`, `test_retrieval_pipeline.py`, `test_retriever_index_shadow.py`, `test_write_gate.py`
- evolution: `backend/tests/services/test_evolution_ledger.py`, `test_reflection_service.py`, `test_auto_dream.py`
- extraction durability: `backend/tests/services/test_extract_queue.py`, `test_extract_queue_replay.py`, `test_extract_agent.py`
- architecture guard: `backend/tests/architecture/test_tool_runtime_single_entry.py`, `test_h5_evolution_ledger_contract.py`
- eval scaffolding: `backend/app/evals/run.py`, `backend/app/evals/bakeoff_runtime.py`, `backend/app/runtime/task_eval.py`

---

## 3. Hermes Gap

Hermes is weaker as an enterprise platform, but stronger in perceived single-agent learning speed.

Verified local signals:

- `hermes-agent/agent/background_review.py` forks a post-turn review agent that inherits runtime and cached prompt, then uses a memory/skill tool whitelist.
- `hermes-agent/agent/background_review.py` treats user corrections, style complaints, and workflow corrections as first-class skill signals.
- `hermes-agent/agent/conversation_loop.py:4152-4162` runs background memory/skill review after the response is delivered.
- `hermes-agent/agent/system_prompt.py:266-298` keeps system prompt stable across a session and uses date-level time.
- `hermes-agent/agent/memory_manager.py:339-375` supports prefetch, queued prefetch, and sync of completed turns.

Hermes advantage:

- next-turn learning feel
- aggressive skill update posture
- lower single-user governance friction
- prompt cache discipline

Hermes weakness:

- direct write posture
- no Hive-grade tenant/company/owner boundary
- no equivalent enterprise promotion ledger and policy replay
- weaker rollback and governance story

Hive should not copy Hermes directly. Hive should copy the fast loop, then constrain it with candidate, manifest, eval, policy, and rollback.

---

## 4. Canonical Architecture Target

### 4.1 The self-evolution loop

```text
Task / conversation turn
  -> structured trace + artifacts
  -> fast reflection candidate
  -> session-visible projection
  -> verifier / eval / policy gate
  -> durable promotion or hold/reject
  -> heartbeat/dream consolidation
  -> regression replay and rollback readiness
```

### 4.2 Role separation

The implementation can use services rather than separate physical agents, but responsibilities must remain separate:

| Role | Responsibility | Hive home |
|---|---|---|
| Actor | solve the user's task | `AgentKernel`, `invoke_agent()` |
| Verifier | run deterministic/LLM/state checks | new `evolution_verification.py` |
| Debugger | explain failures from trace/artifacts | fast reflection service |
| Evolver | propose memory/skill/harness candidates | fast reflection + skill distiller |
| Gatekeeper | promote/hold/reject with rollback refs | `evolution_ledger.py` + policy replay |

### 4.3 Change manifest

Every self-improvement candidate that changes durable behavior must have this minimum shape:

```json
{
  "schema": "hive_evolution_manifest.v1",
  "candidate_id": "...",
  "target_type": "memory:t3|memory:soul|skill|tool_description|retrieval_policy|prompt|harness",
  "changed_components": ["..."],
  "failure_evidence": [
    {
      "trace_ref": "...",
      "artifact_ref": "...",
      "symptom": "..."
    }
  ],
  "root_cause": "...",
  "targeted_fix": "...",
  "expected_improvements": ["..."],
  "regression_risks": ["..."],
  "eval_suite": ["..."],
  "promotion_decision": "candidate|hold|promote|reject",
  "rollback_ref": "..."
}
```

This manifest is the bridge between Hermes-like speed and enterprise-grade safety.

### 4.4 Brain / hands / session split

Hive should converge on this harness contract:

- `AgentBrain`: model, prompt/runtime config, memory projection, active skills.
- `AgentHands`: tool runtime, filesystem, sandbox, MCP, browser, external connectors.
- `AgentSessionLog`: append-only event stream with tool calls, results, traces, artifacts, test output, decisions.
- `WorkspaceManifest`: files, mounts, output dirs, allowed execution user, network/secrets policy.
- `ExecutionArtifactRef`: large tool results, screenshots, test reports, PDFs, logs, trace spans by reference, not full prompt copy.

The model should see compact pointers and request details on demand.

---

## 5. Core Invariants

### 5.1 Memory invariants

- T0/T2/T3/soul remain the memory pyramid. Do not add a fifth memory layer.
- New durable memory must have evidence classification and source refs.
- `inferred` memories can be candidates or T2 projections; they cannot directly promote to T3/soul.
- P0 memory such as user correction and blocked patterns remains direct.
- P1/P2 memory can move toward index-first retrieval only after shadow evidence shows acceptable miss rate.
- Dream proposes candidates; it does not get unchecked write authority over soul.

### 5.2 Skill invariants

- Skill updates are candidates first.
- Prefer updating loaded/current umbrella skills before creating new skills.
- Support files go under `references/`, `templates/`, or `scripts/`.
- Skills must have a lint/eval surface before durable promotion.
- Protected/platform skills require higher review.
- Skills cannot carry unreviewed binaries, secrets, or tenant-specific private data.

### 5.3 Harness invariants

- No self-edit can promote without manifest, eval result, and rollback ref.
- Low-risk harness surfaces come first: tool descriptions, skill docs, retrieval rules, middleware policy.
- High-risk surfaces come later: tool implementation, sub-agent routing, system prompt, agent source, model weights.
- Weight-level adaptation is out of Module 1.

### 5.4 Governance invariants

- Do not bypass `ToolRuntimeService`.
- Do not bypass action preflight for external-visible, sensitive, irreversible, or company-boundary actions.
- Tenant, owner, company, sensitivity, capability policy, and checkpoint boundaries are part of the product, not optional friction.
- Hot-path governance can be optimized, but not removed.

---

## 6. Revised Phase Plan

This is the improved plan after consolidating the four documents. The main change from the previous draft is that eval/trace/manifest is pulled into P0 instead of living behind later phases.

### P0: Canonical Baseline, Trace, Eval, Manifest

Goal: self-improvement cannot start until the system can observe, score, and roll back behavior.

Work:

- Keep this file as the single canonical Module 1 roadmap.
- Define `hive_evolution_manifest.v1`.
- Define minimum trace/artifact refs needed by evolution candidates.
- Add or extend eval fixtures for next-turn adaptation, promotion safety, tool-use correctness, and tenant leakage.
- Add architecture test that rejects direct durable self-improvement writes without manifest/ledger.

Red tests first:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_evolution_manifest.py \
  tests/architecture/test_h5_evolution_ledger_contract.py \
  tests/evals/test_bakeoff_runtime.py -q
```

Acceptance:

- A candidate without manifest fields is invalid.
- A durable promotion without eval evidence is held or rejected.
- A test fixture exists for next-turn adaptation.
- This doc is the only self-evolution research/plan doc in `docs/`.

### P1: Per-turn Fast Reflection Candidate

Goal: close the Hermes gap while preserving Hive safety.

Work:

- Add a non-blocking `RESPONSE_COMPLETE` fast reflection hook.
- Input: current messages, tool digest, final response, error/test artifacts, user correction signals, session id, tenant/agent/user ids.
- Output: evolution candidate and optional session projection.
- Candidate writes only to ledger/manifest, never directly to T3/soul/skill.
- Reflection must distinguish:
  - user preference correction
  - workflow correction
  - tool failure lesson
  - verification failure
  - repeated task pattern
  - low-signal chatter

Red tests first:

```bash
pytest tests/runtime/test_fast_reflection_hook.py \
  tests/services/test_fast_reflection_candidate.py -q
```

Acceptance:

- Hook scheduling does not block response completion.
- User correction generates a candidate with source refs.
- Ordinary chatter does not create churn.
- Candidate cannot modify durable memory/skill directly.

### P2: Session-visible Learning Projection

Goal: make learning visible on the next turn without polluting long-term memory.

Work:

- Add `session_learning_projection.v1`.
- Render projection in dynamic suffix only.
- Attach TTL, candidate id, evidence type, promotion state, and source refs.
- Update projection when candidate is promoted/rejected/expired.

Schema:

```json
{
  "schema": "session_learning_projection.v1",
  "session_id": "...",
  "candidate_id": "...",
  "lesson": "...",
  "source_refs": ["..."],
  "evidence": "user_stated|tool_verified|system_observed|inferred",
  "expires_at": "...",
  "promotion_state": "candidate|held|promoted|rejected"
}
```

Red tests first:

```bash
pytest tests/services/test_session_learning.py \
  tests/runtime/test_session_learning_projection.py \
  tests/runtime/test_prompt_cache.py -q
```

Acceptance:

- Projection appears in the next turn of the same session.
- Projection never enters frozen prefix.
- Expired/rejected projections disappear.

### P3: Verification-gated Promotion

Goal: every durable behavior change needs evidence beyond LLM confidence.

Work:

- Add `backend/app/services/evolution_verification.py`.
- Support grader types:
  - deterministic command: pytest, ruff, npm test/build
  - state check: file/db/runtime task state
  - tool-call check: required/forbidden tool sequence
  - LLM rubric: open-ended reports and communication tasks
  - human confirmation: explicit user approval
- Wire verification result into `record_eval_run()`.
- Promotion gate:
  - `tool_verified` and `user_stated` can enter short-lived projection.
  - `inferred` cannot durable-promote alone.
  - skill/soul/harness candidates need eval evidence or human confirmation.

Red tests first:

```bash
pytest tests/services/test_evolution_verification.py \
  tests/services/test_evolution_ledger.py -q
```

Acceptance:

- No verification -> hold.
- Failed verification -> reject or hold with next action.
- Passing deterministic verification + source refs -> promote candidate.

### P4: Skill Flywheel v2

Goal: match Hermes/Voyager learning speed while avoiding skill poisoning.

Work:

- Feed fast reflection skill signals into skill candidate flow.
- Prefer current loaded skill, then umbrella skill, then support files, then new class-level skill.
- Add skill candidate manifest and verification.
- Add no-secret/no-tenant-leak/no-binary checks.
- Add progressive disclosure metadata for promoted skills.

Red tests first:

```bash
pytest tests/services/test_skill_distiller.py \
  tests/services/test_skill_lifecycle.py \
  tests/templates/test_skill_eval_contracts.py -q
```

Acceptance:

- Workflow correction creates a skill candidate.
- Candidate does not directly mutate active skill.
- Promoted skill is discoverable through metadata and has validation evidence.

### P5: Prompt Cache and Hot-path Diet

Goal: keep enterprise governance but remove avoidable latency and cache churn.

Work:

- Assert frozen prefix hash stability across ordinary same-session turns.
- Keep runtime metadata, current user, time, focus, memory, and session learning projection in dynamic suffix.
- Cache or bound optional LLM rerank with timeout/fallback.
- Move low-risk audit enrichment, access count, policy replay sampling, and reflection artifact generation off hot path.
- Add gate latency telemetry.

Red tests first:

```bash
pytest tests/kernel/test_prompt_cache_integration.py \
  tests/runtime/test_prompt_cache.py \
  tests/memory/test_retriever_rerank_prompt.py \
  tests/tools/test_tool_runtime_preflight.py -q
```

Acceptance:

- Destructive/external-visible tools remain synchronously protected.
- Ordinary chat/task turn is not blocked by low-risk governance work.
- Prefix hash remains stable.
- Rerank failure does not fail the main invocation.

### P6: Harness Contract and Artifact Refs

Goal: make Hive a real long-running harness, not only a prompt/runtime loop.

Work:

- Add `WorkspaceManifest` and `ExecutionArtifactRef` model/service surface.
- Attach manifest/artifact refs to `RuntimeTask.metadata_json` and session events.
- Ensure big outputs, screenshots, logs, reports, and test artifacts are addressable by ref.
- Add resume/interrogation path from session log and artifact refs.
- Keep generated code away from tenant/provider credentials by default.

Red tests first:

```bash
pytest tests/architecture/test_harness_contract.py \
  tests/architecture/test_h4_long_task_runtime_contract.py \
  tests/kernel/test_engine.py -q
```

Acceptance:

- Long task can resume from session log + artifact refs.
- Tool result eviction path is readable by agent on demand.
- Sandbox/workspace manifest captures execution boundaries.

### P7: Hive vs Hermes Bakeoff

Goal: prove the foundation is better, not just more complex.

Work:

- Build a fixed bakeoff dataset for:
  - user correction next-turn adaptation
  - repeated workflow learning
  - tool failure lesson reuse
  - skill candidate creation
  - long task resume
  - safety/tenant/policy boundaries
- Run Hive and Hermes on the same task prompts where feasible.
- Score with deterministic checks first, LLM rubric second, human review for disputed cases.

Red tests first:

```bash
pytest tests/evals/test_self_evolution_bakeoff.py \
  tests/runtime/test_task_eval.py -q
```

Acceptance:

- Hive matches or beats Hermes on next-turn adaptation.
- Hive beats Hermes on auditability, rollback, and enterprise safety.
- Cost/latency regression is visible and bounded.

---

## 7. PR Order

| PR | Content | Depends on | Estimate |
|---|---|---:|---:|
| PR-0 | Canonical doc cleanup + manifest/eval/trace contract tests | none | 1d |
| PR-1 | Fast reflection candidate hook | PR-0 | 2d |
| PR-2 | Session-visible learning projection | PR-1 | 1.5d |
| PR-3 | Verification-gated promotion | PR-0, PR-1 | 2d |
| PR-4 | Skill flywheel v2 | PR-1, PR-3 | 2d |
| PR-5 | Prompt cache + hot-path governance diet | PR-0 | 1.5d |
| PR-6 | Harness contract + artifact refs | PR-5 | 3d |
| PR-7 | Hive vs Hermes bakeoff suite | PR-2, PR-3, PR-4 | 2d |

Core proof path: PR-0 -> PR-1 -> PR-2 -> PR-3 -> PR-7.

---

## 8. What Not To Do

- Do not add a fifth memory layer.
- Do not preserve multiple roadmap docs for the same module.
- Do not copy Hermes direct-write behavior.
- Do not remove enterprise governance to gain speed.
- Do not promote inferred memory to T3/soul.
- Do not allow dream to directly rewrite identity.
- Do not use LLM self-judgment as the only verifier.
- Do not begin weight-level training in Module 1.
- Do not start Module 2 control-plane redesign or Module 3 A2A orchestration before Module 1 has bakeoff evidence.

---

## 9. Deferred Modules

### Module 2: Enterprise Control Plane

Start after PR-0 through PR-3 are working.

Questions to evaluate:

- Can admins configure charter/capability/preflight/memory policy from UI?
- Are decision traces, checkpoints, approvals, and policy replay visible?
- Can non-engineer admins understand owner/company/tenant boundaries?
- Can policy changes be evaluated before production rollout?

### Module 3: Agent-to-agent

Start after fast learning and verification gates are stable.

Questions to evaluate:

- Do Lease/Signal/Checkpoint/Sentinel cover real collaboration?
- Is delegation evaluated rather than just called?
- Can subagents return artifact refs instead of huge summaries?
- Is cross-agent memory sharing governed by tenant/company/owner policy?

---

## 10. Current Decision

The single highest-leverage next implementation is:

```text
P0 manifest/eval/trace contract
-> P1 fast reflection candidate
-> P2 session-visible projection
-> P3 verification-gated promotion
```

This sequence improves the earlier draft. The previous version correctly identified the Hermes gap, but it put verification too late. The merged plan now makes eval, trace, and manifest the first-class substrate of fast learning, which is the only way to get Hermes-like speed without losing Hive's enterprise safety.
