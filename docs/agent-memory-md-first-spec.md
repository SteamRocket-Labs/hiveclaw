# Agent Memory MD-first Spec

> Merged specification from `docs/agent-memory-research.md` and `docs/agent-memory-md-first-research-codex.md`.
> Date: 2026-06-04.
> Scope: Hive agent memory architecture, MD-first source of truth, distillation boundaries, Memory Control Plane, and AgentDetail presentation.

## 0. Executive Position

Hive memory should be specified as:

```text
Memory Engine =
  MD-first plaintext memory
  + lifecycle-governed distillation
  + dynamic activation
  + evidence-backed promotion
  + reversible retirement
```

It should not be described as "neural memory" or as "vector DB recall".

The strongest architecture rule is:

```text
Markdown is the durable truth source.
Indexes, vector stores, Hindsight, graph views, manifests, and UI read models are derived accelerators.
Every derived result must be rebuildable from Markdown plus runtime artifacts.
```

The second rule is:

```text
Distillers produce candidates.
The Memory Control Plane decides writes, activation, promotion, retirement, and audit.
```

The third rule is:

```text
Knowledge is not the parent of Skills, MCP, or Workflows.
Knowledge explains evidence and activation.
Skills, MCP, and Workflows remain independent capability / protocol / execution modules.
```

## 1. Honest Scientific Position

The original strong claim, "all distillation is based on neuroscience memory consolidation", should be weakened.

Hive's current T0 -> T2 -> T3 -> soul pyramid is best understood as multi-level compression inside MemOS-style plaintext memory. It does not implement activation-state memory such as KV-cache, parameter-state memory such as LoRA, or neural mechanisms directly.

The scientifically honest claim is:

```text
Hive distillation is structurally inspired by consolidation and forgetting principles,
but its thresholds, scoring, intervals, categories, and permissions are engineering rules.
```

### 1.1 Valid Inspirations

These principles are useful because they produce engineering constraints:

| Principle | Engineering Constraint |
| --- | --- |
| Complementary Learning Systems | Keep fast encoding and slow consolidation separate. Heartbeat and Dream should not be merged merely to reduce component count. |
| Interleaved replay | Consolidation should mix new candidate memories with relevant old memories, not only process deltas. |
| Episodic vs semantic distinction | Preserve raw episodes / scenes separately from semantic wiki claims. Do not collapse everything into generic facts. |
| Reconsolidation | A memory that is retrieved, corrected, contradicted, or successfully reused should become eligible for lifecycle update. |
| Reversible forgetting | Retirement should usually mean de-index / archive from active recall, not physical deletion. |
| Interference vs decay | Retirement needs two lanes: contradiction / supersession and long-term low-usage decay. |
| Adaptive forgetting | Removing low-value active memories improves retrieval quality; forgetting is a feature, not merely data loss. |

### 1.2 Claims To Avoid

Avoid these product or architecture claims:

- "T0/T2/T3/soul are biologically faithful memory layers."
- "Importance score has a neuroscience-grounded threshold."
- "A 45 minute or 4 hour consolidation interval is scientifically determined."
- "Soul is long-term memory in the neural sense."
- "Distillation equals brain consolidation."

Use this instead:

```text
The memory engine uses MD-first plaintext memories with lifecycle governance.
It borrows the shape of fast encoding, slow consolidation, reconsolidation, and reversible forgetting,
while all concrete gates are observable engineering policy.
```

### 1.3 Optional Future Scientific Route

HippoRAG is the strongest brain-inspired candidate because it maps a theory to a falsifiable engineering choice: knowledge graph + Personalized PageRank for pattern completion style multi-hop retrieval.

For Hive this should stay future-facing:

```text
T3 / wiki Markdown -> derived KG -> PPR multi-hop retrieval -> source_ref back to Markdown
```

Do not make KG/PPR a P0 requirement.

## 2. Container Boundary

Hive has four durable knowledge containers and two adjacent execution/protocol surfaces:

```text
Memory   = evidence, episodes, scenes, semantic wiki, lifecycle, source refs
Soul     = stable identity, accountability, charter, long-term behavior invariants
Skill    = reusable verified method packaged as SKILL.md
Workflow = durable executable process with state, gates, replay, and verification
MCP      = external connector/tool protocol surface
Artifact = runtime output / Work Ledger / task evidence, not long-term memory by default
```

Memory is the root evidence layer, but it is not the final home for everything:

```text
T0 / runtime artifacts
  -> T2 atom
  -> T3 memory entry / episode / scene / wiki
  -> candidate:
       soul patch
       skill candidate
       workflow candidate
       memory lifecycle patch
       artifact-only
```

### 2.1 Memory

Memory should contain:

- Durable facts.
- User corrections and preferences that may later promote to soul.
- Project / domain knowledge.
- Failure patterns and blocked approaches.
- Repeated strategy evidence before it becomes a skill or workflow.
- Episodes and scenes with source refs.
- Wiki concept pages with claims, scope, evidence, contradictions, and retrieval tags.
- Lifecycle state such as active, stale, superseded, archived, frozen.

Memory should not contain:

- Current task state.
- One-off runtime artifacts with no durable value.
- Frozen identity policy.
- Installed skill source of truth.
- MCP auth/tool configuration source of truth.
- Workflow run state source of truth.

### 2.2 Soul

Soul should contain:

- Long-term agent identity.
- Owner and company accountability.
- Frozen company / owner charter sections.
- Stable behavior invariants.
- Repeatedly confirmed high-level preferences.
- Authority boundaries and non-negotiable constraints.

Soul should not contain:

- Temporary project facts.
- Single-session corrections.
- Paths, ports, API details, or current branch state.
- SOP steps that belong in skills or workflows.
- Tool or MCP configuration.
- Anything that expands authority without approval.

Soul promotion gate:

```text
memory evidence
  + repeated signal or explicit owner instruction
  + no active contradiction
  + no policy conflict
  + owner/company context known
  + approval if frozen / charter / authority boundary
  -> soul patch proposal
```

### 2.3 Skill

Skill is an independent standard protocol module, not a Knowledge subpage.

Skill should contain:

- A reusable method.
- Trigger conditions.
- Steps.
- Verification commands or checklists.
- Failure handling.
- Examples or helper scripts when useful.
- Progressive disclosure instructions.

Skill should not contain:

- One-off facts.
- Current project status.
- Unverified inspiration.
- Durable workflow state.
- MCP connector auth.

Skill promotion gate:

```text
scene or strategy evidence
  + repeated successful use
  + at least one failure or edge case considered
  + verification method
  + source_refs to memory entries / runtime artifacts
  + no need for durable workflow state
  -> SKILL.md candidate
```

### 2.4 Workflow

Workflow should contain:

- Multi-step execution structure.
- Durable state.
- Start / stop conditions.
- Checkpoints.
- Resume / replay semantics.
- Verification gates.
- Worker / verifier / integration boundaries when needed.
- Audit artifacts.

