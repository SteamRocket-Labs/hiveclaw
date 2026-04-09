# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Hive is an open-source **multi-agent collaboration platform** — enterprise "digital employees" with persistent identity, long-term memory, private workspaces, and autonomous trigger-driven execution. Built with FastAPI (Python) backend + React 19 (TypeScript) frontend.

**Version:** tracked in the root `VERSION` file (currently 1.7.0), shared by both frontend and backend.

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
Entry Points (WebSocket, Feishu, Slack, DingTalk, WeChat, Teams, Trigger, Heartbeat, Delegation)
    ↓
runtime/invoker.py — invoke_agent() resolves deps, builds prompt, calls kernel
    ↓
kernel/engine.py — AgentKernel.handle() — stateless LLM loop, zero DB deps (1,505 LOC)
    ↓ (14 injected callbacks via KernelDependencies)
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

**Execution flow:** Every entry point builds an `InvocationRequest` and calls `invoke_agent()`. The kernel runs a multi-round LLM loop (max 50 rounds) with streaming callbacks. Context compaction at 85% threshold, tool result eviction at 50KB/result.

### Tool System (`app/tools/`)

Tools follow a registry + executor + governance pattern:

| File | Purpose |
|------|---------|
| `runtime.py` | `ToolExecutionRegistry` — name → executor mapping, `try_execute()` |
| `service.py` | `ToolRuntimeService` — wraps governance + execution + timeout + logging |
| `governance.py` | `run_tool_governance()` — 2-layer preflight: security zone → capability gate |
| `governance_resolver.py` | Connects governance to real DB (security_zone, capability policies, approval) |
| `packs.py` | `ToolPackSpec` — static capability bundles (web, feishu, email, etc.) |
| `handlers/` | 11 handler files: filesystem, search, communication, email, feishu, plaza, skills, triggers, hr, mcp |
| `workspace.py` | `ensure_workspace()` — bootstraps agent filesystem (soul.md, memory/, skills/, workspace/) |

**60+ built-in tools** across categories: file I/O, web search/fetch, Feishu office (docs/wiki/sheets/base/tasks/calendar), email, messaging, plaza, triggers, skills, MCP.

### Skill System (`app/skills/`)

Markdown files with YAML frontmatter defining agent capabilities. `SkillParser` → `WorkspaceSkillLoader` → `SkillRegistry`. Skills loaded progressively: catalog in prompt, full body via `load_skill` tool.

### Memory System — 4-Layer MD Pyramid

MD files are the source of truth. SQLite is demoted to FTS recall index only.

```
T0 (raw logs, 30d)  →  T2 (learnings/*.md, episodic)  →  T3 (memory/*.md, semantic)  →  soul.md (identity)
     ↑ write                    ↑ extract                       ↑ curate                      ↑ dream
SESSION_IDLE/CLOSE      RESPONSE_COMPLETE              Heartbeat (45min)              Dream (4h+3s gate)
  cursor-based            cursor-based                  T2→T3 curation               T3→soul consolidation
```

| Layer | Location | Written By | Read By |
|-------|----------|-----------|---------|
| **T0** | `logs/YYYY-MM-DD/*.md` | `t0_logger.py` (cursor-based, incremental) | Dream gate counting |
| **T2** | `memory/learnings/*.md` | `extract_agent.py` (LLM per-response) | Heartbeat curation |
| **T3** | `memory/feedback.md`, `knowledge.md`, `strategies.md`, `blocked.md`, `user.md` | Heartbeat (T2→T3) | Prompt injection via `retriever.py` |
| **soul.md** | Root workspace | Dream consolidation | Prompt injection (frozen prefix) |
| **focus.md** | Root workspace | Agent + heartbeat | Prompt injection (dynamic suffix) |

**Key files:**

| File | Purpose |
|------|---------|
| `services/t0_logger.py` | Write T0 MD logs (chat, trigger, delegation, heartbeat, dream) |
| `services/extract_agent.py` | LLM extraction T0→T2 (cursor-based, per-response via RESPONSE_COMPLETE hook) |
| `services/heartbeat.py` | T2→T3 curation (KAIROS persistent session, 45min ticks) |
| `services/auto_dream.py` | T3→soul consolidation (4h + 3 sessions gate) |
| `memory/retriever.py` | Read T3 MD files directly into prompt (sqlite for recall-only) |
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
| `api/` | 48 files | FastAPI routers — agents, auth, chat, enterprise, triggers, channels, admin, plaza |
| `models/` | 31 files | SQLAlchemy ORM — all async, tenant-scoped with RLS |
| `services/` | 58 files | Business logic — LLM client, trigger daemon, channel streaming, quota, approval |
| `services/agent_tool_domains/` | 20 files | Tool domain implementations — Feishu (8), messaging, tasks, workspace, email |
| `kernel/` | 3 files | Core engine — invocation loop, contracts, context management |
| `runtime/` | 8+ files | Hooks, invoker, prompt builder, prompt sections, session context |
| `tools/` | 11+ files | Tool registry, governance, handlers, workspace |
| `skills/` | 5 files | Skill parser, loader, registry |
| `memory/` | 5 files | Retriever (T3 MD → prompt), assembler, sqlite FTS (recall-only) |
| `core/` | — | Security, permissions, middleware, Redis pub/sub |
| `migrations/` | 35 versions | Alembic schema evolution |

### Frontend Layout (`frontend/src/`)

| Directory | Purpose |
|-----------|---------|
| `pages/` | 17 pages + 20 section files — Dashboard, AgentDetail, Plaza, EnterpriseSettings, Admin |
| `components/` | 9 reusable components — ChannelConfig, FileBrowser, MarkdownRenderer, etc. |
| `api/core/` | HTTP abstraction — `request<T>()` with JWT, error handling, upload progress |
| `api/domains/` | 20 typed domain adapters — agents, enterprise, tools, chat, notifications, etc. |
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

### Capability Packs
Agents start with kernel-only tools (file I/O, skill loading, triggers). Capability packs (web, feishu, email, etc.) activate on-demand when a skill is loaded. Pack state tracked in `SessionContext.active_packs`.

### Alembic Migrations
- Check `alembic heads` before creating — must be single head
- `entrypoint.sh` applies `ALTER TABLE IF NOT EXISTS` patches for backward compatibility
- `main.py` lifespan runs `create_all` on startup

### i18n
Both `en.json` and `zh.json` must be updated for any UI text. Use `t('key')` from `useTranslation()`.

### Channel Integrations
Feishu/Lark, Discord, Slack, DingTalk, WeChat Work, Microsoft Teams — each has its own router in `api/` and streaming service in `services/`. Channel configs are per-agent. Feishu supports WebSocket long connections via `feishu_ws.py`.

### Environment Variables
Key vars (see `.env.example`): `DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`, `JWT_SECRET_KEY`, `SECRETS_MASTER_KEY`, `AGENT_DATA_DIR`, `EXA_API_KEY`, `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`, `XCRAWL_API_KEY`, `FEISHU_APP_ID`/`FEISHU_APP_SECRET`.

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
