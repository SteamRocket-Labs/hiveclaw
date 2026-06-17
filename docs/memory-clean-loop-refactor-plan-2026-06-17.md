# Memory Clean Loop Refactor Plan (2026-06-17)

> Status: design plan for the next code pass.
>
> Goal: make Hive memory responsibilities explicit, remove duplicate semantic lanes, and preserve model intelligence while keeping governed writes, audit, rollback, and enterprise access control.
>
> North-star rule:
>
> ```text
> LLM 负责判断、提炼、反思、归纳、候选生成；
> 平台负责证据引用、权限、去重、回滚、审计、最终落盘。
> ```

## 0. Verdict

The memory architecture is the right shape, but the current implementation is too layered and has drifted into repeated authority.

The main problem is not the four-layer pyramid. The problem is the set of side loops wrapped around it:

- `RESPONSE_COMPLETE` currently forks both Learning Brain and Extractor over the same context.
- Fast Reflection is both an adapter and a fallback classifier.
- Heartbeat curates memory, writes audit, routes its own reflection back into learning, kicks skill distillation, scene/wiki curation, and dream scheduling.
- Dream does LLM consolidation, mechanical repeated-feedback promotion, T3 cleanup, T2 truncation, T0 audit/backfill, soul writes, and preservation flags.
- Memory Control Plane exists conceptually, but code ownership is split across `write_gate.py`, `t2_store.py`, `t3_store.py`, `activation.py`, `visibility.py`, tool handlers, workspace guards, dream writeback, and heartbeat writeback.

This creates three operational risks:

1. **Semantic duplication** — multiple components decide what the same event means.
2. **Truth-source ambiguity** — operators cannot tell whether T2, fast-reflection candidates, `lineage.md`, ActivityLog, or T0 is the source of truth.
3. **Authority bleed** — curator/consolidator services sometimes also behave like orchestrators or control-plane writers.

## 1. Target Shape

Hive memory should become a clean loop with seven named planes.

```text
Capture Plane
  -> Semantic Decision Plane
  -> Atomization Plane
  -> Curation Plane
  -> Consolidation Plane
  -> Memory Control Plane
  -> Read / Activation / Prompt Plane
```

Only two planes use model intelligence as semantic owners:

- **Learning Brain** owns post-turn learning judgment.
- **Dream Reconsolidator** owns slow global consolidation and identity proposals.

All other services either capture evidence, transform already-decided signals, apply governed writes, or render read context.

## 2. Responsibility Matrix

| Component | Owns | May Read | May Write | Must Not Do |
|---|---|---|---|---|
| Runtime hooks | Event routing and nonblocking fanout | Messages, metadata | Hook events only | Semantic classification, durable memory writes |
| T0 logger | Raw/replayable behavior evidence and system audit | Hook payloads | `logs/.../behavior/*.md`, `logs/.../system/*.md` | Feed `system/` logs into semantic memory by default |
| Learning Brain | Post-turn semantic judgment | Full message context, tool results, metadata, session projection | `MemorySignalEnvelope` only | Write T2/T3/soul/skills/workflows |
| Fast Reflection service | Adapter from Learning Brain decision to short-lived candidate/projection | Learning Brain output | `evolution_ledger.jsonl`, `session_learning_projections.jsonl` | Independently classify semantics except observable fallback |
| Extractor | Atomization into T2 evidence lines | `MemorySignalEnvelope` or raw replay packet | `memory/learnings/*.md` through T2 gate | Decide final container, promote to T3/soul/skill/workflow |
| Heartbeat Curator | T2 -> T3 curation | T2, T3 summary, source refs | `save_memory` / governed T3 append; heartbeat audit | Run unrelated evolution jobs inline; directly write memory/evolution files |
| Dream Reconsolidator | T3 merge, contradiction resolution, soul proposals | T3, soul, candidate ledger, preservation flags | governed lifecycle patches, soul promotion decisions | Treat `lineage.md` as semantic substrate; directly promote inferred weak evidence |
| SkillDistiller | Skill candidate validation/promotion lane | skill evidence candidates, eval reports | skill candidate/eval/promotion ledger; active skill only after gates | Compete with memory lanes for the same signal |
| Memory Control Plane | Write/read authority, permissions, dedup, lifecycle, rollback, audit | candidate requests + source refs | T2/T3/soul/lifecycle/archive/evolution decision records | Perform semantic learning itself |
| Retriever/Activation | Principal-aware memory recall | T3, high-priority T2, session projection, derived indexes | access telemetry only | Mutate semantic memory |
| Prompt assembly | Render current memory into model context | identity, memory snapshot, navigation, session learning | final system prompt only | Create new memory state |

