# Hive Engineering Documentation

Version 1.8.0 | FastAPI + React 19 | Apache 2.0

## Table of Contents

- [System Architecture](#system-architecture)
- [Backend](#backend)
  - [Startup Sequence](#startup-sequence)
  - [Middleware Stack](#middleware-stack)
  - [Agent Kernel Engine](#agent-kernel-engine)
  - [Prompt Assembly](#prompt-assembly)
  - [Tool Governance](#tool-governance)
  - [Tool Packs](#tool-packs)
  - [Trigger Daemon](#trigger-daemon)
  - [LLM Client](#llm-client)
  - [Memory System](#memory-system--4-layer-md-pyramid)
  - [Hook System](#hook-system-runtimehookspy)
  - [HR Agent](#hr-agent--agent-creation-pipeline)
  - [Authentication & Authorization](#authentication--authorization)
  - [Database & Multi-Tenancy](#database--multi-tenancy)
  - [Configuration Reference](#configuration-reference)
- [Frontend](#frontend)
  - [Route Architecture](#route-architecture)
  - [State Management](#state-management)
  - [API Layer](#api-layer)
  - [Design System](#design-system)
  - [Agent Detail Interface](#agent-detail-interface)
  - [Enterprise Settings](#enterprise-settings)
- [Deployment](#deployment)
  - [Docker](#docker)
  - [Railway](#railway)
  - [Entrypoint Script](#entrypoint-script)
- [Channel Integrations](#channel-integrations)

---

## System Architecture

```
                    +-----------------------+
                    |   Frontend (React 19) |
                    |   Vite + TanStack Query|
                    +----------+------------+
                               | /api proxy (:3008 -> :8008)
                               | /ws  WebSocket
                    +----------v------------+
                    |   Backend (FastAPI)    |
                    |   Uvicorn :8008       |
                    +---+------+------+-----+
                        |      |      |
              +---------+  +---+---+  +----------+
              |PostgreSQL|  | Redis |  | Agent FS |
              | (asyncpg)|  |       |  |(/data/)  |
              +----------+  +-------+  +----------+

Background Tasks:
  - Trigger Daemon (15s tick loop)
  - Feishu WebSocket Manager
  - DingTalk Stream Manager
  - WeChat Work Stream Manager
  - SOCKS5 Proxy (Discord, optional)
```

---

## Backend

### Startup Sequence

`main.py` lifespan runs in this order:

1. **Secrets validation** — Warns if `SECRET_KEY` or `JWT_SECRET_KEY` are defaults
2. **Secrets provider init** — `init_secrets_provider(SECRETS_MASTER_KEY)` for API key encryption
3. **Database tables** — `Base.metadata.create_all()` (idempotent)
4. **Seed data** (each step isolated, non-blocking):
   - Default tenant (slug: `"default"`)
   - Legacy path migration (`enterprise_info/` -> `enterprise_info_{tenant_id}/`)
   - Builtin tools registration
   - Resume interrupted async delegations (limit 50)
   - Reconcile orphaned runtime tasks
   - Atlassian Rovo config + tool import
   - Default skills seeding
   - Default agent seeding
5. **Memory hook registration** — `register_memory_hooks()`: 11 handlers (3 log + 2 extract + 6 T0)
6. **Background tasks** (4 async tasks):
   - `trigger_daemon` — evaluates cron/interval/poll/webhook/on_message triggers
   - `feishu_ws_manager.start_all()` — Feishu WebSocket listeners
   - `dingtalk_stream_manager.start_all()` — DingTalk stream listeners
   - `wecom_stream_manager.start_all()` — WeChat Work stream listeners
6. **SOCKS5 proxy** — `ss-local` for Discord API (optional, from env or `/data/ss-nodes.json`)
7. **Shutdown** — Close Redis, close OpenViking client

### Middleware Stack

Applied in this order (executes in reverse for requests):

| Order | Middleware | Purpose |
|-------|-----------|---------|
| 1 | `TraceIdMiddleware` | Generate trace IDs for request tracking |
| 2 | `CORSMiddleware` | Origins from `CORS_ORIGINS`, credentials=True |
| 3 | `TenantMiddleware` | Extract tenant_id from JWT, set `request.state.tenant_id` |

### Agent Kernel Engine

All agent execution flows through a unified kernel (`kernel/engine.py`, 1,505 LOC). The kernel is **stateless** with zero DB imports — all I/O via 14 injected `KernelDependencies` callbacks.

#### Invocation Flow (`runtime/invoker.py`)

```
1. Resolve execution identity (agent_bot vs delegated_user)
2. Resolve runtime config (max_tool_rounds, execution_mode, tenant_id)
3. Build frozen system prompt (agent context + memory snapshot + skill catalog)
4. Create KernelDependencies (14 callbacks for DB, tools, memory, LLM)
5. Call AgentKernel.handle(request)
   -> Multi-round LLM loop (max 50 rounds default)
   -> Per-round: resolve retrieval context, execute tools, stream chunks
   -> Context compaction at 85% window threshold
   -> Tool result eviction: 50KB/result, 200KB/round
6. Return AgentInvocationResult(content, tokens_used, parts)
```

#### Kernel Dependencies

| Callback | Purpose |
|----------|---------|
| `build_system_prompt` | Assemble frozen prefix |
| `resolve_memory_context` | Load memory snapshot |
| `resolve_retrieval_context` | Fetch knowledge + runtime context per round |
| `get_tools` | Get tool definitions for agent |
| `resolve_tool_expansion` | Expand tools on skill/MCP load |
| `create_client` | Instantiate LLM client |
| `execute_tool` | Run a governed tool |
| `maybe_compress_messages` | Compact old messages for token savings |
| `persist_memory` | Save memory to store |
| `record_token_usage` | Log token consumption |
| `apply_vision_transform` | Handle multimodal images |
| `apply_cache_hints` | Anthropic prefix caching |

#### Context Management

| Feature | Value |
|---------|-------|
| Max tool rounds | 50 (configurable per agent) |
| Compaction threshold | 85% of context window |
| Compaction check interval | Every 3 rounds |
| Tool result max size | 50KB per result |
| Round aggregate budget | 200KB per round |
| Microcompaction | Clear tool results > 20 rounds old |
| Prompt-Too-Long retries | 2 retries with reactive compaction |

### Prompt Assembly

Three-layer architecture (`runtime/prompt_builder.py`) with 14 modular prompt sections (`runtime/prompt_sections/`):

```
Layer 1: Frozen Prefix (session-stable, cached)
  - Agent identity (soul contract via agent_context.py)
  - T3 memory injection (feedback, knowledge, strategies, blocked, user — via memory_context.py)
  - Kernel tools catalog
  - Skill catalog
  - Marked with __PROMPT_CACHE_BOUNDARY__

Layer 2: Dynamic Suffix (per-round)
  - Active capability packs
  - Task state + verification rules (tasks.py)
  - Output efficiency rules (output_efficiency.py)
  - Memory save rules (executing_actions.py)
  - Knowledge retrieval results
  - Compaction hints

Layer 3: Conversation Messages
  - User/assistant/tool messages
```

**Prompt sections** (`runtime/prompt_sections/`): `agent_context`, `memory_context`, `tasks`, `executing_actions`, `output_efficiency` — each returns a section string assembled by the builder. Cache boundary separates frozen (soul+memory+tools) from dynamic (tasks+session).

Budget allocation scales with context window. Frozen prefix trimmed if total exceeds budget (dynamic preserved for per-round accuracy).

### Tool Governance

Every tool call passes through `tools/governance.py` (5s timeout, fail-closed):

```
1. Resolve agent security zone
   - "public"     → only safe tools allowed
   - "restricted" → sensitive tools need approval
   - default      → "restricted"

2. Capability gate check (per tenant + agent)
   - denied=True     → BLOCK + audit log
   - escalate_to_l3  → proceed to approval

3. Approval escalation (if required)
   - Create approval request
   - Return "awaiting approval" message
   - Tool NOT executed until approved
```

**Safe tools** (no governance): `list_files`, `read_file`, `load_skill`, `web_fetch`, `web_search`, `read_document`, `list_tasks`, `get_task`

**Sensitive tools** (require governance): `create_digital_employee`, `send_feishu_message`, `send_email`, `delete_file`, `write_file`, `execute_code`, `run_command`, `set_trigger`, `import_mcp_server`, `send_message_to_agent`

### Tool Packs

Static packs defined in `tools/packs.py`:

| Pack | Tools | Activation |
|------|-------|-----------|
| `web_pack` | web_search, web_fetch, firecrawl_fetch, xcrawl_scrape | Via web research skills |
| `feishu_pack` | 24 Feishu tools (docs/wiki/sheets/base/tasks/calendar) | After Feishu channel configured |
| `plaza_pack` | plaza_get_new_posts, plaza_create_post, plaza_add_comment | On-demand |
| `mcp_admin_pack` | discover_resources, import_mcp_server, list/read_mcp_resources | Platform extension |

Dynamic packs generated for each imported MCP server (`mcp_server:{name}`).

### Trigger Daemon

Background loop (`trigger_daemon.py`, 721 LOC):

| Parameter | Value |
|-----------|-------|
| Tick interval | 15 seconds |
| Dedup window | 120 seconds (persisted to JSON) |
| Max fires/hour | 6 per agent |
| Chain depth | Max 5 (prevents A->B->A loops) |
| Min poll interval | 30 minutes |
| Webhook rate limit | 5/minute/token |

**Supported trigger types:**

| Type | Evaluation |
|------|-----------|
| `cron` | croniter with agent timezone |
| `once` | Fires at specific ISO datetime |
| `interval` | Every N minutes (default 30) |
| `poll` | HTTP poll with change detection (SSRF-protected) |
| `on_message` | Agent-to-agent or human message events |
| `webhook` | External POST with payload |

Triggers grouped by agent_id, then agent invoked ONCE with all trigger context. Creates internal "Reflection Session" for trigger execution.

### LLM Client

Unified client (`llm_client.py`, 2,132 LOC) supporting 14+ providers:

| Provider | Protocol | Key Features |
|----------|----------|-------------|
| `anthropic` | Native | Prefix caching, thinking blocks, tool use |
| `openai` | OpenAI-compatible | Streaming, parallel tools |
| `openai-response` | Responses API | Batch endpoint |
| `gemini` | Native | 1M context, function calling |
| `azure` | OpenAI-compatible | Azure AD auth |
| `deepseek` | OpenAI-compatible | Think tag filtering |
| `qwen` | OpenAI-compatible | Alibaba DashScope |
| `minimax` | OpenAI-compatible | MiniMax M2 |
| `openrouter` | OpenAI-compatible | Multi-provider routing |
| `zhipu` | OpenAI-compatible | GLM models |
| `kimi` | OpenAI-compatible | Moonshot |
| `vllm` / `ollama` / `sglang` | OpenAI-compatible | Local inference |
| `custom` | OpenAI-compatible | Any OpenAI-compatible endpoint |

Features: streaming with usage tracking, 429 retry (3x exponential backoff), connection error retry (3x), prompt caching (Anthropic), vision support.

### Memory System — 4-Layer MD Pyramid

MD files are the source of truth. SQLite is demoted to FTS recall index only.

```
T0 (raw logs, 30d)  →  T2 (learnings/*.md)  →  T3 (memory/*.md)  →  soul.md
     ↑ write               ↑ extract               ↑ curate             ↑ dream
SESSION_IDLE/CLOSE   RESPONSE_COMPLETE         Heartbeat (45min)    Dream (4h+3s gate)
  cursor-based         cursor-based             T2→T3 curation      T3→soul consolidation
```

| Layer | Location | Written By | Retention |
|-------|----------|-----------|-----------|
| **T0** | `logs/YYYY-MM-DD/{type}-{HHmm}-{id}.md` | `t0_logger.py` (cursor-based) | 30 days |
| **T2** | `memory/learnings/*.md` | `extract_agent.py` (LLM, per-response) | Until curated |
| **T3** | `memory/feedback.md`, `knowledge.md`, `strategies.md`, `blocked.md`, `user.md` | Heartbeat (KAIROS persistent session) | Until dream dedup |
| **soul.md** | Agent root | Dream consolidation | Permanent |
| **focus.md** | Agent root | Agent + heartbeat | Volatile |

**Key files:**

| File | Purpose |
|------|---------|
| `services/t0_logger.py` | Write T0 MD logs: chat, trigger, delegation, heartbeat, dream |
| `services/extract_agent.py` | LLM extraction T0→T2 (cursor-based, fire-and-forget on RESPONSE_COMPLETE) |
| `services/heartbeat.py` | T2→T3 curation with persistent session across ticks (45min interval) |
| `services/auto_dream.py` | T3→soul consolidation (gate: 4h since last + 3 sessions since last) |
| `memory/retriever.py` | Read T3 MD files directly into prompt; sqlite for recall-only FTS |
| `memory/assembler.py` | Build memory context string for system prompt injection |
| `runtime/hooks_setup.py` | Hook handlers: T0 writers (cursor), extraction triggers, drain on close |

**T0 cursor dedup:** SESSION_IDLE and SESSION_CLOSE track a per-session cursor into the messages list, writing only new messages since the last T0 write. Safe across WebSocket reconnects — the cursor persists in memory.

**Extraction cursor:** `extract_agent.py` maintains a separate cursor per agent+session. RESPONSE_COMPLETE advances it. No extraction on SESSION_IDLE (aligned with Claude Code — CC has no idle-triggered extraction).

### Hook System (`runtime/hooks.py`)

15-event lifecycle bus for memory pipeline and tool governance. Handlers registered in `hooks_setup.py` during startup.

| Category | Events | Handler |
|----------|--------|---------|
| Session | `SESSION_START` | Log + reset extractor cursor |
| | `RESPONSE_COMPLETE` | Fire-and-forget LLM extraction to T2 |
| | `SESSION_IDLE` | Incremental T0 write (cursor-based) |
| | `SESSION_CLOSE` | Drain extractor + incremental T0 write |
| Tool | `PRE_TOOL_USE` | Governance preflight (can block) |
| | `POST_TOOL_USE`, `POST_TOOL_FAILURE` | Audit logging |
| Compression | `PRE_COMPACTION` | Synchronous extraction before context lost |
| | `POST_COMPACTION` | Logging |
| Delegation | `DELEGATION_START`, `DELEGATION_END` | Logging + T0 write |
| Hive-specific | `TRIGGER_END` | T0 trigger log |
| | `HEARTBEAT_TICK_END` | T0 heartbeat log |
| | `DREAM_END` | T0 dream log + reset heartbeat session |
| Notification | `MEMORY_EXTRACTED` | Debug/monitoring |

**Hook mechanics:** `HookRegistry` (global singleton) dispatches events to registered async handlers. PRE_TOOL_USE can return `HookResult(block=True)` to prevent tool execution. All other events are fire-and-forget.

### HR Agent — Agent Creation Pipeline

HR agent (`hr_agent_template/`) creates new agents through a 2-3 round conversational flow with LLM soul refinement.

**Creation flow:**

```
HR conversation → create_digital_employee tool call
                      ↓
                _refine_soul_inputs() — LLM refinement
                  Input: raw name, role_description, personality, boundaries
                  Prompt: full 4-layer architecture awareness, soul-vs-focus boundary,
                          BAD/GOOD examples for each field
                  Output: role_description (3-5 sentences), personality (4-6 behaviors),
                          boundaries (3-5 risk-specific rules), quality_standards,
                          primary_users, core_outputs, first_tasks
                      ↓
                _render_agent_soul_from_blueprint() — structured template
                  soul.md: Identity / Who I Serve / Core Outputs / Operating Style /
                           Quality Standard / Boundaries / How I Learn
                      ↓
                _render_focus_from_blueprint() — operational priorities
                  focus.md: Mission / First 3 Tasks / Setup Debt / Active Triggers
                  (NO capabilities, NO tool routing — system prompt handles those dynamically)
```

**Soul-vs-focus boundary rule:** If it changes when a skill is installed or a trigger is added, it belongs in focus.md, not soul.md. Soul is permanent identity; focus is volatile operations.

**Tools:** `create_digital_employee` (governance: sensitive), `preview_agent_blueprint` (governance: safe). HR agent class: `internal_system`. Created lazily per tenant via `GET /agents/system/hr`.

### Authentication & Authorization

#### JWT Tokens (`core/security.py`)

| Parameter | Value |
|-----------|-------|
| Algorithm | HS256 |
| Access token expiry | 24 hours |
| Refresh token expiry | 30 days |
| Password hashing | bcrypt with auto-salt |
| Refresh token storage | SHA-256 hash only |

JWT payload: `{ sub: user_id, role: "member"|"org_admin"|"platform_admin", exp, tid: tenant_id }`

#### Permission Model (`core/permissions.py`)

`check_agent_access(db, user, agent_id)` returns `(agent, "manage"|"use")`:

1. Platform admin -> MANAGE any agent
2. Creator check -> MANAGE own agents
3. Explicit `AgentPermission` rows (scope: company/user/department)
4. RBAC policies (action: manage/execute/read, principal: user/department)
5. Tenant boundary -> 404 for cross-tenant access

### Database & Multi-Tenancy

#### Connection Pool (`database.py`)

| Parameter | Value |
|-----------|-------|
| Pool size | 20 |
| Max overflow | 10 |
| Total capacity | 30 connections |
| Driver | asyncpg |
| ORM | SQLAlchemy 2.0 async |

#### Row-Level Security

```python
# TenantMiddleware extracts tenant_id from JWT
# get_db() sets PostgreSQL session variable:
await session.execute("SET LOCAL app.current_tenant_id = '{tenant_id}'")
# PostgreSQL RLS policies enforce:
# WHERE tenant_id = current_setting('app.current_tenant_id')::uuid
```

`SET LOCAL` scopes to current transaction. UUID validation prevents injection.

### Configuration Reference

All settings from `config.py` (env-based):

| Setting | Default | Purpose |
|---------|---------|---------|
| `DATABASE_URL` | `postgresql+asyncpg://hive:hive@localhost:5432/hive` | PostgreSQL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis |
| `SECRET_KEY` | `change-me-in-production` | Session secret |
| `JWT_SECRET_KEY` | `change-me-jwt-secret` | JWT signing |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | 1440 (24h) | Token expiry |
| `AGENT_DATA_DIR` | `/data/agents` or `~/.hive/data/agents` | Agent workspace |
| `SECRETS_MASTER_KEY` | `""` | API key encryption |
| `FEISHU_APP_ID` | `""` | Feishu OAuth |
| `FEISHU_APP_SECRET` | `""` | Feishu OAuth |
| `FEISHU_CLI_ENABLED` | `false` | lark-cli integration |
| `CORS_ORIGINS` | `["http://localhost:3000", "http://localhost:5173"]` | Allowed origins |
| `TAVILY_API_KEY` | `""` | Web search |
| `EXA_API_KEY` | `""` | Web search |
| `FIRECRAWL_API_KEY` | `""` | Web crawling |
| `XCRAWL_API_KEY` | `""` | Web crawling |
| `OPENVIKING_URL` | `""` | Knowledge backbone |
| `DOCKER_NETWORK` | `hive_network` | Docker networking |
| `OPENCLAW_IMAGE` | `openclaw:local` | Remote agent image |
| `DEBUG` | `false` | Debug mode |

---

## Frontend

### Route Architecture

```
/login                          → Login (public)
/setup-company                  → CompanySetup (public)

/ (ProtectedRoute)              → AppLayout
  /dashboard                    → Dashboard
  /plaza                        → Agent Plaza
  /agents/new                   → AgentCreate (redirects to HR agent)
  /agents/:id                   → AgentDetail (11 tabs)
  /agents/:id/chat              → Chat
  /messages                     → Messages

/enterprise (WorkspaceGuard)    → WorkspaceLayout
  /info                         → Company info
  /llm                          → LLM model management
  /memory                       → Memory config
  /hr                           → HR Agent
  /tools                        → Tools registry
  /skills                       → Skills library
  /quotas                       → Usage quotas
  /users                        → User management
  /org                          → Org structure
  /approvals                    → Approval workflows
  /audit                        → Audit logs
  /invitations                  → Invite codes

/admin (AdminGuard)             → AdminLayout
  /platform-settings            → Platform admin
```

**Guards:**
- `ProtectedRoute` — requires token + tenant_id
- `WorkspaceGuard` — requires org_admin or platform_admin
- `AdminGuard` — requires platform_admin

All pages lazy-loaded with `React.lazy()` + `Suspense`.

### State Management

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Server state | TanStack React Query 5 | API data with caching, retry: 1, no refetch on focus |
| Client state | Zustand 5 | Auth (user/token) + UI (sidebar, selection) |
| Persistence | localStorage | Token, tenant_id, theme, accent color |

### API Layer

Core HTTP abstraction in `api/core/request.ts`:

- Auto-injects `Authorization: Bearer {token}` and `X-Tenant-Id` headers
- 401 -> clears auth, redirects to `/login`
- Error parsing with Pydantic validation detail support
- 20 typed domain adapters in `api/domains/`

### Design System

CSS custom properties (`index.css`, 900+ lines):

| Token | Dark | Light |
|-------|------|-------|
| `--bg-primary` | `#0a0a0f` | `#ffffff` |
| `--bg-secondary` | `#111119` | `#f8f8fa` |
| `--text-primary` | `#e1e1e8` | `#1a1a22` |
| `--accent-primary` | `#e1e1e8` | `#3a3a42` |
| `--border-default` | `#26263a` | `#e5e5ea` |

Font: Inter (body), JetBrains Mono (code). Spacing: 4px grid. Shadows scale by elevation.

Components: `.btn`, `.card`, `.badge`, `.tabs`, `.status-dot` (animated), `.agent-avatar`.

Dark/light mode via `data-theme` attribute, stored in localStorage.

### Agent Detail Interface

11 tabs at `/agents/:id`:

| Tab | Features |
|-----|----------|
| status | 3-column metrics, capability installs, activity snippet |
| aware | Trigger list (5s poll), focus.md viewer, reflection sessions |
| mind | Memory browser (T3 files, editable) + Evolution browser (lineage/scorecard) |
| tools | Tool catalog, install/remove |
| skills | Installed skills, management |
| relationships | Inter-agent relationship editor |
| workspace | Shared folder config |
| chat | Session list, WebSocket real-time messages, file browser |
| activityLog | Audit trail (10s refresh), tool failures |
| approvals | Pending approval cards, approve/reject |
| settings | Model selection, quotas, heartbeat, timezone, execution mode |

### Enterprise Settings

13 workspace admin sections at `/enterprise/*`:

info, llm (14+ providers), memory, hr, tools, skills, quotas, users, org, approvals, audit, invitations, notifications

---

## Deployment

### Docker

```yaml
# docker-compose.yml
services:
  postgres:   # PostgreSQL 15, port 5432
  redis:      # Redis 7, port 6379
  backend:    # FastAPI, port 8000 (internal)
  frontend:   # Nginx + React, port 3008
```

Backend Dockerfile: Python 3.12-slim, multi-stage (deps + production), Node.js 20 + lark-cli, non-root user `hive`, healthcheck on `/api/health`.

Frontend Dockerfile: Node 20 Alpine build -> nginx Alpine, SPA routing via `try_files`, API proxy to `backend:8000`, security headers (CSP, X-Frame-Options, X-Content-Type-Options).

### Railway

`railway.json`: Dockerfile-based build, restart ON_FAILURE (max 10 retries).

Services: backend, frontend, Postgres, Redis.

### Entrypoint Script

`entrypoint.sh` runs before uvicorn:

1. Fix volume permissions (Railway mounts as root)
2. Configure Git for HTTPS
3. Create/verify DB tables (`Base.metadata.create_all`)
4. Apply idempotent column patches (`ALTER TABLE IF NOT EXISTS`)
5. Run `alembic upgrade head`
6. Auto-authenticate lark-cli (if credentials available)
7. Start uvicorn on `:8000` as non-root user

---

## Channel Integrations

| Channel | Files | Connection | Features |
|---------|-------|-----------|----------|
| Feishu/Lark | `api/feishu.py`, `services/feishu_service.py`, `services/feishu_ws.py`, 8 tool domain files | WebSocket / Webhook | Chat, OAuth SSO, Docs, Wiki, Sheets, Base, Tasks, Calendar, approval cards |
| Slack | `api/slack.py` | Bot API | Chat |
| Discord | `api/discord_bot.py` | Bot Gateway | Chat (SOCKS5 proxy support) |
| DingTalk | `api/dingtalk.py`, `services/dingtalk_stream.py` | Stream SDK | Chat |
| WeChat Work | `api/wecom.py`, `services/wecom_stream.py` | WebSocket / Webhook | Chat (AES-CBC encrypted) |
| Microsoft Teams | `api/teams.py` | Bot Framework | Chat |

Channel configs stored per-agent in `channel_configs` table. Each channel has its own router and streaming manager. Feishu has the deepest integration with 24 office tools.
