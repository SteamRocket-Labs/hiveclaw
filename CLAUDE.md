# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## North Star — Highest-Priority Goal (overrides all other guidance)

Hive exists to be **two things, and every line of code must serve one of them**:

1. **A self-evolving agent infrastructure with enterprise-grade access control** — digital employees that genuinely improve over time (memory, reflection, skill acquisition, soul evolution) while every capability, memory write, and external action stays permission-governed and auditable.
2. **A control plane (控制中台)** for operating those agents at company scale — org/permission management, governance, budgeting, coordination, and observability.

**Quality bar:** the per-agent intelligence and self-evolution must be **at least as good as `hermes-agent`** (internal benchmark at `/Users/rocky243/vc-saas/hermes-agent`) — not merely architecturally grander. A system that *feels* weaker than a lean benchmark agent is a failure of Goal 1, not a success.

**Build order:** Goal 1 (the agent's own intelligence + self-evolution) is the **foundational cornerstone** — it is hardened and judged *first*; the control-plane and agent-to-agent layers build on top of it. When a trade-off is unclear, resolve it in favor of these two goals. Current SOTA target entry: `docs/hive-sota-master-goal.md`; foundation roadmap: `docs/self-evolution-sota-plan.md`.

## Reference Baselines — 对照物顺序

Hive is a **Cloud Code Python evolution**, so implementation comparisons must use the current local source baselines in this order:

1. **FreeCode TS runnable baseline**: `/Users/rocky243/vc-saas/free-code-main` — first reference for answering "what is the essential CC runtime semantics?"
2. **claw-code Python port**: `/Users/rocky243/Context Engineering/claw-code/src` — only for Python-port direction and existing port boundaries; not a full parity baseline.
3. **claw-code Rust runtime**: `/Users/rocky243/Context Engineering/claw-code/rust` — reference for session hygiene, workspace partition, JSONL rotation, resume/fork/compact.
4. **claude-code-org TS source**: `/Users/rocky243/Context Engineering/claude-code-org` — cross-check against FreeCode.
5. **Codex Rust**: `/Users/rocky243/Context Engineering/codex/codex-rs` — Codex delta only; it must not override the CC baseline.

If sources conflict, judge CC parity from FreeCode first, use `claude-code-org` only as cross-check, use `claw-code/rust` for low-level runtime/session lessons, and use Codex Rust only for additive deltas that do not conflict with CC semantics.

## Full Lifecycle Parity — 全生命周期对标

“全面对标” means **agent full-lifecycle parity**, not feature-count parity. Compare the whole agent lifecycle: definition, context assembly, accepted user prompt, transcript write, model loop, tool loop, hook boundaries, compaction, stop, resume, subagent, workflow, skill loading, and session close.

The deliberate non-parity delta is Hive's **Memory / Iter self-evolution system**. Memory stays Hive-native: T0/T2/T3/soul, governed write surfaces, Skill evolution, and Iter are not reduced to CC behavior. Everything else must have an explicit CC/Hive mapping before being called aligned.

Context composition must be mapped one-to-one:

- CC `CLAUDE.md` / project instructions → Hive `soul.md` and governed identity/context sections.
- CC skill progressive disclosure → Hive `Skill` capsule plus `load_skill` / `tool_search` split.
- CC task/todo scaffolding → Hive Work Ledger / Progress Ledger.
- CC transcript/session artifacts → Hive T0 raw evidence plus product read models.
- CC hooks/session boundaries → Hive hook events with equivalent blocking/resume semantics.

Current session-middle parity priorities are **Skill, Sub-agent, Workflow, and Hooks**. Their prompts, tool descriptions, and lifecycle events must be source-checked against FreeCode first and kept vendor-neutral. Anthropic/Claude/Codex names may appear as documented baselines, but runtime prompts must not privilege a model vendor or product identity.

## AI-Native Design Law (最高设计法律 — judges every architectural decision)

Hive is an **AI-native system**. Three layers, in strict priority order:

1. **L1 — Unleash the model first.** Any step that requires intelligence (summarization, planning, extraction, synthesis, judgment) belongs to the LLM at full capability: complete input visibility (no mechanical pruning of what the model sees), sufficient output budget (no starved `max_tokens`), prompts engineered to benchmark (Claude Code) quality — structure, examples, anti-drift constraints. Mechanical/string-based handling of intelligent steps is allowed ONLY as an observable fallback on failure paths, never the primary path. *Case law: compaction once fed the LLM a `[-40:]` truncated slice with a 2500-token output cap and a silent regex fallback — the canonical violation (fixed in `docs/compaction-cc-alignment.md`).*
2. **L2 — Harness constrains, never replaces.** Governance, safety, budgets, audit wrap *above* model capability: they bound what the agent may **do**, not how well it **thinks**. A constraint that degrades model intelligence (instead of scoping authority) is a design bug.
3. **L3 — Hive's identity: a neutral, organization-facing control plane.** Hive is an independent third party with **model equality**: every feature, every constraint, every prompt works equally for every model — no privileged vendor, no model-specific feature gates, no prompt favoritism. On top of that equality Hive adds what no model vendor provides: the company-scoped controllable agent control plane.

**Memory / self-evolution boundary law:** LLM 负责判断、提炼、反思、归纳、候选生成；平台负责证据引用、权限、去重、回滚、审计、最终落盘。Any memory, heartbeat, dream, skill, workflow, or evolution path that replaces model judgment with counters, regexes, truncated summaries, or platform-authored "semantic" text is an AI-native violation. Any path that lets the model bypass governed write surfaces for durable memory/evolution/soul files is a governance violation.

**Memory target form:** Hive memory is an Agent Markdown Wiki / Learning Vault: T0 raw evidence, T2 tagged Markdown Segment Packages under `memory/t2/sessions/**`, a converged T3 semantic layer (`memory/t3/episodes.md`, `user.md`, `worker.md`, `capabilities.md`), source_refs-backed residual evidence verification, and `soul.md`. Skill is a progressive capability capsule grown from T3 capability evidence and eval-backed candidate packages, not a T3 page. `relations`, `contradictions`, graph/vector/search/UI views are derived and rebuildable; no external memory provider may become the T3 source of truth. `memory/indexes/wiki_map.md` is the single generated navigation map, not always-on prompt memory; control sidecars live under `memory/control/`. Current path contract: `docs/memory-vault-path-contract-2026-06-23.md`.

**T0 session truth:** JSONL is the mechanical truth and Markdown is the deterministic projection. Per-segment `memory/t0/sessions/<session_id>/segments/<segment_id>/events.jsonl` is the resume/replay/fork/checkpoint/rollback source of truth; same-segment `source.md` is the human/LLM-readable Markdown/XML projection and legacy fallback, not the mechanical truth.

**Review lens — apply to every subsystem:** ① Is the LLM's input visibility complete? ② Is its output budget sufficient? ③ Is the prompt engineered to benchmark quality? ④ Does mechanical processing appear only as an observable fallback?

## Delivery Discipline — One Complete Pass, No MVP (交付纪律 — 一次改完，零技术债)

**Owner law (2026-06-08, "必须记住"): any revision/rework round ships as ONE complete pass — no MVP, no phased "first implementation," no technical debt deferred.** Before starting a change, define the *complete* scope up front (tests, edge cases, error paths, schema migration, **legacy-data backfill**, production cleanup, observability) and deliver it in one pass. Forbidden: "ship Phase 0 first," default-off flags hiding half-built work, "add tests later," "skip the migration for now."

*Case law: the agent memory system rotted into dirty, drifting files precisely because P0–P10 took the spec-sanctioned "first implementation can encode inline rather than rewriting every existing bullet" shortcut and never paid it down — accumulating ten debts D1–D10 (`docs/agent-memory-purity-spec.md`). MVP's "later" = never.*

**Only exception:** a genuinely irreversible step (production data migration/deletion) uses a dry-run + confirmation gate — that is a safety gate, not an MVP stage; completeness never waives safety.

## Project Overview

Hive is an open-source **multi-agent collaboration platform** — enterprise "digital employees" with persistent identity, long-term memory, private workspaces, autonomous trigger-driven execution, governed self-evolution, durable web chat runs, Office workbench editing, and owner/company-aware Memory Gate + Platform Gate governance. Built with FastAPI (Python) backend + React 19 (TypeScript) frontend.

**Version:** tracked in `backend/VERSION` and `frontend/VERSION` (currently 1.7.0).

## Current Engineering Baseline (2026-06-15)

Before making architecture claims, use the current evidence surface:

- `docs/hive-sota-master-goal.md` — canonical SOTA total goal, target matrix, and future loop-comparison ledger.
- `docs/harness-engineering-audit-2026-06-11.md` — harness audit, remediation log, and verification evidence.
- `docs/round2-sota-benchmark-2026.md` — second-round SOTA benchmark, detailed comparison sources, and milestone evidence.
- `docs/memory-clean-loop-refactor-plan-2026-06-17.md` — current memory clean-loop redesign and Agent Markdown Wiki / Learning Vault target.
- `docs/memory-system-flow-map-2026-06-17.md` — end-to-end memory flow map, including source_refs-backed residual evidence verification, T3 semantic layer, and capability candidate lanes.
- `docs/memory-vault-path-contract-2026-06-23.md` — current single-agent memory filesystem contract: canonical T0/T2/T3/session_state/index/control paths plus legacy import quarantine semantics.
- `docs/agent-memory-md-first-spec.md` — MD-first memory truth-source contract and lifecycle spec.
- `docs/self-evolution-sota-plan.md` — canonical self-evolution foundation and completed substrate baseline.
- `docs/agent-memory-purity-spec.md` — memory purity, lifecycle, and hygiene contract.

Current closures that must not regress:

- Durable self-evolution promotion requires evidence, verification/eval, rollback metadata, and audit records.
- Web chat and long tasks are `RuntimeTask` backed and restart-resumable; browser disconnects are subscription changes, not cancellation.
- Plan Mode is a first-class confirmation/planning boundary: substantive plan content must be agent-authored, clarification must be first-class, and unconfirmed autonomous/high-risk work must not execute.
- Workflow is a first-class deterministic orchestration substrate parallel to Plan Mode: `RuntimeTask(task_type="workflow")`, workflow step/leaf journals, run quotas, gate/wait/resume, trigger integration, admin ops, and Deep Research workflow-native execution must remain governed and auditable.
- Subagent/delegation is a first-class collaboration capability: lightweight workers, peer delegation, fanout, context isolation, result distillation, governed shared tool execution, and replay-safe resume boundaries must remain distinct from Workflow control flow.
- Agent TodoList / Work Ledger / Progress Ledger is the CC Task/Todo-equivalent agent-authored task board: `track_todo` records todos/dependencies, `record_finding` records findings/failures/replan, and `read_ledger` restores state. Writing a todo is cognitive bookkeeping; it must not start execution.
- Skill is a progressive-disclosure capability capsule, not merely a Markdown prompt. A Skill may package instructions, references, templates, scripts, evals, workflow definitions, and subagent definitions; loading a Skill adds context/guidance only. Executable components still run through their governed runtime (`preview_workflow`/`start_workflow`, `spawn_subagent`/`delegate_to_agent`, or approved sandbox/code execution).
- Memory/Self-Evolution target form is Agent Markdown Wiki / Learning Vault: T0 -> T2 -> T3 -> `soul.md` is the durable gradient; residual verification means T3 curation follows T2 `source_refs` back to T0 evidence; Skill may grow from memory evidence, while Workflow remains a separate execution-control system that memory can only reference or hand off evidence to.
- `invocation_spans` is the canonical PostgreSQL trace surface; JSONL spans are compatibility artifacts.
- Provider retry/overload fallback, CJK-aware token estimates, canonical assistant-turn prompt-cache anchors, output-cap telemetry, and Anthropic thinking-signature preservation are runtime contracts.
- Agent-controlled code execution is provider based: local/trusted hosts use the shared OS sandbox builder (`bubblewrap` or `sandbox-exec`), while Railway production uses `HIVE_CODE_EXEC_PROVIDER=vercel_sandbox` and Vercel Sandbox credentials. Never fall back to raw subprocesses.
- MCP authz forbids URL userinfo/token passthrough; A2A Agent Cards and `/interoperability/profile` must mark unsupported OAuth/JSON-RPC surfaces as `not_exposed`.
- Memory hygiene startup repair retires legacy shadow stores and quarantines dead stubs through a reversible shared path.
- Latest full backend evidence before the current documentation-only update: `cd backend && source .venv/bin/activate && pytest tests -q` -> `4223 passed, 7 skipped, 4 warnings`.

## Development Commands

### First-Time Setup
```bash
bash setup.sh           # Production: env, PostgreSQL, backend venv, frontend npm, DB seed
bash setup.sh --dev     # Also installs pytest, ruff, and dev tools
```

### Start/Stop Services
```bash
bash restart.sh         # Stops old processes, starts backend(:8008) + frontend(:3008)
```

### Backend (cd backend/)
```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8008 --reload  # Dev server

ruff check app/ --fix && ruff format app/   # Lint + format

pip install -e ".[dev]"
pytest                                       # All tests
pytest tests/test_foo.py -v                  # Single file
pytest tests/test_foo.py::test_bar -v        # Single case

alembic upgrade head                         # Apply migrations
alembic revision --autogenerate -m "desc"    # New migration
alembic heads                                # Must be single head
python -m app.scripts.repair_memory_hygiene  # Dry-run memory hygiene
python -m app.scripts.repair_memory_hygiene --apply --confirm
```

### Frontend (cd frontend/)
```bash
npm run dev              # Vite dev server on :3008 (proxies /api→:8008, /ws→ws://:8008)
npm run build            # tsc + vite build → dist/
```

### Docker
```bash
cp .env.example .env
docker compose up -d --build    # Full stack → :3008
```

## Architecture

```
Frontend (React 19 + Vite + TanStack Query)
    ↓ /api proxy (:3008 → :8008)
Backend (FastAPI + SQLAlchemy async)
    ↓
PostgreSQL (asyncpg) + Redis
```

### Agent Kernel — The Core Runtime

All agent execution flows through a unified kernel. This is the most important architectural layer.

```
Entry Points (web chat RuntimeTask, Feishu, Slack, DingTalk, WeChat, Teams, Trigger, Heartbeat, Delegation)
    ↓
runtime/invoker.py — invoke_agent() resolves deps, builds prompt, calls kernel
    ↓
kernel/engine.py — AgentKernel.handle() — stateless LLM loop, zero DB deps
    ↓ (injected callbacks via KernelDependencies)
tools/service.py — ToolRuntimeService.execute() — governed tool execution
    ↓
tools/governance.py — security zone → capability gate → approval flow
    ↓
tools/executors/ — core.py, extended.py, integrations.py
```

**Key files:**

| File | Purpose |
|------|---------|
| `kernel/contracts.py` | `InvocationRequest`, `InvocationResult`, `RuntimeConfig` — pure dataclasses |
| `kernel/engine.py` | `AgentKernel` — stateless LLM loop with DI. Context compaction, token budgeting, vision support |
| `runtime/invoker.py` | `invoke_agent()` — wires kernel to platform (DB, tools, memory, prompt). Single entry for ALL paths |
| `runtime/prompt_builder.py` | Assembles system prompt: agent context → knowledge → memory → active runtime tool groups → skill catalog |
| `runtime/session.py` | `SessionContext` — tracks source, channel, active runtime tool groups per invocation |
| `core/execution_context.py` | `ExecutionIdentity` ContextVar — agent_bot vs delegated_user, read by audit |

**Execution flow:** Every entry point builds an `InvocationRequest` and calls `invoke_agent()`. The kernel runs a multi-round LLM loop with streaming callbacks. Round budget: `max_tool_rounds` defaults to **200**; heartbeat overrides to **40**. Round-pressure warnings are injected at 80% and with 2 rounds remaining. Context compaction is **proactive** (≥75% utilization, checked every 3 rounds) + **reactive** (prompt-too-long retries with truncation). Individual tool results >50KB spill to `workspace/logs/.../artifacts/`; per-round aggregate budget is 200K chars. Semantic loop detection is wired via `LoopGuard` over assistant text, tool calls, and tool results; the round cap is the backstop. Invocation, generation, and tool spans are persisted through `record_invocation_span`, and provider behavior is wrapped by retry/overload fallback, output-cap telemetry, prompt-cache anchor stability, and Anthropic thinking-signature preservation.

### Tool System (`app/tools/`)

Tools follow a registry + executor + governance pattern:

| File | Purpose |
|------|---------|
| `runtime.py` | `ToolExecutionRegistry` — name → executor mapping, `try_execute()` |
| `service.py` | `ToolRuntimeService` — wraps governance + execution + timeout + logging |
| `governance.py` | `run_tool_governance()` — 2-layer preflight: security zone → capability gate |
| `governance_resolver.py` | Connects governance to real DB (security_zone, capability policies, approval) |
| `packs.py` | `ToolPackSpec` — static capability bundles (web, feishu, email, etc.) |
| `handlers/` | 18 handler files: filesystem, search, communication, email, feishu, plaza, skills, triggers, hr, mcp, office, memory, plan_mode, subagent, tasks, work_ledger, workflow, deep_research |
| `workspace.py` | `ensure_workspace()` — bootstraps agent filesystem (soul.md, memory/, skills/, workspace/) |

**100+ registered built-in tool definitions** across categories: file I/O, web search/fetch, Feishu office (docs/wiki/sheets/base/tasks/calendar), OfficeCLI/ONLYOFFICE document workflows, email, messaging, Agent Circle/plaza, triggers, skills, deep research, workflows, work ledger, MCP.

### Skill System (`app/skills/`)

Progressive-disclosure capability capsules. `SkillParser` → `WorkspaceSkillLoader` → `SkillRegistry`. Skills load progressively: catalog metadata in prompt, full body via `load_skill`. A folder-based Skill may carry instructions, references, templates, scripts, evals, workflow definitions, and subagent definitions. Loading a Skill does not execute side effects or unlock schemas; workflows still run through `preview_workflow`/`start_workflow`, subagents through `spawn_subagent`/`delegate_to_agent`, scripts through the approved sandbox/code execution path, and missing schemas through `tool_search`.

### Memory System — 4-Layer MD Pyramid + Memory Gate / Platform Gate

MD files are the source of truth; the legacy SQLite/JSON shadow stores are retired and repaired through `memory/hygiene.py`.

```
T0 append-only session ledger
  → T2 Segment Package (summary.md / labels.md / review.md / manifest.json)
  → T3 Consolidation Batch (LLM pitch + Memory Gate review + Platform Gate commit)
  → accepted T3 Markdown (episodes.md / user.md / worker.md / capabilities.md)
  → soul.md (Dream soul reconsolidation)

Explicit user-commanded memories enter memory/explicit/** immediately, then may
be absorbed by the same T3 Consolidation Batch. They are not accepted T3 until
the gate commits an LLM-authored T3 block.
```

**Cadence configuration (P1-W2-5, current)**: the `evolution_daemon` dispatcher
ticks every `HEARTBEAT_TICK_SECONDS` (default 60s). Runnable agents are eligible
on the platform-managed `HEARTBEAT_DEFAULT_INTERVAL_MINUTES` cadence (default
120 minutes), not per-agent UI cadence. Subsequent heartbeat ticks skip when
there are no new T2 entries. Full Dream is gated by `MIN_HOURS_BETWEEN_DREAMS`
(24h) plus either `MIN_SESSIONS_SINCE_DREAM` (3 sessions) or
`MIN_HEARTBEAT_TICKS_SINCE_DREAM` (2 productive heartbeat ticks). Dream handles
soul-level reconsolidation; it does not rewrite accepted T3 files.

**T0 session layout (Claude Code transcript / Codex rollout aligned):**

```
memory/t0/sessions/<chat_session_id>/
  index.json
  segments/
    <segment_id>/
      events.jsonl ← append-only mechanical truth records with hash chain
      source.md    ← deterministic Markdown/XML projection: user_message, assistant_message, tool_result, segment_boundary
```

`logs/YYYY-MM-DD/**` is now legacy/import compatibility only. Old chat logs can be imported, but runtime chat, one-off task, trigger, delegation, heartbeat, and dream T0 truth is the append-only session ledger.

**T2 Segment Package layout:**

```
memory/t2/sessions/<session_id>/segments/<t2_segment_id>/
  summary.md      ← LLM-authored session/segment summary with XML blocks
  labels.md       ← LLM-authored event labels + quantified engineering labels
  review.md       ← independent Memory Gate review/rubric output
  manifest.json   ← platform evidence refs, source bundle refs, revisions, audit metadata

memory/t2/sessions/<session_id>/episodes/<episode_id>/
  synthesis.md    ← LLM-authored stitched episode for broken/continuing segments
  review.md       ← independent Memory Gate review/rubric output
  manifest.json   ← platform evidence refs, source package refs, revisions, audit metadata
```

`memory/learnings/*.md` is a legacy migration/audit surface only and must not be
used as prompt semantic memory. New T0→T2 semantic truth is the Segment Package
above. Build-time refs may exist under
`memory/.staging/t2_jobs/<job_id>/source_bundle.json`; after commit, evidence
pointers live in `manifest.json` and in-file `source_refs`.

| Layer | Location | Written By | Read By |
|-------|----------|-----------|---------|
| **T0 session ledger** | `memory/t0/sessions/<session_id>/segments/<segment_id>/events.jsonl` + deterministic `source.md` projection | `web_chat_runtime` append points; `task_executor` one-off task events; runtime hook events for trigger/delegation/heartbeat/dream; `SESSION_IDLE/CLOSE` seal chat segments | resume/replay/fork/checkpoint/rollback/export read JSONL first; T2/human review may use projection and source refs |
| **T0 legacy/import logs** | `logs/YYYY-MM-DD/{behavior,system}/` | Legacy import/manual compatibility only; not a runtime T0 writer | legacy import/operators |
| **T2 Segment Package** | `memory/t2/sessions/<session_id>/segments/<t2_segment_id>/{summary.md,labels.md,review.md,manifest.json}` | LLM summary/label agents + independent review; Platform Gate commits package metadata | T3 Consolidator only when complete/standalone; residual T0 evidence lookup |
| **T2 Episode Stitch Package** | `memory/t2/sessions/<session_id>/episodes/<episode_id>/{synthesis.md,review.md,manifest.json}` | Continuity/Episode Stitcher + independent review; Platform Gate commits package metadata | T3 Consolidator for broken/continuing segments after stitching |
| **Explicit Memory Overlay** | `memory/explicit/entries/<explicit_id>.md` + `memory/explicit/manifest.jsonl` + generated `memory/explicit/MEMORY.md` | `save_memory` only for explicit user-commanded memory; write gate enforces sensitivity/privacy | Prompt activation immediately; later T3 absorption candidate |
| **T3 Accepted Memory** | `memory/t3/{episodes.md,user.md,worker.md,capabilities.md}` | T3 Consolidator submits pitch/revised patch; Memory Gate reviews; Platform Gate commits exact accepted XML blocks | Dynamic memory activation and Dream soul evidence |
| **soul.md** | Root workspace | Dream soul reconsolidation through promotion/frozen-mission gates | Prompt injection (frozen prefix) |

The pyramid is the storage and distillation path. Runtime behavior is governed by the owner/company-aware Memory Gate / Platform Gate split:

| Capability | Code paths | Rule |
|------------|------------|------|
| Principal + charter context | `services/agency_charter.py`, `services/principal_context.py` | Agent memory/action decisions must preserve direct owner, company, creator/current user, and delegating agent context when available. |
| Memory write safety | `memory/write_gate.py`, `memory/t2/segment_package.py`, `memory/t3_platform_gate.py`, `memory/explicit_overlay.py`, `tools/handlers/memory.py` | T2/T3 semantic files must be agent-authored candidates reviewed by Memory Gate and committed by Platform Gate. PL4 credentials are rejected; legacy `memory/t2_store.py` is compatibility/repair only. |
| Dynamic activation | `memory/activation.py`, `memory/retriever.py`, `services/memory_service.py`, `runtime/invoker.py` | Prompt memory is activated by objective, owner/company relevance, open-loop pressure, retention/confidence, and sensitivity access. |
| Decision trace + action preflight | `services/action_preflight.py`, `services/decision_trace.py`, `tools/service.py` | External-visible, irreversible, sensitive, or company-conflicting actions must pass preflight before tool execution. |
| Feedback learning | `services/session_feedback.py`, `memory/explicit_overlay.py`, `memory/t2/segment_package.py`, `services/auto_dream.py` | Owner feedback should carry reaction/polarity and link back to `decision/<id>` when possible; explicit feedback enters the overlay immediately and is absorbed into accepted T3 only through the consolidation lane. |
| Session calibration | `services/session_feedback.py`, `models/session_feedback.py`, `api/chat_sessions.py` | Useful/misleading session feedback is persisted and re-enters durable memory only through governed write paths. |
| Memory hygiene | `memory/hygiene.py`, `tools/workspace.py`, `scripts/repair_memory_hygiene.py` | Legacy shadow stores, dead stubs, and missing lifecycle metadata are repaired through reversible shared reports. |
| Coordination runtime | `agents/coordination.py`, `agents/orchestrator.py` | Delegation uses Lease/Signal; confirm-first actions create Checkpoint; Sentinel emits Signal or Checkpoint for trigger-like open loops. |
| Proactive steward loop | `services/proactive_employee_loop.py`, `services/heartbeat.py`, `memory/policy_replay.py` | Heartbeat may prepare low-risk artifacts; external-visible actions require Checkpoint; activation policy changes must pass replay guard. |

**Key files:**

| File | Purpose |
|------|---------|
| `memory/t0/ledger.py` | Append-only T0 session ledger writer, sealer, replay, and legacy import |
| `services/t0_logger.py` | Legacy T0 formatter/import compatibility; runtime hooks no longer call it; chat backfill writes the session ledger |
| `memory/t2/segment_package.py` | Canonical T0→T2 builder: sealed T0 segment -> source_bundle.json -> LLM-authored `summary.md` / `labels.md` / `review.md` -> Platform Gate atomic Segment Package commit. |
| `services/extract_agent.py` / `memory/t2_store.py` | Legacy compatibility, admin backfill, and migration/read-model helpers only. They must not be reconnected to runtime canonical T0→T2 hooks. |
| `services/heartbeat.py` | Platform-managed T2→T3 curation and self-evolution tick (KAIROS persistent session, 120min default eligibility, no-new-T2 skip). Loads `templates/HEARTBEAT.md`; per-agent `workspace/HEARTBEAT.md` overrides via `_load_heartbeat_instruction` — **already SOP-driven** |
| `services/auto_dream.py` | T3→soul consolidation (24h plus 3 sessions or 2 productive heartbeat ticks; soft dream is 6h T3-pressure maintenance). Runtime system prompt now loads `templates/DREAM.md` as dream protocol guidance while preserving the JSON-only consolidator contract; durable memory/soul writeback must go through Soul Memory Gate + Platform Soul Gate, not direct `write_file` under `memory/`. |
| `services/evolution_ledger.py` | `evolution_ledger.jsonl` — candidate → eval (with `traces`) → promotion audit chain for automatic prompt/skill/policy changes. Distinct from per-invocation runtime trace. |
| `services/invocation_trace.py` | Per-invocation runtime trace: file-backed JSONL compatibility plus PostgreSQL `invocation_spans` canonical query surface. |
| `services/session_feedback.py` | Persists useful/misleading feedback and writes calibrated memory through governed paths. |
| `memory/hygiene.py` | Retires legacy shadow stores, quarantines dead stubs, and backfills lifecycle metadata with dry-run/apply reports. |
| `memory/retriever.py` | Read T3 into prompt. High-priority files are injected directly where policy allows; knowledge/strategy/user entries are scored against query. |
| `memory/md_store.py` | Maintains Markdown T3 stores and generated `memory/indexes/wiki_map.md`; the map is a navigation artifact, not the primary retriever route. |
| `runtime/hooks_setup.py` | Hook handlers: T0 writers, extraction triggers, drain on close |

### Hook System (`app/runtime/hooks.py`)

15-event lifecycle bus for memory pipeline and tool governance:

| Category | Events |
|----------|--------|
| Session | `SESSION_START`, `RESPONSE_COMPLETE`, `SESSION_IDLE`, `SESSION_CLOSE` |
| Tool | `PRE_TOOL_USE`, `POST_TOOL_USE`, `POST_TOOL_FAILURE` |
| Compression | `PRE_COMPACTION`, `POST_COMPACTION` |
| Delegation | `DELEGATION_START`, `DELEGATION_END` |
| Hive-specific | `TRIGGER_END`, `HEARTBEAT_TICK_END`, `DREAM_END` |
| Notification | `MEMORY_EXTRACTED` |

Memory pipeline hooks (registered in `hooks_setup.py`):
- `RESPONSE_COMPLETE` → append accepted runtime evidence into the T0 session ledger and route fast-reflection candidate signals; it must not reconnect the legacy `memory/learnings` hot path
- `PRE_COMPACTION` → preserve evidence before context is summarized away; semantic writes still go through Segment Packages / T3 candidates
- `SESSION_IDLE` → seal or advance T0 session ledger segments without duplicating already-flushed messages
- `SESSION_CLOSE` → finalize the T0 segment and trigger canonical T0→T2 Segment Package construction for eligible user-facing segments

### Prompt Architecture (`app/runtime/prompt_sections/`)

14 modular prompt sections assembled by `prompt_builder.py`:

| Section | Source |
|---------|--------|
| `agent_context.py` | Soul identity + tone/style rules |
| `memory_context.py` | Accepted T3 files (`memory/t3/user.md`, `worker.md`, `episodes.md`, `capabilities.md`) plus explicit overlay when activation policy allows |
| `tasks.py` | Active tasks + verification rules |
| `executing_actions.py` | Tool usage + memory save rules |
| `output_efficiency.py` | Response format and conciseness |

Cache boundary: frozen prefix (soul + memory + tools) + dynamic suffix (tasks + session context).

### HR Agent — Agent Creation Pipeline

HR agent (`hr_agent_template/`) creates new agents through conversational guidance. The creation pipeline includes LLM soul refinement:

```
HR conversation (2-3 rounds) → _refine_soul_inputs() → _render_agent_soul_from_blueprint()
                                    ↓ LLM call                    ↓ Python template
                              Refined: role_description,     Structured soul.md:
                              personality, boundaries,        Identity / Users / Outputs /
                              quality_standards, first_tasks  Style / Quality / Boundaries /
                                                              How I Learn
```

Soul refinement prompt teaches the LLM the full 4-layer architecture, soul-vs-focus boundary, and produces role-specific content with BAD/GOOD examples. Falls back to raw inputs if LLM fails.

### Multi-Agent (`app/agents/`)

`delegate_to_agent()` wraps `invoke_agent()` with `SessionContext(source="agent")` and `core_tools_only=True` to prevent nested delegation loops.

### Backend Layout (`backend/app/`)

| Directory | Count | Purpose |
|-----------|-------|---------|
| `api/` | 62 files | FastAPI routers — agents, auth, chat sessions, enterprise, triggers, channels, admin, plaza, office, deep research, interoperability |
| `models/` | 43 files | SQLAlchemy ORM — all async, tenant-scoped with RLS, including invocation spans and session feedback |
| `services/` | 163 files | Business logic — LLM client, trigger/evolution daemons, channel streaming, memory, office, quota, approval, trace, MCP authz, interoperability |
| `services/agent_tool_domains/` | 21 files | Tool domain implementations — Feishu, messaging, tasks, workspace, email |
| `kernel/` | 3 files | Core engine — invocation loop, contracts, context management |
| `runtime/` | 17 files | Hooks, invoker, prompt builder, prompt sections, context engines, workflow runtime, session context, recovery/coordinator helpers |
| `tools/` | 18 handler modules + registry files | Tool registry, governance, runtime groups, catalog, result envelopes, workspace |
| `skills/` | 5 files | Skill parser, loader, registry |
| `memory/` | 25 files | MD-first: retriever, assembler, md_store (T3), t2_store, write gate, activation, lifecycle, retention, access log, replay corpus, hygiene, optional backends |
| `memory/enhancement.py` | — | Optional enhancement adapter boundary; currently a no-op. Native T3 Markdown remains the only memory backend. |
| `core/` | — | Security, permissions, middleware, Redis pub/sub |
| `migrations/` | 79 versions | Alembic schema evolution |

### Frontend Layout (`frontend/src/`)

| Directory | Purpose |
|-----------|---------|
| `pages/` | 16 page entries + 40 nested page/section helper files — AgentDetail, Agent Circle, Company Admin workbench/settings, Admin |
| `components/` | 9 reusable components — ChannelConfig, FileBrowser, MarkdownRenderer, etc. |
| `api/core/` | HTTP abstraction — `request<T>()` with JWT, error handling, upload progress |
| `api/domains/` | 37 files including tests and index — agents, enterprise, tools, chat, office, deep research, memory, notifications, etc. |
| `stores/` | Zustand — `useAuthStore` (user/token) + `useAppStore` (sidebar/selection) |
| `i18n/` | i18next — `en.json` + `zh.json` (both must be updated for any UI text) |
| `types/` | Core TypeScript interfaces — User, Agent, Task, ChatMessage |
| `surfaces/` | Layout shells — App, Workspace, Admin with role-based guards |

**State:** TanStack React Query 5 for server state; Zustand 5 for UI state.
**Routing:** React Router 7 with lazy loading. Guards: ProtectedRoute, WorkspaceGuard, AdminGuard.
**Path alias:** `@/` maps to `src/`.

## Critical Conventions

### Multi-Tenancy
Every entity is tenant-scoped. All queries filter by `tenant_id`. First registered user becomes platform admin. Use `check_agent_access(db, current_user, agent_id)` before returning agent-scoped data. PostgreSQL RLS policies enforce isolation at DB level.

### Agent Kernel Invariant
All agent execution goes through `invoke_agent()` → `AgentKernel.handle()`. Never call LLM directly from a route handler. The kernel is pure (zero DB imports) — all I/O via `KernelDependencies` callbacks.

### Tool Governance Invariant
All tool execution goes through `ToolRuntimeService.execute()` → `run_tool_governance()`. Never call a tool handler directly without governance checks.

### Code Execution Provider Invariant
Agent-controlled code execution must go through `services/code_execution/`. Local/trusted hosts may use `services/subprocess_sandbox.py`; Railway production must use `HIVE_CODE_EXEC_PROVIDER=vercel_sandbox`. Do not launch raw subprocesses from tool handlers or pass host secrets into agent-controlled environments.

### MCP Authz Invariant
MCP import/execution must go through `services/mcp_authz.py`. URL userinfo, `access_token`, and token passthrough credentials are forbidden; legacy `apiKey` query credentials are normalized to authorization headers.

### Trace Invariant
Runtime evidence lives in append-only invocation spans. Spans must carry tenant, agent, user, runtime task, session, request, trace, span, and parent identifiers where available.

### Interoperability Invariant
A2A/interoperability descriptors are machine-readable contracts. Unsupported OAuth delegation or JSON-RPC task surfaces must remain `not_exposed`.

### Memory Hygiene Invariant
Do not manually edit legacy memory stores or dead stubs as a one-off fix. Use `memory/hygiene.py` or `python -m app.scripts.repair_memory_hygiene` so repairs are reversible and reportable.

### Runtime Tool Groups
Agents start with CORE tools plus the dynamic skill catalog. Deferred runtime tool groups (advanced web/crawl, Feishu, email, imported MCP tools, etc.) are discovered through `tool_search`, not by loading a Skill. Active group state is tracked as runtime tool groups for the current invocation; historical `active_packs` wording is compatibility terminology only.

### Alembic Migrations
- Check `alembic heads` before creating — must be single head
- `entrypoint.sh` applies `ALTER TABLE IF NOT EXISTS` patches for backward compatibility
- `main.py` lifespan runs `create_all` on startup

### i18n
Both `en.json` and `zh.json` must be updated for any UI text. Use `t('key')` from `useTranslation()`.

### Channel Integrations
Feishu/Lark, Discord, Slack, DingTalk, WeChat Work, WeChat Personal, Telegram, Email, Microsoft Teams — each has its own router in `api/` and streaming service or delivery path in `services/`. Channel configs are per-agent unless explicitly tenant-scoped. Feishu supports WebSocket long connections via `feishu_ws.py`.

### Environment Variables
Key vars (see `.env.example`): `DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`, `JWT_SECRET_KEY`, `SECRETS_MASTER_KEY`, `AGENT_DATA_DIR`, `EXA_API_KEY`, `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`, `XCRAWL_API_KEY`, `FEISHU_APP_ID`/`FEISHU_APP_SECRET`, `ONLYOFFICE_DOCS_URL`, `ONLYOFFICE_JWT_SECRET`, `WS_IDLE_TIMEOUT_SECONDS`, `HIVE_CODE_EXEC_PROVIDER`, `VERCEL_TEAM_ID`, `VERCEL_PROJECT_ID`, `VERCEL_TOKEN`.

### Ports
Frontend dev: 3008, Backend dev: 8008, PostgreSQL: 5432, Redis: 6379.

### Ruff
`target-version = "py311"`, `line-length = 120`.

## Design Context

See `.impeccable.md` for full details. Key points for all frontend work:

**Users:** Enterprise managers and business teams (non-technical). Interface must be approachable.

**Brand:** Intelligent · Cutting-edge · Refined — Vercel/Raycast sophistication with Notion/Slack warmth.

**Design Principles:**
1. **Clarity over cleverness** — obvious affordances, predictable patterns
2. **Warm intelligence** — tech-forward but approachable, purposeful color, friendly micro-copy
3. **Progressive disclosure** — simple path first, power on demand
4. **Information density when it matters** — scannable dashboards, spacious chat/onboarding
5. **Consistent motion, minimal animation** — fast (120-200ms), purposeful, never decorative

**Technical:** Vanilla CSS custom properties (no framework), Inter font, Tabler Icons, 4px spacing base, dark/light mode via `data-theme`. Refer to `.impeccable.md` for full token reference.
