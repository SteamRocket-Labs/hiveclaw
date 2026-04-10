# Hive

Open-source multi-agent collaboration platform for enterprise. Build persistent "digital employees" with identity, long-term memory, private workspaces, and autonomous execution.

## Features

- **Persistent Agent Identity** — Each agent has a soul contract, focus priorities, memory, skills, and workspace that persist across conversations
- **4-Layer Memory Pyramid** — T0 raw logs → T2 episodic learnings → T3 semantic memory → soul.md identity, with automatic extraction, heartbeat curation, and dream consolidation
- **15-Event Hook System** — Lifecycle event bus powering the memory pipeline: per-response extraction, cursor-based T0 writes, session idle/close handling
- **HR Agent with LLM Soul Refinement** — Conversational agent creation in 2-3 rounds; LLM crafts rich, role-specific soul contracts with system-aware prompts
- **6-Channel Integration** — Feishu/Lark, Slack, Discord, DingTalk, WeChat Work, Microsoft Teams
- **Tool Governance** — Security zones, capability policies, and human-in-the-loop approval flows
- **Autonomous Triggers** — Cron, interval, webhook, poll, and event-driven execution
- **Multi-Tenant** — Full tenant isolation with PostgreSQL RLS, per-tenant LLM pools, quotas, and org sync
- **Skill System** — Markdown-based skill definitions with progressive loading and on-demand tool pack activation
- **Agent Collaboration** — Agent-to-agent delegation, social plaza, and shared workspaces
- **60+ Built-in Tools** — File I/O, web search, document processing, Feishu office suite, email, MCP integration

## Quick Start

```bash
# Clone and setup
git clone https://github.com/rocky2431/hive-agents.git && cd hive-agents
bash setup.sh --dev

# Start services
bash restart.sh
# Frontend → http://localhost:3008
# Backend  → http://localhost:8008
```

Or with Docker:

```bash
cp .env.example .env
docker compose up -d --build    # Full stack → http://localhost:3008
```

## Architecture

```
Frontend (React 19 + Vite + TanStack Query)
    |  /api proxy (:3008 -> :8008)
    v
Backend (FastAPI + SQLAlchemy async)
    |
    v
PostgreSQL (asyncpg) + Redis
```

### Agent Kernel

All agent execution flows through a unified kernel:

```
Entry Points (WebSocket, Feishu, Slack, Trigger, Heartbeat, Delegation)
    -> runtime/invoker.py    (resolve deps, build prompt)
    -> kernel/engine.py      (stateless LLM loop, DI-based)
    -> tools/service.py      (governed tool execution)
    -> tools/governance.py   (security zone -> capability gate -> approval)
```

### Memory Pipeline

```
T0 (raw logs) -> T2 (learnings) -> T3 (memory) -> soul.md
     ^                ^                ^               ^
SESSION_IDLE    RESPONSE_COMPLETE   Heartbeat        Dream
cursor-based    per-response LLM    45min curation   4h+3s gate
```

MD files are the source of truth; SQLite is a recall-only FTS index.

### Backend

| Layer | Count | Purpose |
|-------|-------|---------|
| API Routes | 48 | FastAPI routers (agents, auth, chat, enterprise, channels, admin) |
| Models | 31 | SQLAlchemy ORM (async, tenant-scoped, RLS) |
| Services | 58 | Business logic (LLM client, trigger daemon, channel streaming) |
| Tool Handlers | 60+ | File I/O, web search, Feishu office, email, MCP |
| Migrations | 35 | Alembic schema evolution |

### Frontend

| Layer | Purpose |
|-------|---------|
| 17 Pages | Dashboard, Agent Detail, Plaza, Enterprise Settings, Admin |
| 20 API Domains | Typed HTTP adapters per feature |
| State | TanStack Query (server) + Zustand (UI) |
| i18n | English + Chinese |
| Tests | 14 Vitest suites |

## Channel Integrations

| Channel | Connection | Features |
|---------|-----------|----------|
| Feishu/Lark | WebSocket / Webhook | Chat, Docs, Wiki, Sheets, Base, Tasks, Calendar |
| Slack | Bot API | Chat |
| Discord | Bot Gateway | Chat (with SOCKS5 proxy support) |
| DingTalk | Stream SDK | Chat |
| WeChat Work | WebSocket / Webhook | Chat (AES-CBC encrypted) |
| Microsoft Teams | Bot Framework | Chat |

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0, asyncpg |
| Frontend | React 19, TypeScript 5, Vite 6, React Router 7 |
| Database | PostgreSQL 15 (RLS), Redis 7 |
| LLM | OpenAI, Anthropic, Gemini, OpenAI-compatible |
| Deployment | Docker, Railway |
| License | Apache 2.0 |

## Development

```bash
# Backend
cd backend && source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8008 --reload
ruff check app/ --fix && ruff format app/
pytest

# Frontend
cd frontend
npm run dev        # Dev server on :3008
npm run build      # Production build

# Migrations
cd backend
alembic upgrade head
alembic revision --autogenerate -m "description"
```

## License

[Apache License 2.0](LICENSE)