Workflow should not contain:

- A single reusable trick that fits a skill.
- Static facts.
- Simple preferences.
- Natural-language SOP with no state or gate.

Workflow promotion gate:

```text
repeated multi-step task
  + clear start/stop condition
  + durable state required
  + step journal required
  + failure recovery required
  + verification contract
  -> workflow definition candidate
```

### 2.5 MCP

MCP is an independent external protocol surface. It should be governed like a capability module, not stored under Knowledge.

MCP owns:

- Server registry.
- Tool discovery.
- Auth and secrets boundary.
- Capability policy.
- Availability / health / tool metadata.
- Audit of external tool access.

Knowledge may reference MCP tools as evidence:

```text
memory entry: "This agent often needs spreadsheet parsing."
linked capability ref: MCP server/tool candidate
```

But installation, auth, and execution stay in MCP.

## 3. MD-first Memory Shape

Everything durable should remain legible as Markdown, but "Everything is Markdown" does not mean "everything is one Markdown file".

Recommended logical layers:

```text
soul.md
relationships.md
memory/
  feedback.md
  blocked.md
  knowledge.md
  strategies.md
  user.md
  wiki/
    <concept>.md
  scenes/
    <scene>.md
  INDEX.md
  lifecycle.json
  access_log.jsonl
  distillation_audit.jsonl
skills/
  <skill>/SKILL.md
workflows/
  <workflow>.yaml|json|md
runtime_artifacts/
  ...
```

`lifecycle.json`, `access_log.jsonl`, and `distillation_audit.jsonl` may be structured sidecars. They still serve the MD-first model because they explain and index Markdown truth, rather than replacing it.

### 3.1 Markdown Micro-syntax For Wiki / Scenes

Borrow the useful part of basic-memory: make graph relations expressible in Markdown.

Example:

```markdown
---
title: Memory Control Plane
type: concept
tags: [memory, governance]
status: active
---

## Current Claim

Distillers should not directly own final durable writes.

## Evidence

- [decision] Distillers produce candidates; Memory Control Plane decides writes. #governance
- [fact] Extractor already routes T2 writes through write gate. #code
- [risk] Heartbeat and Dream can still rewrite T3 Markdown directly. #gap

## Relations

- depends_on [[Memory Write Gate]]
- supersedes [[Direct T3 Rewrite]]
- blocks [[Ungoverned Skill Promotion]]
```

Parsing rules:

- Frontmatter carries structured metadata.
- `## Evidence` lines can use `[category] content #tag`.
- `## Relations` lines can express typed edges to `[[wikilinks]]`.
- Inline `[[wikilink]]` references are allowed and may point to pages not yet created.
- Derived graph indexes must resolve links back to Markdown paths and entry ids.

### 3.2 Entry Metadata

Each memory entry should eventually carry:

```yaml
entry_id: stable id
source_refs: runtime artifact / T0 / T2 / message refs
category: feedback | constraint | blocked_pattern | project | reference | general | strategy | user
concepts: []
scope: personal | agent | owner | company | tenant
sensitivity: PL0 | PL1 | PL2 | PL3 | PL4
confidence: 0.0-1.0
importance: engineering score, not neuroscience truth
status: active | stale | superseded | archived | frozen
created_at:
updated_at:
last_recalled_at:
recall_count:
supersedes: []
contradicts: []
promoted_to: soul | skill | workflow | null
```

First implementation can encode most of this in frontmatter or a sidecar manifest rather than rewriting every existing bullet format at once.

## 4. Lifecycle Pipeline

Hive should make the whole memory lifecycle explicit:

```text
capture
  -> encode
  -> atom extraction
  -> episode / scene consolidation
  -> wiki consolidation
  -> activation / retrieval
  -> use
  -> reconsolidation
  -> promotion or retirement
```

Every stage must make six operational elements explicit. Semantic judgment belongs to the LLM unless a deterministic mechanical rule is explicitly listed. Mechanical code may validate schemas, rebuild indexes, update counters, enforce policy, deny writes, or hold candidates; it must not silently replace semantic judgment.

| Stage | Input | Decision Owner | Criteria | Evidence | Output | Failure Handling |
| --- | --- | --- | --- | --- | --- | --- |
| Capture | messages, RuntimeTask journals, Work Ledger, tool results, artifacts | mechanical recorder + policy prefilter | event validity, agent scope, tenant/owner context, access rights | raw event ids, task ids, artifact paths | source refs / raw packets | record `capture_error`, retry if transient, do not synthesize memory from missing evidence |
| Encode | captured raw packet | mechanical context builder | timestamp, principal context, goal, user instruction, artifact refs, sensitivity hints | raw refs plus current owner/company context | source packet for LLM judgment | mark packet `incomplete_context` and hold if principal or source refs are missing |
| Atom Extraction | source packet + nearby existing entries | LLM primary + mechanical schema validator | durability, surprise, persistence, correction value, conflict, sensitivity, container candidate | source refs, retrieved comparable entries, extraction prompt version | atom / T2 candidate | on LLM or schema failure, write no durable memory; keep source packet for retry and audit the failure |
| Episode / Scene Consolidation | atom batch, similar scenes, relevant old memory | LLM primary + mechanical similarity/capacity support | update vs merge vs create, anti-proliferation, interleaved replay, heat | atom ids, scene ids, old/new diffs, heat | scene patch candidate | if duplicate vs contradiction is ambiguous, hold unresolved candidate instead of rewriting Markdown |
| Wiki Consolidation | scenes, T3 entries, existing wiki page | LLM primary + mechanical link/frontmatter parser | current claim, scope, evidence strength, contradiction, supersession | entry ids, scene refs, current page revision | wiki patch candidate | if claim confidence is low, add or hold contradiction evidence; do not overwrite current claim |
| Activation / Retrieval | goal, query, principal context, manifest/index | mechanical retrieval + policy filter; LLM rerank when needed | relevance, recency, importance, heat, owner/company match, sensitivity access | manifest, lifecycle state, access log, query match | activated memory packet with reasons | if index fails, fall back to direct T3 scan with degraded marker; if policy is uncertain, suppress |
| Use | activated memory + runtime outcome | mechanical telemetry; LLM/user signal for semantic outcome when available | helpful, unused, contradicted, user-corrected, caused bad action | run id, task id, response/tool outcome, user correction | access log / use outcome event | telemetry failure is non-fatal but auditable; never mutate memory based only on missing telemetry |
| Reconsolidation | use outcomes, corrections, conflicts, scope drift | LLM primary + mechanical lifecycle writer | supersede, contradict, archive, freeze, keep active | old/new entry ids, correction refs, policy context | lifecycle patch candidate | on LLM failure or ambiguous result, hold candidate; do not delete or rewrite source Markdown |
| Promotion / Retirement | memory candidates, lifecycle patches, usage data | PromotionRouter fast-path + LLM adjudication + policy/human gate | soul/skill/workflow gates, decay, interference, owner approval, protected evidence | source refs, repeated evidence, eval results, access log | target candidate or lifecycle state change | denied or low-confidence cases become held/rejected audit records; protected evidence is never physically deleted |