## 3. Single Truth Source Per Concern

| Concern | Canonical Source | Derived / Audit Only |
|---|---|---|
| Raw replay evidence | `logs/YYYY-MM-DD/behavior/*.md`, DB ChatMessage, invocation spans | `logs/.../system/*.md` |
| Post-turn semantic decision | Learning Brain `MemorySignalEnvelope` | Fast reflection fallback result |
| T2 atom candidates | `memory/learnings/*.md` + lifecycle sidecar | Extractor queue/cursor |
| T3 durable semantic memory | `memory/feedback.md`, `knowledge.md`, `strategies.md`, `blocked.md`, `user.md` | Memory Navigation |
| Identity memory | `soul.md` | dream reasoning, promotion candidates |
| Candidate/eval/promotion decisions | `evolution/evolution_ledger.jsonl` | ActivityLog UI timeline |
| Heartbeat counters/history | `evolution/scorecard.md`, `lineage.md`, `blocklist.md` | Not semantic memory |
| Read visibility | `ActivationContext` + `PrincipalStack` + lifecycle metadata | prompt preview |
| Search acceleration | none configured | Future adapters must be rebuildable and never truth |

## 4. New Core Contract: `MemorySignalEnvelope`

All semantic lanes should converge on a single internal signal schema before anything becomes T2/T3/soul/skill/workflow evidence.

```python
class MemorySignalEnvelope(TypedDict):
    schema: Literal["memory_signal.v1"]
    agent_id: str
    tenant_id: str
    source: Literal[
        "web_turn",
        "trigger_turn",
        "delegation_turn",
        "workflow_turn",
        "heartbeat_reflection",
        "session_feedback",
        "work_ledger",
        "t0_backfill",
    ]
    source_refs: list[str]
    evidence_refs: list[str]
    signal_type: Literal[
        "low_signal",
        "session_learning",
        "memory_candidate",
        "soul_candidate",
        "skill_candidate",
        "workflow_candidate",
        "lifecycle_patch",
        "audit_only",
    ]
    lesson: str
    confidence: float
    volatility: Literal["ephemeral", "session", "project", "stable"]
    evidence_kind: Literal["user_stated", "tool_verified", "system_observed", "inferred"]
    routing_hint: Literal[
        "none",
        "memory_append",
        "soul_candidate",
        "skill_candidate",
        "workflow_candidate",
        "artifact_only",
    ]
    rationale: str
    boundary_checks: dict[str, bool | str]
    created_at: str
```

Rules:

- Learning Brain is the primary writer of this envelope for live turns.
- Backfill may replay old evidence into the same envelope path.
- Extractor consumes the envelope and creates T2 atoms when `signal_type in {"memory_candidate", "session_learning", "soul_candidate", "skill_candidate", "workflow_candidate"}`.
- `audit_only` and `low_signal` never enter T2/T3.
- Fast Reflection becomes a compatibility adapter around this envelope, not an independent semantic owner.

## 5. Clean Runtime Loop

### 5.1 Normal Turn

```text
invoke_agent
  -> DB ChatMessage / invocation_spans
  -> RESPONSE_COMPLETE
  -> Learning Brain full-context decision
  -> MemorySignalEnvelope
  -> if session_learning: session projection only
  -> if memory/soul/skill/workflow candidate: evolution ledger candidate + optional T2 atom
  -> Extractor atomizes approved/enveloped evidence into T2
  -> heartbeat later curates T2 -> T3
  -> dream later consolidates T3/candidates -> soul/T3 lifecycle
  -> retriever/activation/prompt read the latest governed state
```

### 5.2 Heartbeat Reflection

```text
Heartbeat curator runs T2 -> T3
  -> final heartbeat reply
  -> if noop: audit only
  -> if action_taken / curated / failure / crash:
       route full model reflection as source=heartbeat_reflection
       Learning Brain produces MemorySignalEnvelope
       normal candidate/T2 path handles it
  -> lineage/scorecard remain audit only
```

### 5.3 Dream

```text
Dream reads T3 + soul + recent candidate evidence
  -> LLM reconsolidation decision
  -> memory promotion candidates recorded in evolution_ledger
  -> Memory Control Plane gates source refs, volatility, rollback, frozen mission
  -> accepted soul/T3 lifecycle patch is applied
  -> rejected/held decisions remain visible in ledger
```

## 6. What To Remove Or Downgrade

### R1. Demote Fast Reflection classifier

