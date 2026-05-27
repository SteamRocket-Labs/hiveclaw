# AGENTS.md

Technical reference for AI coding assistants working with the Hive platform.

## North Star — Highest-Priority Goal (overrides all other guidance)

Hive exists to be **two things, and every line of code must serve one of them**:

1. **A self-evolving agent infrastructure with enterprise-grade access control** — digital employees that genuinely improve over time (memory, reflection, skill acquisition, soul evolution) while every capability, memory write, and external action stays permission-governed and auditable.
2. **A control plane (控制中台)** for operating those agents at company scale — org/permission management, governance, budgeting, coordination, and observability.

**Quality bar:** the per-agent intelligence and self-evolution must be **at least as good as `hermes-agent`** (internal benchmark at `/Users/rocky243/vc-saas/hermes-agent`) — not merely architecturally grander. A system that *feels* weaker than a lean benchmark agent is a failure of Goal 1, not a success.

**Build order:** Goal 1 (the agent's own intelligence + self-evolution) is the **foundational cornerstone** — it is hardened and judged *first*; the control-plane and agent-to-agent layers build on top of it. Roadmap: `docs/self-evolution-sota-plan.md`.

## Project Overview

Hive is an open-source **multi-agent collaboration platform** — enterprise "digital employees" with persistent identity, long-term memory, private workspaces, autonomous trigger-driven execution, and an owner/company-aware Memory Control Plane.

- **Version:** 1.7.0 (tracked in `backend/VERSION` and `frontend/VERSION`)
- **License:** Apache 2.0
- **Stack:** FastAPI (Python 3.12) + React 19 (TypeScript 5) + PostgreSQL 15 + Redis 7
- **Deployment:** Docker / Railway

## Commands

```bash
# Setup
bash setup.sh --dev

# Run
bash restart.sh                    # Backend(:8008) + Frontend(:3008)

# Backend (cd backend/)
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8008 --reload
ruff check app/ --fix && ruff format app/
pytest
alembic upgrade head
alembic revision --autogenerate -m "desc"

# Frontend (cd frontend/)
npm run dev                        # Vite on :3008
npm run build                      # tsc + vite build

# Docker
docker compose up -d --build       # Full stack → :3008
```

## Backend Architecture (`backend/app/`)

### Codebase Stats

| Layer | Files | LOC | Purpose |
|-------|-------|-----|---------|
| API Routes | 55 | ~20K | FastAPI routers |
| Models | 36 | ~1.8K | SQLAlchemy ORM (async, RLS) |
| Services | 130 | ~49K | Business logic |
| Tool Domains | 21 | — | Feishu office, messaging, tasks, workspace, email |
| Kernel | 3 | ~2.7K | Core LLM execution engine |
| Tools | 16 handlers | — | Handler implementations |
| Skills | 5 | ~310 | Markdown skill system |
| Memory | 18 | — | MD-first pyramid (T0/T2/T3/soul) + control plane: write gate, activation, retention, lifecycle, understanding |
| Migrations | 58 | — | Alembic schema versions |

### API Routers (55 files)

Core: `agents`, `auth`, `users`, `tenants`, `enterprise`, `admin`
Agent features: `tasks`, `triggers`, `schedules`, `relationships`, `skills`, `files`, `chat_sessions`, `objectives`, `autonomy`, `deep_research`
Channels: `feishu`, `slack`, `discord_bot`, `dingtalk`, `wecom`, `wechat_personal`, `teams`, `telegram`, `email_channel`, `tenant_channels`
Platform: `tools`, `packs`, `capabilities`, `plaza`, `notification`, `websocket`, `office`
Enterprise: `organization`, `memory`, `guard_policies`, `feature_flags`, `config_history`, `role_templates`
Desktop: `desktop_auth`, `desktop_sync`, `desktop_agents`, `desktop_audit`
Other: `upload`, `webhooks`, `gateway`, `llm_proxy`, `oidc`, `onboarding`, `advanced`, `atlassian`

Most routers are mounted under both `/api` and `/api/v1`; public webhooks and `/ws/chat/{agent_id}` are mounted without the API prefix.

### Models (36 files)

Core entities: `User`, `Agent`, `Tenant`, `LLMModel`, `Tool`, `Skill`, `Task`, `RuntimeTask`, `Objective`
Agent config: `AgentTrigger`, `AgentSchedule`, `ChannelConfig`, `AgentPermission`, `AgentTemplate`
Relationships: `AgentRelationship`, `AgentAgentRelationship`, `OrgMember`, `OrgDepartment`, coordination lease/signal/checkpoint models
Audit: `AuditLog`, `SecurityAuditEvent`, `ChatMessage`, `ChatSession`, `AgentActivityLog`
Platform: `CapabilityPolicy`, `CapabilityInstall`, `GuardPolicy`, `FeatureFlag`, `Notification`
Auth: `RefreshToken`, `InvitationCode`, `Participant`, identity provider models
Social: `PlazaPost`, `PlazaComment`, `PlazaLike`

### Services (130 files)

| Category | Services |
|----------|---------|
| Agent lifecycle | `agent_manager`, `agent_seeder`, `auto_dream`, `auto_provision` |
| LLM | `llm_client` (OpenAI/Anthropic/Gemini/compatible), `llm_utils` |
| Execution | `trigger_daemon` (15s loop), `task_executor`, `scheduler`, `heartbeat`, `evolution_daemon`, `web_chat_runtime`, `long_task_runtime` |
| Channels | `feishu_service`, `feishu_ws`, `dingtalk_stream`, `wecom_stream`, `wechat_personal_stream`, `channel_delivery_service` |
| Tools | `agent_tools`, `agent_tool_assignment_service`, `tool_seeder`, `tool_telemetry` |
| Security | `capability_gate`, `approval_service`, `quota_guard`, `secrets_provider`, `audit_logger` |
| Memory | `memory_service`, `conversation_summarizer`, `knowledge_inject`, `extract_agent`, `extract_queue`, `agency_charter`, `decision_trace` |
| Integration | `mcp_client`, `mcp_registry_service`, `email_service`, `viking_client` |
| Multi-tenant | `enterprise_sync`, `org_sync_service`, `sync_service` |
| Office / docs | `office_document_service`, `officecli_adapter`, `text_extractor` |
| Other | `pack_service`, `skill_creator_content`, `token_tracker`, `objective_service`, `autonomy_repair_plan` |

### Memory Control Plane

Hive keeps the T0/T2/T3/soul Markdown memory pyramid, but runtime behavior is governed by a Memory Control Plane. This layer decides what can be stored, what can be activated, and which actions require owner/company confirmation.

| Capability | Primary code paths | Runtime invariant |
|------------|--------------------|-------------------|
| Principal + charter context | `services/agency_charter.py`, `services/principal_context.py` | Memory/action decisions must know direct owner, company, creator/current user, and delegation context when available. |
| Write safety | `memory/write_gate.py`, `memory/t2_store.py`, `tools/handlers/memory.py` | New durable memories must pass privacy/sensitivity classification before T2/T3 persistence; PL4 credentials are rejected. |
| Dynamic activation | `memory/activation.py`, `memory/retriever.py`, `services/memory_service.py`, `runtime/invoker.py` | Prompt memory is selected by owner/company/goal/open-loop relevance and sensitivity access, not by static file inclusion alone. |
| Decision trace + preflight | `services/action_preflight.py`, `services/decision_trace.py`, `tools/service.py` | External-visible, sensitive, irreversible, or company-conflicting tool calls must pass preflight before execution. |
| Coordination primitives | `agents/coordination.py`, `agents/orchestrator.py`, `tools/service.py` | Cross-agent work uses Lease/Signal; confirm-first actions create Checkpoint; Sentinel can emit Signal or Checkpoint. |
| Proactive steward loop | `services/proactive_employee_loop.py`, `services/heartbeat.py`, `memory/policy_replay.py` | Heartbeat may prepare low-risk work, but external-visible action requires Checkpoint and policy tuning requires replay guard. |

### Web Chat Runtime

Web chat runs are durable background tasks:

- `chat_sessions.py` exposes session history and start/active/cancel run APIs.
- `web_chat_runtime.py` creates and executes `RuntimeTask(task_type="web_chat_turn")`.
- `web_chat_broker.py` broadcasts session-scoped runtime events to WebSocket subscribers.
- `websocket.py` is a subscription and compatibility start path; disconnecting the browser must not cancel the run.
- Frontend `AgentDetail.tsx` sends a 30s keepalive ping while waiting/streaming; backend replies with `pong`.
- `WS_IDLE_TIMEOUT_SECONDS` defaults to 3600; if an active run exists, idle close is deferred.

### Office Runtime

Office editing is a first-class runtime:

- Backend API: `backend/app/api/office.py`
- Workspace document service: `backend/app/services/office_document_service.py`
- OfficeCLI adapter: `backend/app/services/officecli_adapter.py`
- Frontend workbench: `frontend/src/pages/agent-detail/OfficeWorkbenchSection.tsx`
- Required production env includes `ONLYOFFICE_DOCS_URL`, `ONLYOFFICE_JWT_SECRET`, and public base URL config.
- Agent workspace remains file source of truth; ONLYOFFICE handles browser WYSIWYG editing and signed callbacks.

### Kernel Engine

Stateless LLM loop with dependency injection. Zero DB imports — all I/O goes through `KernelDependencies` callbacks.

- Max 200 tool rounds per invocation (`Agent.max_tool_rounds`); heartbeat overrides to 40
- Semantic loop detection via `LoopGuard` (`kernel/loop_guard.py`, wired in `engine.py`)
- Proactive compaction at 75% utilization (`_MIDLOOP_COMPACT_THRESHOLD`); microcompact pressure at 60%; reactive compaction on prompt-too-long
- Tool result eviction: 50KB/result, 200KB/round
- Parallel-safe tool execution
- Vision support for multimodal models
- Provider-specific cache hints

### Tool Handlers (60+ tools)

| Handler | Tools |
|---------|-------|
| `filesystem` | list_files, read_file, write_file, edit_file, delete_file |
| `search` | web_search, web_fetch, firecrawl_fetch, xcrawl_scrape |
| `communication` | send_feishu_message, send_web_message |
| `email` | send_email, read_emails, reply_email |
| `feishu` | feishu_wiki_list, feishu_doc_read/append/create/share |
| `office` | office_document_create/view/query/apply/validate/dump |
| `deep_research` | deep_research_start/check/cancel/export |
| `memory` | save_memory and memory-control helpers |
| `finance` | finance provider status, statements, filings, workflows |
| `plaza` | plaza_get_new_posts, plaza_create_post, plaza_add_comment |
| `skills` | load_skill, tool_search |
| `triggers` | set_trigger, update_trigger, list_triggers, cancel_trigger |
| `hr` | create_digital_employee |
| `mcp` | list_mcp_resources, read_mcp_resource, import_mcp_server |

## Frontend Architecture (`frontend/src/`)

### Pages (16 + 25 sections)

| Page | Route | Purpose |
|------|-------|---------|
| Login | `/login` | Authentication |
| CompanySetup | `/setup-company` | Tenant onboarding |
| Dashboard / Workbench | `/enterprise/dashboard` | Company Admin workbench; `/dashboard` redirects here |
| Agent Circle | `/plaza` | Agent social feed; backend route remains `plaza` |
| AgentDetail | `/agents/:id` | Agent management hub |
| EnterpriseSettings | `/enterprise/*` | Workspace admin settings sections |
| PlatformDashboard | `/admin/*` | Platform admin |
| UserManagement | `/enterprise/users` | User/team admin |

### Tech Stack

| Aspect | Choice |
|--------|--------|
| Framework | React 19 |
| Bundler | Vite 6 |
| Routing | React Router 7 (lazy loading) |
| Server state | TanStack React Query 5 |
| Client state | Zustand 5 |
| i18n | i18next (en + zh) |
| Icons | Tabler Icons |
| Charts | Recharts 3 |
| Tests | Vitest 4 (14 suites) |

### API Layer

Core HTTP abstraction in `api/core/request.ts` — `get<T>()`, `post<T>()`, `put<T>()` with JWT auth and tenant header injection.

25 production domain adapters in `api/domains/` (30 files including tests and index), including agents, enterprise, tools, chat, auth, notifications, files, tasks, skills, relationships, plaza, channels, schedules, admin, activity, users, messages, system, triggers, office, deepResearch, memory, objectives, autonomy, and evolution.

## Conventions

- **Multi-tenancy:** All entities tenant-scoped. PostgreSQL RLS. `check_agent_access()` required.
- **Kernel invariant:** All LLM calls via `invoke_agent()` → `AgentKernel.handle()`. Never direct.
- **Tool governance:** All tool calls via `ToolRuntimeService.execute()`. Never bypass.
- **Memory write invariant:** Do not write T2/T3 durable memory directly from tools or extractors; use `prepare_memory_write()` or an existing wrapper that calls it.
- **Memory read invariant:** Prompt memory retrieval must preserve `ActivationContext` and sensitivity stripping when current user/owner/company context is known.
- **Action boundary invariant:** Do not bypass `ActionPreflightService` for external-visible, sensitive, irreversible, or company-boundary actions.
- **Agent creation invariant:** New employee agents must render first-person accountability plus frozen Company Charter and Owner Agency Charter sections in `soul.md`.
- **Coordination invariant:** Duplicate delegation should acquire a Lease; progress/handoff should use Signal; confirm-first work should create Checkpoint metadata.
- **i18n:** Both `en.json` and `zh.json` must be updated for any UI text.
- **Migrations:** `alembic heads` must show single head before creating new migration.
- **Ruff:** `target-version = "py311"`, `line-length = 120`.
- **Ports:** Frontend 3008, Backend 8008, PostgreSQL 5432, Redis 6379.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL async connection |
| `REDIS_URL` | Redis cache/sessions |
| `SECRET_KEY` | Session secret |
| `JWT_SECRET_KEY` | JWT signing |
| `SECRETS_MASTER_KEY` | Encrypt LLM keys and channel credentials |
| `AGENT_DATA_DIR` | Agent workspace root |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | Feishu SSO |
| `ONLYOFFICE_DOCS_URL` / `ONLYOFFICE_INTERNAL_DOCS_URL` / `ONLYOFFICE_JWT_SECRET` | Browser Office editing |
| `WS_IDLE_TIMEOUT_SECONDS` / `WS_IDLE_DREAM_SECONDS` | Web chat WebSocket idle and idle-hook behavior |
| `TAVILY_API_KEY` | Web search |
| `EXA_API_KEY` | Web search |
| `FIRECRAWL_API_KEY` | Web crawling |
| `XCRAWL_API_KEY` | Web crawling |