### 4.1 Capture

Inputs:

- Chat messages.
- RuntimeTask journals.
- Work Ledger.
- Tool observations.
- User corrections.
- External artifacts.
- Verification results.

Capture does not decide long-term truth. It records evidence.

### 4.2 Encode

Encode creates source packets with enough context for LLM judgment:

```text
source packet =
  absolute timestamp
  agent_id / owner / company / tenant context
  goal / task summary
  user instruction
  tool observations
  artifact refs
  sensitivity hints
```

### 4.3 Atom Extraction

Extractor should produce atom candidates, not directly write soul, skills, or workflows.

Required fields:

```text
content
category
concepts
source_refs
confidence
sensitivity
container_candidate
reason
```

Extractor questions:

1. Is this durable?
2. Is it surprising, persistent, corrective, or safety-relevant?
3. Does it conflict with existing memory?
4. Should it remain memory, become a candidate, or stay artifact-only?

### 4.4 Episode / Scene Consolidation

Scene consolidation should prefer update / merge over creating new files.

Borrow from Tencent's anti-proliferation design:

- Default to updating an existing scene.
- Create only after checking similar scenes.
- Use capacity caps to force merge.
- Track heat as usage / update count.
- Preserve evolution trail when conflicts appear.
- Avoid simple bullet append; produce a coherent narrative when appropriate.

Heartbeat should not only process new T2 deltas. It should include relevant old memory as interleaved replay context.

### 4.5 Wiki Consolidation

Wiki pages hold semantic claims:

```markdown
# <Concept>

## Current Claim

## Scope

## Evidence

## Contradictions

## Changes

## Retrieval Tags
```

Wiki is the default user-facing knowledge view. It is not a random document dump.

### 4.6 Activation / Retrieval

Activation should produce a reasoned memory packet:

```text
Always-on:
  soul identity / frozen charters

High priority:
  recent P0 feedback and blocked patterns

Selected:
  T3 entries by relevance, recency, importance, owner/company scope, sensitivity access

Indexed:
  navigation rows with id/path/summary/heat/reason

On demand:
  load_memory(ids) / read_file for full Markdown
```

Retrieval should expose why a memory was activated:

```text
activated because:
  query match
  owner/company match
  high heat
  recent correction
  related workflow or skill evidence
  safety constraint
```

### 4.7 Use

Using a memory should update telemetry:

```text
entry_id
used_at
run_id / task_id
activation_reason
outcome: helpful | neutral | contradicted | user_corrected | unused
```

This usage data feeds retention, heat, retirement, and promotion.

### 4.8 Reconsolidation

Reconsolidation handles:

- User correction.
- Conflict between new and old facts.
- Successful repeated use.
- Failed repeated use.
- Owner/company scope drift.
- Sensitive access changes.

Output should be a lifecycle patch, not silent deletion:

```text
old entry -> superseded / archived / stale
new entry -> active
edge -> supersedes / contradicts / derived_from
audit -> why
```

### 4.9 Retirement

Retirement should be reversible by default.

Two lanes:

```text
interference lane:
  new memory conflicts with old memory
  -> dedupe / supersede / contradiction edge

decay lane:
  low recall, low utility, old scope, no safety protection
  -> de-index from active recall, archive in Markdown
```

Do not physically delete PL4, audit, compliance, approval, or charter evidence.

## 5. Distillers And Memory Control Plane

Current Hive should keep the distillers but reduce their authority.

| Component | Target Role | Authority Boundary |
| --- | --- | --- |
| Extractor | Fast atom extraction from T0/message/Work Ledger into T2 candidates | Does not write T3/soul/skill/workflow directly. |
| Heartbeat | Medium-frequency Memory Curator: T2 -> T3 candidates, scene/wiki updates, candidate signals | Does not directly save skills; should not bypass governed T3 append. |
| Dream | Slow Reconsolidator + IdentityPromoter: T3 cleanup, contradictions, soul proposals, retirement | Does not silently delete T3; does not bypass owner/charter gates. |
| SkillDistiller | Skill promotion lane consuming evidence-backed skill candidates | Does not bypass Memory Engine evidence and promotion routing. |

The control relationship:

```text
Extractor ─┐
Heartbeat ├──> PromotionRouter / Memory Control Plane -> MD truth source + lifecycle + derived indexes
Dream ────┘
SkillDistiller consumes skill_candidate evidence and returns candidate/eval/audit.
```

Memory Control Plane owns:

- write gate
- sensitivity and owner/company access
- lifecycle state
- activation policy
- promotion router
- retention / retirement
- Hindsight sync as derived index
- audit / replay / eval evidence

## 6. PromotionRouter

Introduce a small, testable router before changing large runtime flows.

```python
class PromotionKind(str, Enum):
    MEMORY_APPEND = "memory_append"
    SOUL_CANDIDATE = "soul_candidate"
    SKILL_CANDIDATE = "skill_candidate"
    WORKFLOW_CANDIDATE = "workflow_candidate"
    ARTIFACT_ONLY = "artifact_only"
    LIFECYCLE_PATCH = "lifecycle_patch"


@dataclass
class PromotionCandidate:
    kind: PromotionKind
    content: str
    source_refs: list[str]
    evidence: str
    confidence: float
    scope: str
    concept: str | None = None
    reason: str = ""
```

First-pass routing rules:

```text
feedback / constraint + repeated or explicit -> soul_candidate or memory_append
blocked_pattern -> memory_append or lifecycle_patch
project / reference / general / user -> memory_append
strategy + repeated success + no durable state -> skill_candidate
strategy + durable state / replay / verifier / multi-step gate -> workflow_candidate
runtime-only evidence -> artifact_only
contradiction / duplicate / stale -> lifecycle_patch
```

These rules are a deterministic fast-path, not the semantic source of truth.

AI-native routing rule:

```text
exact rule match + high confidence + no conflicting signals
  -> produce deterministic candidate

ambiguous, low confidence, cross-container, policy-sensitive, or evidence-conflicting case
  -> escalate to LLM adjudication with source refs and retrieved comparable memory

LLM unavailable, schema invalid, or adjudication still uncertain
  -> HELD candidate with audit reason; no durable target write
```

Mechanical routing must never silently choose between `soul_candidate`, `skill_candidate`, and `workflow_candidate` when the evidence is semantic or ambiguous. This prevents the Mem0-V3 failure mode where semantic memory control degrades into hash/entity heuristics.

The important invariant:

```text
One signal should not be simultaneously written as T3 strategy, SKILL.md, and workflow definition.
It should first become a candidate with evidence and then be promoted by target-specific gates.
```

