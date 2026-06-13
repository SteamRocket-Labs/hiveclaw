# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## North Star — Highest-Priority Goal (overrides all other guidance)

Hive exists to be **two things, and every line of code must serve one of them**:

1. **A self-evolving agent infrastructure with enterprise-grade access control** — digital employees that genuinely improve over time (memory, reflection, skill acquisition, soul evolution) while every capability, memory write, and external action stays permission-governed and auditable.
2. **A control plane (控制中台)** for operating those agents at company scale — org/permission management, governance, budgeting, coordination, and observability.

**Quality bar:** the per-agent intelligence and self-evolution must be **at least as good as `hermes-agent`** (internal benchmark at `/Users/rocky243/vc-saas/hermes-agent`) — not merely architecturally grander. A system that *feels* weaker than a lean benchmark agent is a failure of Goal 1, not a success.

**Build order:** Goal 1 (the agent's own intelligence + self-evolution) is the **foundational cornerstone** — it is hardened and judged *first*; the control-plane and agent-to-agent layers build on top of it. When a trade-off is unclear, resolve it in favor of these two goals. Roadmap: `docs/self-evolution-sota-plan.md`.

## AI-Native Design Law (最高设计法律 — judges every architectural decision)

Hive is an **AI-native system**. Three layers, in strict priority order:

1. **L1 — Unleash the model first.** Any step that requires intelligence (summarization, planning, extraction, synthesis, judgment) belongs to the LLM at full capability: complete input visibility (no mechanical pruning of what the model sees), sufficient output budget (no starved `max_tokens`), prompts engineered to benchmark (Claude Code) quality — structure, examples, anti-drift constraints. Mechanical/string-based handling of intelligent steps is allowed ONLY as an observable fallback on failure paths, never the primary path. *Case law: compaction once fed the LLM a `[-40:]` truncated slice with a 2500-token output cap and a silent regex fallback — the canonical violation (fixed in `docs/compaction-cc-alignment.md`).*
2. **L2 — Harness constrains, never replaces.** Governance, safety, budgets, audit wrap *above* model capability: they bound what the agent may **do**, not how well it **thinks**. A constraint that degrades model intelligence (instead of scoping authority) is a design bug.
3. **L3 — Hive's identity: a neutral, organization-facing control plane.** Hive is an independent third party with **model equality**: every feature, every constraint, every prompt works equally for every model — no privileged vendor, no model-specific feature gates, no prompt favoritism. On top of that equality Hive adds what no model vendor provides: the company-scoped controllable agent control plane.

**Review lens — apply to every subsystem:** ① Is the LLM's input visibility complete? ② Is its output budget sufficient? ③ Is the prompt engineered to benchmark quality? ④ Does mechanical processing appear only as an observable fallback?

## Delivery Discipline — One Complete Pass, No MVP (交付纪律 — 一次改完，零技术债)

**Owner law (2026-06-08, "必须记住"): any revision/rework round ships as ONE complete pass — no MVP, no phased "first implementation," no technical debt deferred.** Before starting a change, define the *complete* scope up front (tests, edge cases, error paths, schema migration, **legacy-data backfill**, production cleanup, observability) and deliver it in one pass. Forbidden: "ship Phase 0 first," default-off flags hiding half-built work, "add tests later," "skip the migration for now."

*Case law: the agent memory system rotted into dirty, drifting files precisely because P0–P10 took the spec-sanctioned "first implementation can encode inline rather than rewriting every existing bullet" shortcut and never paid it down — accumulating ten debts D1–D10 (`docs/agent-memory-purity-spec.md`). MVP's "later" = never.*

**Only exception:** a genuinely irreversible step (production data migration/deletion) uses a dry-run + confirmation gate — that is a safety gate, not an MVP stage; completeness never waives safety.

## Project Overview

Hive is an open-source **multi-agent collaboration platform** — enterprise "digital employees" with persistent identity, long-term memory, private workspaces, autonomous trigger-driven execution, governed self-evolution, durable web chat runs, Office workbench editing, and an owner/company-aware Memory Control Plane. Built with FastAPI (Python) backend + React 19 (TypeScript) frontend.

**Version:** tracked in `backend/VERSION` and `frontend/VERSION` (currently 1.7.0).

## Current Engineering Baseline (2026-06-13)

Before making architecture claims, use the current evidence surface:

- `docs/harness-engineering-audit-2026-06-11.md` — harness audit, remediation log, and verification evidence.
- `docs/round2-sota-benchmark-2026.md` — second-round SOTA benchmark and current improvement route.
- `docs/self-evolution-sota-plan.md` — canonical self-evolution foundation and completed substrate baseline.
- `docs/agent-memory-purity-spec.md` — memory purity, lifecycle, and hygiene contract.

Current closures that must not regress:

- Durable self-evolution promotion requires evidence, verification/eval, rollback metadata, and audit records.
- Web chat and long tasks are `RuntimeTask` backed and restart-resumable; browser disconnects are subscription changes, not cancellation.
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
| `runtime/prompt_builder.py` | Assembles system prompt: agent context → knowledge → memory → active packs → skill catalog |
| `runtime/session.py` | `SessionContext` — tracks source, channel, active_packs per invocation |
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
| `handlers/` | 16 handler files: filesystem, search, communication, email, feishu, plaza, skills, triggers, hr, mcp, office, memory, finance, objectives, tasks |
| `workspace.py` | `ensure_workspace()` — bootstraps agent filesystem (soul.md, memory/, skills/, workspace/) |

**60+ built-in tools** across categories: file I/O, web search/fetch, Feishu office (docs/wiki/sheets/base/tasks/calendar), OfficeCLI/ONLYOFFICE document workflows, email, messaging, Agent Circle/plaza, triggers, skills, deep research, MCP.

### Skill System (`app/skills/`)

Markdown files with YAML frontmatter defining agent capabilities. `SkillParser` → `WorkspaceSkillLoader` → `SkillRegistry`. Skills loaded progressively: catalog in prompt, full body via `load_skill` tool.

### Memory System — 4-Layer MD Pyramid + Control Plane

MD files are the source of truth; the legacy SQLite/JSON shadow stores are retired and repaired through `memory/hygiene.py`.

```
T0 (raw logs, 30d)  →  T2 (learnings/*.md)  →  T3 (memory/*.md)  →  soul.md (identity)
     ↑ write                 ↑ extract               ↑ curate              ↑ dream
SESSION_IDLE/CLOSE      RESPONSE_COMPLETE      Heartbeat (45min)     Dream (4h+3 sessions)
  behavior/ only       (in-memory primary;    T2→T3 curation        T3→soul consolidation
  feeds T2 backfill     T0 backfill fallback)
```

**Cadence configuration (P1-W2-5)**: the `evolution_daemon` ticks every
`HEARTBEAT_TICK_SECONDS` (default 60s) and per-agent heartbeat intervals
default to `HEARTBEAT_DEFAULT_INTERVAL_MINUTES` (45min). Both live on
`Settings` and can be overridden via env vars for dev/staging.

**T0 layout (split by role, since PR-1):**

```
logs/YYYY-MM-DD/
  behavior/        ← agent ↔ outside-world events — eligible T2 substrate
    chat-*.md      ← full message thread, tool_calls + tool_results inline
    trigger-*.md   ← same, for scheduled tasks
    delegation-*.md ← same, for agent→agent
  system/          ← distiller self-trace, audit only (NOT fed to T2)
    heartbeat-*.md ← decision reasoning + T2 inputs considered + tool calls
    dream-*.md     ← dedup decisions + soul promotion decisions + reasoning
  artifacts/       ← spilled tool_results > 8000 chars (PR-5)
    {tool_call_id}-{tool_name}.json
```

**T2 extraction has two paths:**

1. **Hot path (primary)**: in-memory messages → `extract_agent` via
   `RESPONSE_COMPLETE` hook → `learnings/*.md`. Per-agent cursor skips
   already-processed messages.
2. **Backfill path (PR-4)**: behavior T0 MD → `replay_messages_from_t0`
   → same extractor → `learnings/*.md`. Gated by
   `learnings/.backfill_cursor.json` (idempotent by `session_id`).
   Triggered manually via `POST /api/admin/agents/{id}/backfill-t2`.
   Lifespan startup replays the durable hot-path extract queue via
   `extract_queue_replay`; full T0 backfill remains an explicit admin action.

| Layer | Location | Written By | Read By |
|-------|----------|-----------|---------|
| **T0 behavior** | `logs/YYYY-MM-DD/behavior/` | `t0_logger.write_t0_log` (post-session/trigger/delegation) | `extract_agent.backfill_missing_extractions` |
| **T0 system**   | `logs/YYYY-MM-DD/system/`   | heartbeat / auto_dream | Operators only |
| **T0 artifacts**| `logs/YYYY-MM-DD/artifacts/` | `_spillover_large_tool_results` when tool_result > 8000 chars | `_resolve_artifact_content` during backfill |
| **T2** | `memory/learnings/*.md` | `extract_agent` (LLM hot path + pattern fallback) | Heartbeat curation |
| **T3** | `memory/feedback.md`, `knowledge.md`, `strategies.md`, `blocked.md`, `user.md` | Heartbeat (T2→T3) | Prompt injection via `retriever.py` |
| **soul.md** | Root workspace | Dream consolidation | Prompt injection (frozen prefix) |
| **focus.md** | Root workspace | Agent + heartbeat | Prompt injection (dynamic suffix) |

The pyramid is the storage and distillation path. Runtime behavior is governed by the owner/company-aware Memory Control Plane:

| Capability | Code paths | Rule |
|------------|------------|------|
| Principal + charter context | `services/agency_charter.py`, `services/principal_context.py` | Agent memory/action decisions must preserve direct owner, company, creator/current user, and delegating agent context when available. |
| Memory write safety | `memory/write_gate.py`, `memory/t2_store.py`, `tools/handlers/memory.py` | Do not persist new durable T2/T3 memory without privacy/sensitivity classification and lifecycle/evidence metadata. PL4 credentials are rejected. |
| Dynamic activation | `memory/activation.py`, `memory/retriever.py`, `services/memory_service.py`, `runtime/invoker.py` | Prompt memory is activated by objective, owner/company relevance, open-loop pressure, retention/confidence, and sensitivity access. |
| Decision trace + action preflight | `services/action_preflight.py`, `services/decision_trace.py`, `tools/service.py` | External-visible, irreversible, sensitive, or company-conflicting actions must pass preflight before tool execution. |
| Feedback learning | `services/extract_agent.py`, `memory/t2_store.py`, `services/auto_dream.py` | Owner feedback should carry reaction/polarity and link back to `decision/<id>` when possible; dream may propose calibration, not silently mutate charter. |
| Session calibration | `services/session_feedback.py`, `models/session_feedback.py`, `api/chat_sessions.py` | Useful/misleading session feedback is persisted and re-enters durable memory only through governed write paths. |
| Memory hygiene | `memory/hygiene.py`, `tools/workspace.py`, `scripts/repair_memory_hygiene.py` | Legacy shadow stores, dead stubs, and missing lifecycle metadata are repaired through reversible shared reports. |
| Coordination runtime | `agents/coordination.py`, `agents/orchestrator.py` | Delegation uses Lease/Signal; confirm-first actions create Checkpoint; Sentinel emits Signal or Checkpoint for trigger-like open loops. |
| Proactive steward loop | `services/proactive_employee_loop.py`, `services/heartbeat.py`, `memory/policy_replay.py` | Heartbeat may prepare low-risk artifacts; external-visible actions require Checkpoint; activation policy changes must pass replay guard. |

**Key files:**

| File | Purpose |
|------|---------|
| `services/t0_logger.py` | Write T0 MD logs (chat, trigger, delegation, heartbeat, dream) |
| `services/extract_agent.py` | LLM extraction T0→T2 (cursor-based, per-response via RESPONSE_COMPLETE hook). T2 entries carry `[w=][src=][cat=]` metadata; source bucket weights live in `memory/t2_store.py`. |
| `services/heartbeat.py` | T2→T3 curation (KAIROS persistent session, 45min ticks). Loads `templates/HEARTBEAT.md`; per-agent `workspace/HEARTBEAT.md` overrides via `_load_heartbeat_instruction` — **already SOP-driven** |
| `services/auto_dream.py` | T3→soul consolidation (24h + 3 sessions gate). Runtime system prompt now loads `templates/DREAM.md` as dream protocol guidance while preserving the JSON-only consolidator contract; durable memory/soul writeback is applied by the Memory Control Plane/internal dream service, not by direct `write_file` under `memory/`. |
| `services/evolution_ledger.py` | `evolution_ledger.jsonl` — candidate → eval (with `traces`) → promotion audit chain for automatic prompt/skill/policy changes. Distinct from per-invocation runtime trace. |
| `services/invocation_trace.py` | Per-invocation runtime trace: file-backed JSONL compatibility plus PostgreSQL `invocation_spans` canonical query surface. |
| `services/session_feedback.py` | Persists useful/misleading feedback and writes calibrated memory through governed paths. |
| `memory/hygiene.py` | Retires legacy shadow stores, quarantines dead stubs, and backfills lifecycle metadata with dry-run/apply reports. |
| `memory/retriever.py` | Read T3 into prompt. High-priority files are injected directly where policy allows; knowledge/strategy/user entries are scored against query. |
| `memory/md_store.py` | Maintains Markdown T3 stores and `memory/INDEX.md`; the index is a navigation artifact, not the primary retriever route. |
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
- `RESPONSE_COMPLETE` → fire-and-forget LLM extraction to T2 (CC Stop hook equivalent)
- `PRE_COMPACTION` → synchronous extraction before context is lost
- `SESSION_IDLE` → incremental T0 write (cursor-based, no duplication on reconnect)
- `SESSION_CLOSE` → drain extractor + incremental T0 write

### Prompt Architecture (`app/runtime/prompt_sections/`)

14 modular prompt sections assembled by `prompt_builder.py`:

| Section | Source |
|---------|--------|
| `agent_context.py` | Soul identity + tone/style rules |
| `memory_context.py` | T3 MD files (feedback, knowledge, strategies, blocked, user) |
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
| `runtime/` | 13 files | Hooks, invoker, prompt builder, prompt sections, session context, recovery/coordinator helpers |
| `tools/` | 16 files | Tool registry, governance, packs, catalog, result envelopes, workspace |
| `skills/` | 5 files | Skill parser, loader, registry |
| `memory/` | 25 files | MD-first: retriever, assembler, md_store (T3), t2_store, write gate, activation, lifecycle, retention, access log, replay corpus, hygiene, optional backends |
| `memory/backends/` | `hindsight.py` | Optional read-side accelerator; opt-in per tenant via `tenants.memory_backend` column. Module docstring has the design invariants and operator runbook. |
| `core/` | — | Security, permissions, middleware, Redis pub/sub |
| `migrations/` | 79 versions | Alembic schema evolution |

### Frontend Layout (`frontend/src/`)

| Directory | Purpose |
|-----------|---------|
| `pages/` | 16 pages + 25 section files — AgentDetail, Agent Circle, Company Admin workbench/settings, Admin |
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

### Capability Packs
Agents start with kernel-only tools (file I/O, skill loading, triggers). Capability packs (web, feishu, email, etc.) activate on-demand when a skill is loaded. Pack state tracked in `SessionContext.active_packs`.

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