Current issue: Fast Reflection still has marker-based semantic fallback and can produce candidates independently.

Target:

- Keep `fast_reflection_service.py` as adapter/persistence layer.
- Move semantic classification fully into Learning Brain.
- Mechanical marker fallback may only produce `MemorySignalEnvelope(signal_type="audit_only" or "low_signal", method="mechanical_fallback")`, unless explicitly enabled by an operator fallback mode.

### R2. Split Extractor into two modes

Current issue: Extractor both extracts atoms and carries container routing hints.

Target:

- `extract_live_envelope(envelope)` — atomizes a Learning Brain decision.
- `extract_replay_packet(packet)` — LLM-primary replay from T0/backfill when no envelope exists.
- Container intent comes from envelope first; Extractor can only downgrade thin evidence, not upgrade it to soul/skill/workflow.

### R3. Slim Heartbeat

Current issue: Heartbeat is curator plus reflection router plus skill distiller runner plus scene/wiki curator plus dream scheduler plus audit writer.

Target:

- Heartbeat owns only:
  - read T2/T3
  - call Memory Curator LLM
  - call governed T3 append
  - mark T2 absorbed only after accepted/held decisions are recorded
  - emit heartbeat audit event
  - route non-noop reflection back to Learning Brain
- Move skill distillation, scene/wiki curation, and dream scheduling to a separate `EvolutionScheduler` or daemon tick that consumes ledger/activity events.

### R4. Make Dream writeback purely candidate + patch based

Current issue: Dream still contains direct writeback logic for soul sections and T3 retirement, even though it records ledger decisions.

Target:

- Dream returns `DreamDecision`.
- `MemoryControlPlane.apply_dream_decision()` applies:
  - `soul_patch_candidate`
  - `t3_merge_patch`
  - `t3_contradiction_patch`
  - `preservation_flag_patch`
- All applied patches have candidate id, decision id, source refs, rollback ref.

### R5. Collapse audit surfaces by role

Current issue: UI and operators see too many surfaces with overlapping semantics.

Target:

- `evolution_ledger.jsonl` = candidate/eval/promotion/rollback truth.
- `lineage.md` / `scorecard.md` = human-readable heartbeat audit only.
- `AgentActivityLog` = UI event stream only.
- `invocation_spans` = trace/debug truth.
- `logs/.../system/*.md` = distiller reasoning audit only.

Every UI label must say which category it is: `semantic`, `candidate`, `audit`, `trace`, or `derived`.

### R6. Introduce a real Memory Control Plane facade

Current issue: control-plane rules are scattered.

Target module:

```text
backend/app/memory/control_plane.py
```

Public API:

```python
class MemoryControlPlane:
    async def propose_signal(envelope: MemorySignalEnvelope) -> CandidateDecision
    async def append_t2_atom(atom: T2Atom) -> T2WriteResult
    async def append_t3_candidate(candidate: T3Candidate) -> T3AppendResult
    async def apply_lifecycle_patch(patch: LifecyclePatch) -> PatchResult
    async def apply_soul_patch(patch: SoulPatch) -> SoulPatchResult
    async def retire_memory(entry_id: str, reason: str, source_refs: list[str]) -> RetireResult
    async def retrieve_context(query: str, principal: PrincipalStack) -> MemoryContext
```

Existing modules may remain internally, but callers stop reaching into them directly.

## 7. Target Ownership Boundaries

### Allowed Write Paths

| Target | Only Allowed Writer |
|---|---|
| `logs/.../behavior/*.md` | T0 logger |
| `logs/.../system/*.md` | T0 logger from distiller hooks |
| `memory/learnings/*.md` | Memory Control Plane via Extractor/T2 adapter |
| `memory/*.md` T3 | Memory Control Plane via `append_t3_candidate` |
| `memory/archive.md` | Memory Control Plane lifecycle patch |
| `memory/lifecycle.json` | Memory Control Plane |
| `soul.md` learned behavior sections | Memory Control Plane soul patch |
| `evolution/evolution_ledger.jsonl` | Candidate/eval/promotion services through ledger API |
| `evolution/lineage.md`, `scorecard.md`, `blocklist.md` | platform audit writer only |
| Memory enhancement adapter | empty no-op hook only; no configured program |

### Forbidden Crossovers

- Learning Brain cannot write files.
- Extractor cannot promote.
- Heartbeat cannot write skill/workflow files or run unrelated distillers inline.
- Dream cannot apply identity changes without candidate/decision/rollback records.
- `lineage.md` cannot be a semantic source for T2/T3/soul.
- No external enhancement program can become the source of truth.
- Prompt assembly cannot mutate memory state.