## 7. T3 File Boundary

Current files:

```text
feedback.md    P0 always
blocked.md     P0 always
knowledge.md   P1 on-demand
strategies.md  P1 on-demand
user.md        P2 optional
```

`knowledge.md` and `strategies.md` currently have similar retrieval priority, but they still carry different routing and promotion semantics. Do not immediately merge them without a migration.

Near-term decision:

```text
Keep both files for compatibility.
Move the long-term boundary from file name to entry-level category and metadata.
Mark promoted strategy entries with promoted_to_skill or promoted_to_workflow.
Only consider merging knowledge.md + strategies.md after runtime, prompt, dream, tests, and migration semantics agree.
```

Formal anti-proliferation rule:

```text
Create a new T3 file only when it has either:
  1. different activation priority, or
  2. different governance axis, or
  3. different lifecycle policy.

Pure taxonomy differences belong in entry-level category/concept tags.
```

## 8. Index And Navigation

`memory/INDEX.md` should not be an orphan. It needs a runtime consumer.

Recommended first form:

```text
Memory Navigation prompt section:
  - entry_id
  - source path
  - category
  - summary
  - heat / recall_count
  - last_updated
  - activation reason
  - load command / id
```

Do not append this directly into `soul.md`. Soul must stay identity, not navigation. Instead render it as a separate prompt section during activation:

```text
Soul
Memory Navigation
Activated Memory
Available Skills
Workflow State
Tool/MCP Context
```

The navigation section supports progressive disclosure:

```text
agent sees summary + path/id
agent calls load_memory(ids) or read_file(path)
runtime returns full Markdown with source refs
```

## 9. Hindsight And Other Indexes

Hindsight remains a read-side accelerator over T3 Markdown.

Rules:

- T3 Markdown is source of truth.
- Hindsight store is not a write path.
- Hindsight can be disabled without losing durable memory.
- Rebuild must be possible from `memory/*.md`.
- Activation must preserve sensitivity and owner/company filtering even when using Hindsight.

The same rules apply to BM25, vector, graph, SQLite, or future KG/PPR indexes.

## 10. Frontend Information Architecture

The current AgentDetail problem is not only visual design. It exposes internal files and capability surfaces without a stable product model.

The target top-level IA:

```text
Overview | Chat | Knowledge | Skills | MCP | Workflows | Workspace | Activity | Settings
```

### 10.1 Knowledge Plane

Knowledge answers:

```text
What does this agent know?
Where did that knowledge come from?
Which memories are active now?
Which memories are stale, superseded, or blocked?
Which candidates may become soul/wiki/memory lifecycle changes?
Why was a Skill / MCP / Workflow referenced?
```

Knowledge owns:

- Overview
- Wiki
- Memory entries
- Soul view / soul patch candidates
- Timeline
- Candidate queue for knowledge / identity / memory cleanup
- Raw Markdown advanced view for `soul.md`, `memory/`, `evolution/`

Knowledge does not own:

- Skill installation or editing.
- MCP server/tool/auth management.
- Workflow definition or run control.

### 10.2 Capability Plane

Skills owns:

- `skills/<skill>/SKILL.md`
- Skill registry.
- User-installed skills.
- Third-party skills.
- Self-evolved skill candidates after approval.
- Skill lifecycle and eval status.

MCP owns:

- MCP servers.
- MCP tools.
- Auth / secrets.
- Capability policy.
- Health / availability.

Workflows owns:

- Workflow definitions.
- Run history.
- Resume / replay.
- Gates and verifier evidence.
- SOP hardening.

Knowledge may show linked references:

```text
Skill candidate -> open Skills module
Workflow candidate -> open Workflows module
MCP tool need -> open MCP module
```

It should not approve or manage those modules inside Knowledge.

### 10.3 Knowledge UI Shape

Recommended layout:

```text
┌─────────────────────────────────────────────────────────────┐
│ Knowledge header: health, freshness, active candidates       │
├───────────────┬───────────────────────────┬─────────────────┤
│ Left nav      │ Center wiki/page view      │ Right inspector │
│ - Overview    │ - selected concept/page    │ - provenance    │
│ - Wiki        │ - linked entries           │ - lifecycle     │
│ - Memory      │ - markdown-rendered body   │ - activation    │
│ - Soul        │ - related capabilities     │ - actions       │
│ - Candidates  │                           │                 │
│ - Timeline    │                           │                 │
│ - Raw MD      │                           │                 │
└───────────────┴───────────────────────────┴─────────────────┘
```

Default view should be Wiki / Overview, not a file browser.

## 11. Read Model API

Before frontend redesign, create a stable read model:

```text
GET /api/agents/{agent_id}/knowledge/overview
GET /api/agents/{agent_id}/knowledge/pages
GET /api/agents/{agent_id}/knowledge/pages/{page_id}
GET /api/agents/{agent_id}/knowledge/entries
GET /api/agents/{agent_id}/knowledge/events
GET /api/agents/{agent_id}/knowledge/candidates
```

Initial data sources:

- `soul.md`
- `memory/INDEX.md`
- `build_t3_entry_manifest()`
- `memory/lifecycle.json`
- `memory/access_log.jsonl`
- `understandings.md`
- `evolution/*`
- distillation audit artifacts
- linked capability refs from skill/workflow/MCP surfaces

Example overview type:

```ts
type AgentKnowledgeOverview = {
  identity: {
    sections: number;
    frozenSections: number;
    pendingSoulCandidates: number;
    lastUpdated?: string;
  };
  memory: {
    active: number;
    stale: number;
    superseded: number;
    archived: number;
    sensitiveSuppressed: number;
  };
  distillers: {
    extractor: DistillerStatus;
    heartbeat: DistillerStatus;
    dream: DistillerStatus;
    skillDistiller: DistillerStatus;
  };
  linkedCapabilities: {
    skillsReferenced: number;
    workflowsReferenced: number;
    mcpToolsReferenced: number;
  };
};
```

## 12. Implementation Roadmap

> **Status (2026-06-05): P0–P10 ALL IMPLEMENTED** — one commit per phase
> (`60502154` P0 → `28d31666` P1 → `e3fa4480` P2 → `18cf3f5b` P3 →
> `a2bfdffc` P4 → `8ba6feed` P5 → `1023818f` P6 → `acd661a0` P7 →
> `a1478e6d` P8 → `7978a6b7` P9 → P10 review closure committed 2026-06-05
> after independent main-agent review: full backend 3750 passed / 7 skipped),
> each with TDD red→green evidence recorded under its phase below. P9 baseline:
> backend 3712 passed / ruff clean, frontend 154 passed / tsc clean /
> production build passed. P9 was
> originally deferred (§13) and explicitly green-lit by the owner on
> 2026-06-05; the retrieval benchmark settled the experiment (PPR wins
> multi-hop 1.0 vs 0.333 with no direct-hit regression).

