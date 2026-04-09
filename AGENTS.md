# AGENTS.md

Technical reference for AI coding assistants working with the Hive platform.

## Project Overview

Hive is an open-source **multi-agent collaboration platform** — enterprise "digital employees" with persistent identity, long-term memory, private workspaces, and autonomous trigger-driven execution.

- **Version:** 1.7.0 (tracked in root `VERSION` file)
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
| API Routes | 48 | ~16K | FastAPI routers |
| Models | 31 | ~1.5K | SQLAlchemy ORM (async, RLS) |
| Services | 58 | ~17K | Business logic |
| Tool Domains | 20 | — | Feishu office, messaging, tasks, email |
| Kernel | 3 | ~1.6K | Core LLM execution engine |
| Tools | 11 | ~700 | Handler implementations |
| Skills | 5 | ~310 | Markdown skill system |
| Memory | 5 | ~1K | Semantic memory with FTS |
| Migrations | 35 | — | Alembic schema versions |

### API Routers (48 files)

Core: `agents`, `auth`, `users`, `tenants`, `enterprise`, `admin`
Agent features: `tasks`, `triggers`, `schedules`, `relationships`, `skills`, `files`, `chat_sessions`
Channels: `feishu`, `slack`, `discord_bot`, `dingtalk`, `wecom`, `teams`
Platform: `tools`, `packs`, `capabilities`, `plaza`, `notification`, `websocket`
Enterprise: `organization`, `memory`, `guard_policies`, `feature_flags`, `config_history`
Desktop: `desktop_auth`, `desktop_sync`, `desktop_agents`, `desktop_audit`
Other: `upload`, `webhooks`, `gateway`, `llm_proxy`, `oidc`, `onboarding`, `role_templates`

All routers mounted under `/api` and `/api/v1` prefixes.

### Models (31 files)

Core entities: `User`, `Agent`, `Tenant`, `LLMModel`, `Tool`, `Skill`, `Task`
Agent config: `AgentTrigger`, `AgentSchedule`, `ChannelConfig`, `AgentPermission`, `AgentTemplate`
Relationships: `AgentRelationship`, `AgentAgentRelationship`, `OrgMember`, `OrgDepartment`
Audit: `AuditLog`, `SecurityAuditEvent`, `ChatMessage`, `ChatSession`, `AgentActivityLog`
Platform: `CapabilityPolicy`, `CapabilityInstall`, `GuardPolicy`, `FeatureFlag`, `Notification`
Auth: `RefreshToken`, `InvitationCode`, `Participant`
Social: `PlazaPost`, `PlazaComment`, `PlazaLike`

### Services (58 files)

| Category | Services |
|----------|---------|
| Agent lifecycle | `agent_manager`, `agent_seeder`, `auto_dream`, `auto_provision` |
| LLM | `llm_client` (OpenAI/Anthropic/Gemini/compatible), `llm_utils` |
| Execution | `trigger_daemon` (15s loop), `task_executor`, `scheduler`, `heartbeat` |
| Channels | `feishu_service`, `feishu_ws`, `dingtalk_stream`, `wecom_stream` |
| Tools | `agent_tools`, `agent_tool_assignment_service`, `tool_seeder`, `tool_telemetry` |
| Security | `capability_gate`, `approval_service`, `quota_guard`, `secrets_provider`, `audit_logger` |
| Memory | `memory_service`, `conversation_summarizer`, `knowledge_inject` |
| Integration | `mcp_client`, `mcp_registry_service`, `email_service`, `viking_client` |
| Multi-tenant | `enterprise_sync`, `org_sync_service`, `sync_service` |
| Other | `pack_service`, `skill_creator_content`, `text_extractor`, `token_tracker` |

### Kernel Engine

Stateless LLM loop with dependency injection. Zero DB imports — all I/O via 14 `KernelDependencies` callbacks.

- Max 50 tool rounds per invocation
- Context compaction at 85% window threshold
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
| `plaza` | plaza_get_new_posts, plaza_create_post, plaza_add_comment |
| `skills` | load_skill, tool_search |
| `triggers` | set_trigger, update_trigger, list_triggers, cancel_trigger |
| `hr` | create_digital_employee |
| `mcp` | list_mcp_resources, read_mcp_resource, import_mcp_server |

## Frontend Architecture (`frontend/src/`)

### Pages (17 + 20 sections)

| Page | Route | Purpose |
|------|-------|---------|
| Login | `/login` | Authentication |
| CompanySetup | `/setup-company` | Tenant onboarding |
| Dashboard | `/dashboard` | Agent metrics, activity |
| Plaza | `/plaza` | Agent social feed |
| AgentDetail | `/agents/:id` | Agent management hub (10 tab sections) |
| EnterpriseSettings | `/enterprise/*` | Workspace admin (12 sections) |
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

20 domain adapters in `api/domains/`: agents, enterprise, tools, chat, auth, notifications, files, tasks, skills, relationships, plaza, channels, schedules, admin, activity, users, messages, system, triggers.

## Conventions

- **Multi-tenancy:** All entities tenant-scoped. PostgreSQL RLS. `check_agent_access()` required.
- **Kernel invariant:** All LLM calls via `invoke_agent()` → `AgentKernel.handle()`. Never direct.
- **Tool governance:** All tool calls via `ToolRuntimeService.execute()`. Never bypass.
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
| `TAVILY_API_KEY` | Web search |
| `EXA_API_KEY` | Web search |
| `FIRECRAWL_API_KEY` | Web crawling |
| `XCRAWL_API_KEY` | Web crawling |
