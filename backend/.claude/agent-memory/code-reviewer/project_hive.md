---
name: Hive project architecture
description: Key architecture details for the Hive AI agent management platform - FastAPI backend, PostgreSQL, Redis, T3 MD memory pyramid
type: project
---

Hive is a multi-tenant AI agent management platform.

**Stack**: FastAPI (Python 3.11+ per ruff target, 3.13 runtime observed via .pyc), PostgreSQL (asyncpg), Redis, Nginx frontend proxy, Docker Compose.

**Key files**:
- Config: `backend/app/config.py` (pydantic-settings, env-based, `@lru_cache` singleton)
- Entry: `backend/app/main.py` (lifespan pattern, many background tasks)
- DB: `backend/app/database.py` (async SQLAlchemy, `async_session`)
- Agent model: `backend/app/models/agent.py` (status enum: creating/running/idle/stopped/error)
- Tenant model: `backend/app/models/tenant.py` (multi-tenancy isolation boundary)
- Memory: `backend/app/memory/` — T3 MD pyramid (feedback/knowledge/strategies/blocked/user.md), BM25 in MDBackend, optional HindsightBackend via `backend.py`
- Heartbeat: `backend/app/services/heartbeat.py` — 45-min tick performing T2→T3 curation, T3 normalization, T0 log emission

**Multi-tenancy invariant**: Every entity is tenant-scoped. Queries filter by `tenant_id`. `agent.tenant_id` is a UUID; nullable for legacy agents (check before scoping). Tenants table now has `memory_backend` column (String 32, default/server_default "md", non-null).

**MD-first invariant**: T3 markdown files are the source of truth for agent memory. Any alternative backend (Hindsight, Cognee, etc.) is a derived read-side accelerator that can be rebuilt from MD.

**Why:** Understanding this stack is needed to correctly review crypto, CORS, Docker, tenancy, and memory code changes.

**How to apply:**
- Always check domain enum values match DB enum.
- Check for `print()` vs structured logging.
- Verify Docker socket exposure is intentional.
- For memory changes: verify MD remains authoritative — derived indexes must be rebuildable.
- For tenancy-crossing code: check `tenant_id` is passed from session/token, never trusted from client.
- For heartbeat additions: the tick runs every 45 min but must finish in seconds, never block.
- Alembic chain: `alembic heads` must be single. Current head: `add_tenant_memory_backend_0417` (2026-04-17).