### P0: Freeze Terms And Prompt Contracts

**Status: ✅ DONE (2026-06-04).**

Files:

- `backend/app/services/extract_agent.py`
- `backend/app/templates/HEARTBEAT.md`
- `backend/app/services/auto_dream.py`
- `backend/app/services/skill_distiller.py`
- relevant docs

Acceptance:

- Extractor is named as atom extraction, not promotion.
- Heartbeat is Memory Curator, not final skill/workflow writer.
- Dream is Reconsolidator + IdentityPromoter, not free identity editor.
- SkillDistiller consumes candidate evidence, not raw ungoverned patterns.
- Prompts output or reason about `container_candidate`.

Evidence:

- `CONTAINER_CANDIDATES` vocabulary frozen in `backend/app/memory/types.py`
  (`memory_append | soul_candidate | skill_candidate | workflow_candidate |
  artifact_only`) — shared by Extractor prompt, T2 metadata, and (P1)
  PromotionRouter.
- Extractor: role rewritten to ATOM EXTRACTION with `<container_candidate>`
  advisory-hint section; `_parse_extractions` parses `[container=...]` into
  `container_candidate` (invalid vocabulary dropped). Output examples carry
  container hints.
- T2 round-trip: `format_t2_entry(container_candidate=...)` emits
  `[container=...]`; `parse_t2_entry_line` restores it;
  `render_t2_snapshot` surfaces it to the heartbeat tick injection.
- HEARTBEAT.md: retitled Memory Curator; "not the final skill or workflow
  writer" boundary; container-candidate reasoning block in decision matrix;
  T3 lines preserve `[container=...]` markers (Example D).
- Dream: system prompt names Reconsolidator + IdentityPromoter, "not a free
  identity editor", decisions framed as lifecycle patch candidates.
- SkillDistiller: prompt consumes evidence-backed `skill_candidate` signals,
  "does not invent skills from raw ungoverned patterns".
- Tests: `backend/tests/services/test_container_candidate_contracts.py`
  (7 tests — prompt contracts + parse/round-trip/persist). Affected-area
  suite 252 passed (extract_agent, auto_dream, prompt_contracts,
  distillation_boundary_contracts, t2_store, heartbeat, skill_distiller).

### P1: PromotionRouter Pure Module

**Status: ✅ DONE (2026-06-04).**

Files:

- `backend/app/memory/promotion_router.py`
- `backend/tests/memory/test_promotion_router.py`

Acceptance:

- Same input cannot route to skill and workflow simultaneously.
- Every route has source refs, confidence, and reason.
- Runtime artifacts can be classified as artifact-only.

Evidence:

- Pure module (zero IO / zero LLM-client imports): `PromotionKind` (6 kinds
  per §6) + `PromotionSignal` → `fast_path_route()` deterministic pass →
  `route_promotion_signal()` with injected async `AdjudicatorFn`.
- Single-candidate invariant: route output is one `PromotionCandidate` or an
  escalation/hold — skill+workflow simultaneity impossible by construction.
- Anti-Mem0-V3 enforced: hint/rule conflicts, cross-container hints, and
  low-confidence durable promotions return `NEEDS_ADJUDICATION`; mechanical
  code never picks between soul/skill/workflow on semantic evidence.
- Fail-closed: adjudicator absent / raising / verdict outside
  `allowed_kinds` → `HELD` with audit reason (`no_adjudicator` /
  `adjudicator_error` / `verdict_out_of_bounds`); durable kinds without
  source_refs → `HELD` (`missing_source_refs`).
- Vocabulary shared with P0: `container_hint` validated against
  `CONTAINER_CANDIDATES` from `memory/types.py`.
- Tests: 22 passed (`backend/tests/memory/test_promotion_router.py`) —
  category rules, lifecycle edges, artifact-only, escalations, adjudication
  wiring incl. rogue-verdict and no-call-on-unambiguous.

### P2: Governed T3 Append API

**Status: ✅ DONE (2026-06-04).**

Files:

- `backend/app/memory/t3_store.py`
- tests under `backend/tests/memory/`

API shape:

```python
append_t3_memory_candidate(
    agent_id,
    category,
    content,
    source_refs,
    evidence,
    confidence,
    proposed_by="extractor|heartbeat|dream|manual",
)
```

Internal calls:

```text
prepare_memory_write
find_similar_t3_entries
append_t3_entry
record_active_memory_lifecycle
rebuild_index
sync_t3_to_hindsight(best-effort)
```

Acceptance:

- Heartbeat cannot bypass write gate for T3.
- Entries have ids and lifecycle records.
- Hindsight sync remains derived and best effort.

Evidence:

- `append_t3_memory_candidate()` implements the exact internal-call chain
  above (+ `container_candidate` and `proposed_by` stamped into entry
  metadata). Returns structured `T3AppendResult`
  (accepted/rejected/duplicate) — gate decisions are results, not errors.
- Single write path, no dual path: `save_memory` tool rewritten as a thin
  wrapper over the API (its previous inline gate→dedup→append chain
  removed); raw `write_file`/`edit_file` under `memory/` is REFUSED at the
  workspace layer (`_is_governed_memory_path`) with a save_memory hint —
  this covers heartbeat, dream, and ordinary sessions alike.
- HEARTBEAT.md curation switched from `read_file`+`write_file` to
  `save_memory(category=..., container_candidate=..., source_refs=...)`;
  format/entry-id/lifecycle stamping is owned by the runtime; dedup is
  tool-enforced (`[Skipped]` reply).
- `save_memory` gained `container_candidate` + `source_refs` parameters
  (promotion-lane evidence); tool description updated; `write_file`
  description no longer advertises `memory/knowledge.md` as a target.
- Hindsight sync added as sanctioned trigger #4 in
  `app/memory/hindsight_sync.py` docstring + caller allowlist test.
- Tests: `backend/tests/memory/test_t3_store.py` (8 — id/lifecycle/index,
  PL4 rejection, near-dup skip, container marker, hindsight failure
  non-fatal, write_file/edit_file refusal, non-memory paths still
  writable). Full backend suite 3656 passed.

### P3: Dream Lifecycle Patch

**Status: ✅ DONE (2026-06-04).**

Change Dream from direct line deletion to lifecycle patch.

Acceptance:

- Merge creates superseded edges.
- Contradiction creates contradiction or supersession edges.
- Cap cleanup archives / de-indexes entries instead of silent deletion.
- Hindsight only syncs active entries.

Evidence:

- New retirement APIs in `t3_store.py`: `retire_t3_entries()` (remove from
  active file → archive → rebuild index) and `archive_t3_lines()` (pure
  archival for callers that own the active-file rewrite). Retired lines
  land in `memory/archive.md` with `[from=][reason=][entry_id=][orig_date=]
  [superseded_by=]` metadata — reversible, MD-first evidence, never
  physical deletion.