## 8. Implementation Order

This is an implementation order, not an MVP boundary. The code pass is complete only when all rows are done.

1. Add architecture guard tests for the ownership table.
2. Add `MemorySignalEnvelope` schema and parser/validator.
3. Make Learning Brain emit `MemorySignalEnvelope`.
4. Change Fast Reflection to consume the envelope instead of owning classification.
5. Split Extractor into live-envelope and replay-packet paths.
6. Route heartbeat reflection through the envelope path.
7. Add `MemoryControlPlane` facade and migrate new callers to it.
8. Slim Heartbeat: move skill distillation, scene/wiki curation, and dream trigger to `EvolutionScheduler`.
9. Change Dream apply path to `MemoryControlPlane.apply_dream_decision`.
10. Normalize observability labels across ActivityLog, evolution view, and memory UI.
11. Run full backend tests and targeted production/eval live traces.

## 9. Redline Tests

### Architecture / Ownership

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/architecture/test_memory_clean_loop_ownership.py -q
```

Required assertions:

- Learning Brain has no filesystem writes.
- Extractor has no T3/soul/skill/workflow write calls.
- Heartbeat does not call skill distiller/scene/wiki/dream directly after scheduler split.
- Dream writeback enters Memory Control Plane facade.
- `lineage.md` is not consumed as semantic memory input.
- No concrete external enhancement program is wired into T3 recall/writeback.

### Signal Contract

```bash
pytest tests/services/test_memory_signal_envelope.py -q
```

Required assertions:

- Learning Brain emits valid `memory_signal.v1`.
- `low_signal` and `audit_only` never create T2/T3.
- `heartbeat_reflection` preserves source refs and full reflection enough for learning.
- Fast Reflection cannot create semantic candidates without an envelope except fallback-stamped low-signal/audit-only.

### Extractor

```bash
pytest tests/services/test_extract_agent.py -q
```

Required assertions:

- Envelope path preserves Learning Brain routing hints.
- Replay path is LLM-primary and fallback-stamped.
- Extractor can downgrade thin evidence but cannot upgrade to soul/skill/workflow.

### Heartbeat

```bash
pytest tests/services/test_heartbeat.py tests/services/test_heartbeat_reflection_learning.py -q
```

Required assertions:

- Heartbeat only curates T2 -> T3 and emits audit/reflection events.
- T2 entries are not marked absorbed unless curation result is recorded.
- Noop heartbeat stays audit-only.
- Non-noop heartbeat reflection re-enters Learning Brain through envelope path.

### Dream

```bash
pytest tests/services/test_auto_dream.py tests/services/test_evolution_ledger.py -q
```

Required assertions:

- Dream decisions become candidates/patches before writeback.
- Soul promotions require source refs, verified evidence, frozen mission gate, and rollback ref.
- Mechanical repeated-feedback fallback proposes a candidate, not direct identity write.

### Read / Prompt

```bash
pytest tests/runtime/test_prompt_sections.py tests/runtime/test_system_prompt_budget.py tests/memory/test_activation.py -q
```

Required assertions:

- `soul.md` enters frozen identity only.
- T3/T2/session projection enter dynamic memory only.
- Principal/PL visibility strips inaccessible memories.
- Absence of external enhancement does not remove canonical T3 recall.

## 10. Acceptance Bar

The refactor is accepted only when:

1. There is one post-turn semantic decision owner: Learning Brain.
2. There is one durable write authority: Memory Control Plane.
3. Heartbeat is only a memory curator, not an evolution orchestrator.
4. Dream is only a reconsolidator/identity proposer, not a free writer.
5. All candidates and promotion decisions have source refs and rollback refs where relevant.
6. Audit surfaces are labeled and cannot be mistaken for semantic memory.
7. Prompt assembly has no write side effects.
8. Full backend tests pass.
9. Railway production/eval can show one live trace:

```text
turn / heartbeat reflection
  -> MemorySignalEnvelope
  -> candidate or T2 atom
  -> heartbeat curation or dream consolidation
  -> governed decision
  -> prompt recall
```

## 11. Decision

Do not add more side paths. The next pass should be a responsibility refactor:

- centralize semantic decisions into Learning Brain,
- centralize write authority into Memory Control Plane,
- demote other services into adapters, curators, reconsolidators, schedulers, or audit writers,
- keep the four-layer pyramid as the storage core,
- keep all derived indexes rebuildable and non-authoritative.
