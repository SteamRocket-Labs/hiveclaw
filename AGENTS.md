# AGENTS.md

Technical reference for AI coding assistants working with the Hive platform.

## North Star — Highest-Priority Goal (overrides all other guidance)

Hive exists to be **two things, and every line of code must serve one of them**:

1. **A self-evolving agent infrastructure with enterprise-grade access control** — digital employees that genuinely improve over time (memory, reflection, skill acquisition, soul evolution) while every capability, memory write, and external action stays permission-governed and auditable.
2. **A control plane (控制中台)** for operating those agents at company scale — org/permission management, governance, budgeting, coordination, and observability.

**Quality bar:** the per-agent intelligence and self-evolution must be **at least as good as `hermes-agent`** (internal benchmark at `/Users/rocky243/vc-saas/hermes-agent`) — not merely architecturally grander. A system that *feels* weaker than a lean benchmark agent is a failure of Goal 1, not a success.

**Build order:** Goal 1 (the agent's own intelligence + self-evolution) is the **foundational cornerstone** — it is hardened and judged *first*; the control-plane and agent-to-agent layers build on top of it. Roadmap: `docs/self-evolution-sota-plan.md`.

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

Hive is an open-source **multi-agent collaboration platform** — enterprise "digital employees" with persistent identity, long-term memory, private workspaces, autonomous trigger-driven execution, governed self-evolution, and an owner/company-aware Memory Control Plane.

- **Version:** 1.7.0 (tracked in `backend/VERSION` and `frontend/VERSION`)
- **License:** Apache 2.0
- **Stack:** FastAPI (Python 3.12) + React 19 (TypeScript 5) + PostgreSQL 15 + Redis 7
- **Deployment:** Docker / Railway

## Current Engineering Baseline (2026-06-13)

Treat these documents as the current truth surface before making architecture claims:

- `docs/harness-engineering-audit-2026-06-11.md` — harness audit, remediation log, and verification evidence.
- `docs/round2-sota-benchmark-2026.md` — second-round SOTA benchmark and current improvement route.
- `docs/self-evolution-sota-plan.md` — canonical self-evolution foundation, now a completed substrate plus ongoing benchmark baseline.
- `docs/agent-memory-purity-spec.md` — memory purity, lifecycle, and hygiene contract.

Current implemented closures that future work must preserve:

- Hard verification and rollback metadata are required for durable self-evolution promotion.
- `RuntimeTask` execution is restart-resumable and web chat disconnects do not cancel runs.
- `invocation_spans` are the canonical DB trace surface; JSONL spans remain compatibility artifacts.
- Provider retry/overload fallback, token budget gates, CJK-aware estimates, canonical prompt-cache anchors, and Anthropic thinking-signature preservation are runtime contracts.
- Agent-controlled code execution is provider based: local/trusted hosts use the shared OS sandbox builder (`bubblewrap` or `sandbox-exec`), while Railway production uses `HIVE_CODE_EXEC_PROVIDER=vercel_sandbox` and Vercel Sandbox credentials. Never fall back to raw subprocesses.
- MCP authz rejects token passthrough and URL userinfo; A2A Agent Cards and `/interoperability/profile` must state unsupported OAuth/JSON-RPC surfaces as `not_exposed`.
- Memory hygiene startup repair retires legacy shadow stores and quarantines dead stubs through a reversible shared path.
- Latest full backend evidence before the current documentation-only update: `cd backend && source .venv/bin/activate && pytest tests -q` -> `4223 passed, 7 skipped, 4 warnings`.

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
python -m app.scripts.repair_memory_hygiene      # dry-run fleet memory hygiene
python -m app.scripts.repair_memory_hygiene --apply --confirm

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
| API Routes | 62 | — | FastAPI routers |
| Models | 43 | — | SQLAlchemy ORM (async, RLS) |
| Services | 163 | — | Business logic |
| Tool Domains | 21 | — | Feishu office, messaging, tasks, workspace, email |
| Kernel | 3 | ~2.7K | Core LLM execution engine |
| Tools | 16 handlers | — | Handler implementations |
| Skills | 5 | ~310 | Markdown skill system |
| Memory | 25 | — | MD-first pyramid (T0/T2/T3/soul) + control plane: write gate, activation, retention, lifecycle, understanding, hygiene |
| Migrations | 79 | — | Alembic schema versions |

### API Routers (62 files)

Core: `agents`, `auth`, `users`, `tenants`, `enterprise`, `admin`
Agent features: `tasks`, `triggers`, `schedules`, `relationships`, `skills`, `files`, `chat_sessions`, `objectives`, `autonomy`, `deep_research`
Channels: `feishu`, `slack`, `discord_bot`, `dingtalk`, `wecom`, `wechat_personal`, `teams`, `telegram`, `email_channel`, `tenant_channels`
Platform: `tools`, `packs`, `capabilities`, `plaza`, `notification`, `websocket`, `office`, `interoperability`
Enterprise: `organization`, `memory`, `guard_policies`, `feature_flags`, `config_history`, `role_templates`
Desktop: `desktop_auth`, `desktop_sync`, `desktop_agents`, `desktop_audit`
Other: `upload`, `webhooks`, `gateway`, `llm_proxy`, `oidc`, `onboarding`, `advanced`, `atlassian`

Most routers are mounted under both `/api` and `/api/v1`; public webhooks and `/ws/chat/{agent_id}` are mounted without the API prefix.

### Models (43 files)

Core entities: `User`, `Agent`, `Tenant`, `LLMModel`, `Tool`, `Skill`, `Task`, `RuntimeTask`, `Objective`
Agent config: `AgentTrigger`, `AgentSchedule`, `ChannelConfig`, `AgentPermission`, `AgentTemplate`
Relationships: `AgentRelationship`, `AgentAgentRelationship`, `OrgMember`, `OrgDepartment`, coordination lease/signal/checkpoint models
Audit: `AuditLog`, `SecurityAuditEvent`, `ChatMessage`, `ChatSession`, `AgentActivityLog`, `InvocationSpan`, `SessionFeedbackEvent`
Platform: `CapabilityPolicy`, `CapabilityInstall`, `GuardPolicy`, `FeatureFlag`, `Notification`
Auth: `RefreshToken`, `InvitationCode`, `Participant`, identity provider models
Social: `PlazaPost`, `PlazaComment`, `PlazaLike`

### Services (163 files)

| Category | Services |
|----------|---------|
| Agent lifecycle | `agent_manager`, `agent_seeder`, `auto_dream`, `auto_provision` |
| LLM | `llm_client` (OpenAI/Anthropic/Gemini/compatible), `llm_utils` |
| Execution | `trigger_daemon` (15s loop), `task_executor`, `scheduler`, `heartbeat`, `evolution_daemon`, `web_chat_runtime`, `long_task_runtime`, `invocation_trace` |
| Channels | `feishu_service`, `feishu_ws`, `dingtalk_stream`, `wecom_stream`, `wechat_personal_stream`, `channel_delivery_service` |
| Tools | `agent_tools`, `agent_tool_assignment_service`, `tool_seeder`, `tool_telemetry` |
| Security | `capability_gate`, `approval_service`, `quota_guard`, `secrets_provider`, `audit_logger`, `mcp_authz`, `subprocess_sandbox`, `agent_identity_lifecycle` |
| Memory | `memory_service`, `conversation_summarizer`, `knowledge_inject`, `extract_agent`, `extract_queue`, `agency_charter`, `decision_trace`, `session_feedback` |
| Integration | `mcp_client`, `mcp_registry_service`, `email_service`, `viking_client`, `interoperability` |
| Multi-tenant | `enterprise_sync`, `org_sync_service`, `sync_service` |
| Office / docs | `office_document_service`, `officecli_adapter`, `text_extractor` |
| Other | `pack_service`, `skill_creator_content`, `token_tracker`, `objective_service`, `autonomy_repair_plan` |

### Memory Control Plane

Hive keeps the T0/T2/T3/soul Markdown memory pyramid, but runtime behavior is governed by a Memory Control Plane. This layer decides what can be stored, what can be activated, and which actions require owner/company confirmation.

| Capability | Primary code paths | Runtime invariant |
|------------|--------------------|-------------------|
| Principal + charter context | `services/agency_charter.py`, `services/principal_context.py` | Memory/action decisions must know direct owner, company, creator/current user, and delegation context when available. |
| Write safety | `memory/write_gate.py`, `memory/t2_store.py`, `tools/handlers/memory.py` | New durable memories must pass privacy/sensitivity classification before T2/T3 persistence; PL4 credentials are rejected. |
| Memory hygiene | `memory/hygiene.py`, `tools/workspace.py`, `scripts/repair_memory_hygiene.py` | Legacy shadow stores and dead stubs are retired through reversible quarantine/backfill paths; no ad hoc workspace surgery. |
| Dynamic activation | `memory/activation.py`, `memory/retriever.py`, `services/memory_service.py`, `runtime/invoker.py` | Prompt memory is selected by owner/company/goal/open-loop relevance and sensitivity access, not by static file inclusion alone. |
| Decision trace + preflight | `services/action_preflight.py`, `services/decision_trace.py`, `tools/service.py` | External-visible, sensitive, irreversible, or company-conflicting tool calls must pass preflight before execution. |
| Session feedback | `services/session_feedback.py`, `models/session_feedback.py`, `api/chat_sessions.py` | Useful/misleading feedback is persisted with tenant/session/agent context and re-enters memory through governed write paths. |
| Coordination primitives | `agents/coordination.py`, `agents/orchestrator.py`, `tools/service.py` | Cross-agent work uses Lease/Signal; confirm-first actions create Checkpoint; Sentinel can emit Signal or Checkpoint. |
| Proactive steward loop | `services/proactive_employee_loop.py`, `services/heartbeat.py`, `memory/policy_replay.py` | Heartbeat may prepare low-risk work, but external-visible action requires Checkpoint and policy tuning requires replay guard. |

### Web Chat Runtime

Web chat runs are durable background tasks:

- `chat_sessions.py` exposes session history and start/active/cancel run APIs.
- `web_chat_runtime.py` creates and executes `RuntimeTask(task_type="web_chat_turn")`.
- `web_chat_broker.py` broadcasts session-scoped runtime events to WebSocket subscribers.
- `websocket.py` is a subscription and compatibility start path; disconnecting the browser must not cancel the run.
- Active-run uniqueness and persisted `RuntimeTask` scanning prevent duplicate web-chat/deep-research runs after process restarts.
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
- DB-backed invocation/generation/tool spans through `record_invocation_span`
- Provider retry/overload fallback, output-cap telemetry, and Anthropic thinking-signature preservation
- Turn-level token budget gates where runtime config provides a budget

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

37 files in `api/domains/` including tests and index, covering agents, enterprise, tools, chat, auth, notifications, files, tasks, skills, relationships, plaza, channels, schedules, admin, activity, users, messages, system, triggers, office, deepResearch, memory, objectives, autonomy, and evolution.

## Conventions

- **Multi-tenancy:** All entities tenant-scoped. PostgreSQL RLS. `check_agent_access()` required.
- **Kernel invariant:** All LLM calls via `invoke_agent()` → `AgentKernel.handle()`. Never direct.
- **Tool governance:** All tool calls via `ToolRuntimeService.execute()`. Never bypass.
- **Code execution provider:** `execute_code` / `run_command` must go through `services/code_execution/`; local/trusted hosts may use `services/subprocess_sandbox.py`, but Railway production must use the external Vercel Sandbox provider. Never inherit host secrets or launch raw `subprocess` from tool handlers.
- **MCP authz:** MCP imports/execution must go through `services/mcp_authz.py`; URL userinfo, `access_token`, and token passthrough credentials are forbidden.
- **Memory write invariant:** Do not write T2/T3 durable memory directly from tools or extractors; use `prepare_memory_write()` or an existing wrapper that calls it.
- **Memory read invariant:** Prompt memory retrieval must preserve `ActivationContext` and sensitivity stripping when current user/owner/company context is known.
- **Memory hygiene invariant:** Legacy memory artifacts are repaired by `memory/hygiene.py` only, with reversible quarantine/backfill reports.
- **Action boundary invariant:** Do not bypass `ActionPreflightService` for external-visible, sensitive, irreversible, or company-boundary actions.
- **Agent creation invariant:** New employee agents must render first-person accountability plus frozen Company Charter and Owner Agency Charter sections in `soul.md`.
- **Coordination invariant:** Duplicate delegation should acquire a Lease; progress/handoff should use Signal; confirm-first work should create Checkpoint metadata.
- **Trace invariant:** Invocation spans are append-only evidence with tenant, agent, user, runtime task, session, request, trace, and parent span join keys.
- **Interoperability invariant:** A2A/interoperability descriptors are contracts, not marketing; unsupported OAuth delegation and JSON-RPC task surfaces must remain `not_exposed`.
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