- `lifecycle_store.MemoryLifecycleStore.mark_retired()` upserts terminal
  SUPERSEDED/ARCHIVED records (legacy lines without prior lifecycle rows
  still get an auditable edge).
- Dream `_apply_dream_decisions_unlocked`: t3_merges → supersede edges
  (`superseded_by=keep`); t3_contradictions → `contradiction_resolved`
  supersession toward the winning entry; direct line-deletion code removed
  (no dual path). Fixed a latent bug: a drop needle that is a substring of
  the kept canonical line no longer retires the keep line.
- `_consolidate_t3_files`: dedup drops archive as `dedup_superseded`, cap
  evictions archive as `cap_eviction`; preservation flags still sticky.
- De-index is structural: `archive.md` is not in `T3_FILE_SPECS` /
  `hindsight_sync._T3_FILES`, so manifest, INDEX.md, BM25 search, prompt
  injection, and Hindsight all see only active entries (pinned by test).
- Tests: `backend/tests/services/test_dream_lifecycle_patch.py` (5 —
  merge edges, contradiction edges, archival completeness on consolidation,
  archive out of active recall, hindsight active-files pin). Full backend
  3661 passed.

### P4: Skill / Workflow Candidate Lane

**Status: ✅ DONE (2026-06-04).**

Acceptance:

- Heartbeat only records skill/workflow candidate signals.
- SkillDistiller consumes `skill_candidate`.
- Workflow promotion consumes `workflow_candidate`.
- Promoted T3 strategy entries mark `promoted_to_skill` or `promoted_to_workflow`.

Evidence:

- Heartbeat lane closed both ways: the tool executor BLOCKS `save_skill`
  with a candidate-signal redirect, and the "Skill Candidate Opportunity"
  nudge + HEARTBEAT.md (default and `hr_agent_template`) instruct
  `save_memory(category="strategy", container_candidate="skill_candidate"
  | "workflow_candidate", source_refs=[...])` — direct creation guidance
  removed (no dual path).
- Consumption side in `skill_distiller.py`:
  `load_memory_skill_candidates()` / `load_memory_workflow_candidates()`
  read unpromoted `[container=...]` T3 entries via the manifest;
  `run_skill_distillation_cycle` feeds skill candidates into the LLM draft
  prompt as `memory_candidate_evidence` and the LLM names
  `consumed_memory_candidate_ids` (semantic attribution stays with the
  model; ids validated against the known pool).
- Workflow lane: `record_workflow_candidates_from_memory()` surfaces each
  `workflow_candidate` as an auditable evolution-ledger candidate
  (idempotent per entry_id) — automatic workflow approval stays deferred
  per §13; the promotion lane consumes evidence, not memory greps.
- Promotion marking: `md_store.mark_t3_entry_promoted()` stamps
  `[promoted_to=skill|workflow][promoted_target=...]` on the T3 line
  (evidence stays in T3, entry leaves the candidate pool); called after a
  successful skill save for every consumed candidate.
- Eval contracts updated with the spec (prompt_eval
  `heartbeat_skill_curation_consistency` + duplicate-guidance check now
  pin the candidate-lane wording; the old checks pinned `save_skill`
  guidance — the known "evals lock implementation details" trap).
- Tests: `backend/tests/services/test_candidate_lane.py` (7 — executor
  block + passthrough, template contracts, candidate readers, promoted
  marker leaves pool, workflow ledger recording idempotence). Full backend
  3668 passed.

### P5: Scene / Wiki Curator MVP

**Status: ✅ DONE (2026-06-04).**

This is not the full graph system. It is the first concrete home for the lifecycle stages named in section 4.

Files:

- `backend/app/memory/scene_curator.py`
- `backend/app/memory/wiki_curator.py`
- tests under `backend/tests/memory/`

Acceptance:

- Scene curator can update or hold a scene patch using existing T3 entries and source refs.
- Wiki curator can produce a Markdown concept page patch with claim, scope, evidence, contradictions, changes, and retrieval tags.
- Both curators emit candidates first; governed write APIs apply accepted patches.
- Ambiguous scene merge or wiki claim conflict becomes a held candidate with audit reason.
- No graph database, KG, or PPR is required for this phase.

Evidence:

- `scene_curator.py`: `curate_scene(atoms, llm)` → `ScenePatchCandidate`
  (proposed/held). LLM (injected async callable) owns update-vs-create-vs-
  hold with anti-proliferation prompt (prefer update, capacity pressure,
  evolution trail via `## Changes`); mechanical layer supplies similar-scene
  retrieval (jaccard), capacity signal, frontmatter/schema validation.
  `apply_scene_patch` is the governed write under `memory/scenes/` with a
  privacy gate (PL4 patch refused, never written).
- `wiki_curator.py`: `curate_wiki_page(concept, evidence, llm)` →
  `WikiPatchCandidate`; six-section page contract (Current Claim / Scope /
  Evidence / Contradictions / Changes / Retrieval Tags) validated
  mechanically; claim-safety prompt (weak contradiction → Contradictions
  section, never silent claim rewrite) + confidence floor 0.5 holds weak
  claim upserts. `apply_wiki_patch` governed write under `memory/wiki/`.
- Every hold/refusal/rejection writes `memory/distillation_audit.jsonl`
  (new `distillation_audit.py` sidecar — spec §3): no LLM, invalid output,
  explicit LLM hold, missing sections, low confidence, privacy rejection.
- Live entry point (no orphan): `services/memory_curation.py`
  `run_scene_wiki_curation_tick` wired into the heartbeat tick — cursor-
  gated (`memory/.curation_cursor.json`, ≥3 new knowledge/strategy entries,
  batch 8), tenant summary-model wrapped as the injected LLM, dominant
  `concept` drives the wiki pass, never breaks the tick.
- Raw writes to `memory/scenes/` and `memory/wiki/` were already refused at
  the workspace layer by P2's `memory/` guard — curator apply functions are
  the only write path.
- Tests: `backend/tests/memory/test_scene_wiki_curators.py` (16 — create/
  update/hold paths, audit records, schema holds, confidence floor, governed
  apply incl. PL4 rejection and held refusal, runtime tick skip/run/cursor/
  never-raises). Full backend 3684 passed.

### P6: Navigation And Access Telemetry

**Status: ✅ DONE (2026-06-04).**

Acceptance:

- `memory/INDEX.md` or manifest has consumer in prompt assembly.
- Entry-level `recall_count` and `last_recalled_at` are updated.
- Heat drives navigation order and retirement candidates.
- Activated memory includes activation reasons.

Evidence:

- Manifest consumer: new `prompt_sections/memory_navigation.py` renders the
  §8 navigation table (id / file / category / heat / recall_count /
  last_recalled / preview + `load_memory(ids=[...])` instruction) from
  `build_t3_entry_manifest()`; wired through `build_runtime_prompt` →
  `build_dynamic_prompt_suffix(memory_navigation=...)` as its OWN section
  (never inside soul — §8 boundary).
- Recall telemetry: per-entry counters were already wired end-to-end
  (engine field names `access_count` / `last_accessed`, stamped by the
  write gate and bumped on every activation by
  `retriever._apply_activation` → `access_log.bump_access`); the
  navigation/read layer exposes them under the spec names recall_count /
  last_recalled.
- Heat: `md_store.compute_entry_heat()` (access_count + recency bonus —
  engineering score per §1) orders the navigation table descending and
  ranks `md_store.list_retirement_candidates()` ascending (excludes
  promoted entries and preservation-flag matches).
- Retirement evidence reaches the Reconsolidator: dream's consolidation
  prompt now carries a `<low_heat_retirement_candidates>` block (mechanical
  ranking, LLM decision, preservation flags excluded mechanically).
- Activation reasons: already rendered into the prompt as `[why=...]` by
  `assembler._activation_suffix` from `activation_reasons` metadata
  (pre-existing; verified in the audit).
- Tests: `backend/tests/memory/test_navigation_telemetry.py` (7 — heat
  ordering, navigation rendering/ordering/empty, dynamic-suffix consumer,
  retirement ranking + protection, dream prompt block). Full backend 3691
  passed.

### P7: Knowledge Read Model API

**Status: ✅ DONE (2026-06-04).**

Acceptance:

- Frontend no longer parses raw file layout for primary view.
- Overview, entries, timeline, and candidates are structured.
- Raw Markdown remains available as advanced view.

Evidence:

- `services/knowledge_read_model.py` — pure read side (zero writes, zero
  LLM): overview (§11 `AgentKnowledgeOverview` shape: identity/memory
  counters incl. lifecycle superseded/archived + sensitiveSuppressed,
  distiller statuses from state-file traces, linkedCapabilities incl.
  skill/workflow candidate counts), pages (wiki + scenes with frontmatter),
  page detail (markdown + frontmatter, slug-validated against traversal),
  entries (heat-ordered with recallCount/lastRecalledAt/container/
  promoted_to/sensitivity), events (distillation_audit.jsonl + dream
  history merged, newest first), candidates (skill / workflow / soul-hold
  from evolution_ledger.jsonl + held curations).
- `api/agent_knowledge.py` — the six §11 GET endpoints under
  `/api/agents/{agent_id}/knowledge/*`, each guarded by
  `check_agent_access` (multi-tenant invariant); registered in `main.py`
  under both `/api` and `/api/v1`.
- Raw Markdown advanced view unchanged: workspace file APIs still serve
  `soul.md` / `memory/` / `evolution/` verbatim.
- Tests: `backend/tests/services/test_knowledge_read_model.py` (8 —
  overview structure, pages, page detail + traversal rejection, entries
  telemetry, events merge/order, candidate lanes, empty workspace, router
  route-set pin). Full backend 3699 passed.

### P8: AgentDetail IA

**Status: ✅ DONE (2026-06-04).**

Acceptance:

- Knowledge is separate from Skills, MCP, and Workflows.
- `skills` remains a standalone module.
- `tools` is renamed or split to MCP.
- `workflows` remains standalone.
- Knowledge only deep-links to capability modules.

Evidence:

- New `AgentKnowledgeSection.tsx` — the Knowledge plane over the P7 read
  model with six subviews: Overview (identity/memory/distillers/linked-
  capabilities cards + held curations), Pages (wiki + scenes with markdown
  rendering), Entries (heat/recall/lane table), Candidates, Timeline, and
  Raw (the advanced view — reuses the former Mind file browser, so raw
  Markdown stays reachable but is no longer the primary view; default view
  is Overview per §10.3).
- `AgentDetail.tsx`: the raw-file `mind` tab is REPLACED by `knowledge`
  (no dual path; legacy `#mind` deep links map to `#knowledge`); tab
  groups updated.
- `tools` tab renamed to **MCP** in both locales (en + zh tooltips
  describe external connectors/policy); ToolsManager content was already
  MCP-only from the extension-surface work.
- Deep-link only: skill candidates → Skills tab, workflow candidates →
  Workflows tab via `onNavigateTab` — Knowledge never installs/edits
  capabilities. `skills` and `workflows` tabs untouched.
- Typed adapter `api/domains/knowledge.ts` for all six endpoints.
- i18n: `agent.tabs.knowledge(+Tooltip)` added, `mind` keys removed from
  tabs (section copy reused by Raw view retained), en+zh in sync.
- Tests: `AgentKnowledgeSection.test.tsx` (2 — read-model rendering with
  default-not-file-browser pin, subview surface incl. Raw). Frontend 154
  tests passed, `tsc --noEmit` clean, production build passed.

### P9: Advanced Graph / KG / PPR

**Status: ✅ DONE (2026-06-05; owner explicitly lifted the §13 deferral).**

Only after P0-P8:

- Derived relation graph from Markdown.
- `[[wikilink]]` graph navigation.
- Optional KG + PPR retrieval experiment.
- Memory eval benchmark for retrieval quality and retirement safety.

Evidence:

- Design judgments (pinned before construction): the KG **is** the wikilink
  network the P5 curators (LLM) already author — no second LLM
  triple-extraction pipeline, so no second truth source (§0);
  `relation_graph.py` does deterministic syntax parsing only (§3.1
  `## Relations` typed edges + inline `[[wikilinks]]`, forward references
  kept as exists=False nodes); the graph is rebuilt from Markdown on every
  call — zero persisted derived state (pinned by test); no standalone
  `/graph` API endpoint (would be an orphan) — navigation ships as two real
  consumers instead.
- `relation_graph.py`: `build_relation_graph()` over `memory/wiki` +
  `memory/scenes`, `links_for()` navigation rows, and
  `personalized_pagerank()` — pure-Python power iteration, dangling-mass
  restart, zero new dependencies.
- `wiki_retrieval.py`: `search_wiki_pages(method="bm25"|"ppr")` — BM25
  (reusing md_store's tokenizer/scorer) seeds the PPR personalization
  vector; blended ranking keeps direct hits ahead of equally-connected
  neighbors; only `status=active` pages retrievable (§4.9 de-index);
  every hit carries `source_ref` back to its Markdown path (§1.3).
- `retrieval_eval.py` — the benchmark decides the default, not taste:
  fixed 6-page ops-wiki corpus, 7 cases (4 direct + 3 multi-hop).
  **Measured: PPR recall@3 = 1.0 / MRR 0.738 vs BM25 0.714 / 0.619;
  multi-hop slice PPR 1.0 vs BM25 0.333; direct slice both rank@1.**
  → `DEFAULT_WIKI_METHOD = "ppr"`, and the eval pins
  `multi_hop.ppr_recall >= bm25_recall` so a regression flips the test.
  `evaluate_retirement_safety()`: 5 checks all passing — protected /
  promoted entries never become candidates, cold ranks before hot,
  cap-eviction archives every removed line (lifecycle `archived` records),
  protected + hot entries survive the pass.
- Consumers (no orphans): `search_memory` tool appends a "Knowledge Pages"
  section (PPR-ranked, `read_file` hint to the Markdown source);
  `get_knowledge_page` read model returns `links.outgoing/incoming`; the
  frontend Knowledge → Pages detail renders clickable linked-page chips
  (forward references shown as not-yet-created, unclickable).
- Tests: `backend/tests/memory/test_graph_ppr_eval.py` (13 — typed/inline
  edge parsing, forward references, zero-persistence pin, links_for both
  directions, PPR multi-hop + empty-input, BM25 direct hit, PPR reaches a
  page BM25 provably cannot, active-only filter, empty corpus, retrieval
  eval report shape + multi-hop dominance, retirement safety, page-detail
  links). Full backend 3712 passed; frontend 154 passed / tsc clean /
  build passed.

### P10: Review Closure — Runtime Boundary Hardening

**Status: ✅ DONE (2026-06-05; post-implementation full review).**

This phase records the issues found after comparing the spec against the
implemented code. It does not add a new container; it closes gaps where P6-P9
were correct structurally but not yet consistently enforced at every runtime
entry point.

Findings fixed:

- **Sensitive memory could bypass activation policy.** `Memory Navigation`,
  `load_memory`, and `search_memory` could expose PL3 entries without a
  resolved `PrincipalStack`. Fixed by adding shared read-visibility helpers
  and making all three consumers default to PL1/PL2 only; PL3 requires direct
  owner or company-admin principal, PL4 remains unreadable.
- **Knowledge Read Model was structured but not privacy-governed.** Entry
  lists, page lists, and page detail now apply the same principal-aware
  sensitivity boundary. Unauthorized PL3 pages redact body/frontmatter and
  omit relation links; page lists skip inaccessible pages instead of leaking
  titles.
- **Raw file APIs still formed a side door into `memory/`.** Generic
  workspace file APIs now refuse all raw writes/deletes under `memory/`, block
  `use` access from raw listing/reading/downloading memory files, and leave
  governed read access to the Knowledge API. Frontend Raw view is manage-only;
  its memory browser is read-only.
- **Curator apply trusted candidate paths.** Scene/wiki apply functions now
  resolve candidate paths under flat `memory/scenes/<slug>.md` and
  `memory/wiki/<slug>.md` only; traversal attempts are refused and audited.
- **Curation cursor treated infrastructure holds as terminal.** Missing LLM,
  invalid LLM output, missing scene frontmatter, and missing wiki required
  sections are retryable holds, so the cursor is not advanced. Semantic holds
  remain terminal.
- **Kernel prompt path had navigation only in the legacy builder.** The real
  `AgentKernel.handle()` path now resolves Memory Navigation through a
  dependency and passes it into every dynamic suffix rebuild, including prompt
  cache hits and prompt-too-long retries. Navigation stays outside `soul.md`
  and outside the frozen prefix.

Evidence:

- Backend red→green suite:
  `backend/tests/memory/test_navigation_telemetry.py`,
  `backend/tests/services/test_knowledge_read_model.py`,
  `backend/tests/tools/test_memory_handler.py`,
  `backend/tests/api/test_security_regressions.py`,
  `backend/tests/kernel/test_prompt_cache_integration.py`,
  `backend/tests/memory/test_scene_wiki_curators.py` — 69 passed.
- Frontend IA regression:
  `npm run test -- AgentKnowledgeSection AgentDetailSections` — 38 passed.
- Broader memory-phase regression:
  P1-P10 focused backend suite — 131 passed; frontend production build
  (`npm run build`) passed.

Independent review closure (main-agent verification, 2026-06-05):

- Full backend suite green at commit time: **3750 passed, 7 skipped**
  (`pytest tests/ -x -q`, 49s); ruff check clean; all five touched files
  format-clean.
- **Incident recorded:** the DR-6b commit (`8c6627be`) accidentally carried
  half of P10 (`knowledge_read_model.py`, `prompt_sections/memory_navigation.py`,
  `tools/handlers/memory.py` visibility imports) but missed the new
  `app/memory/visibility.py` file — leaving HEAD with lazy imports of a module
  that did not exist in git. Window was local-only (never pushed); closed by
  this commit adding `visibility.py`.
- Boundary notes verified during review: `overview`/`events`/`candidates`
  endpoints intentionally skip `principal_stack` — they expose only counts and
  curation-audit metadata, never memory body text (convention: audit writers
  must not embed body text in `detail`). `platform_admin` maps to
  `CURRENT_USER` role in the read-model principal stack, so PL3 stays
  company-domain (manage-level raw file API remains the operator escape
  hatch). Upload endpoint already whitelists `workspace/`+`skills/` so the
  `memory/` write guard cannot be bypassed. Legacy `build_runtime_prompt`
  remains production-orphaned (tests only) — navigation now lives on the
  kernel path; orphan cleanup deferred (out of P10 scope).

## 13. Immediate Decisions

Adopt now:

1. Brain science is inspiration, not a truth claim for thresholds.
2. Distillers stay; authority is reduced.
3. Memory Control Plane is horizontal governance.
4. Retirement means de-index first, not physical deletion.
5. Skill / MCP / Workflow remain independent modules.
6. Knowledge UI is a wiki / inspector over evidence, not a capability manager.
7. Index must have a runtime consumer.
8. Hindsight remains optional read-side accelerator.

Defer:

1. Merging `knowledge.md` and `strategies.md`.
2. ~~Full KG/PPR retrieval.~~ — deferral lifted by the owner 2026-06-05;
   implemented as P9 (wikilink-network KG + PPR, benchmark-validated).
3. Automatic skill/workflow approval.
4. Physical deletion of retired memory.
5. Frontend visual redesign before read model exists.

## 14. Source Notes

Primary local research inputs:

- `docs/agent-memory-research.md`
- `docs/agent-memory-md-first-research-codex.md`
- `docs/knowledge-container-boundaries.md`

Important code surfaces:

- `backend/app/services/extract_agent.py`
- `backend/app/services/heartbeat.py`
- `backend/app/templates/HEARTBEAT.md`
- `backend/app/services/auto_dream.py`
- `backend/app/services/skill_distiller.py`
- `backend/app/memory/write_gate.py`
- `backend/app/memory/md_store.py`
- `backend/app/memory/retriever.py`
- `backend/app/memory/hindsight_sync.py`
- `frontend/src/pages/AgentDetail.tsx`
- `frontend/src/pages/agent-detail/AgentSkillsSection.tsx`
- `frontend/src/pages/agent-detail/ToolsManager.tsx`
