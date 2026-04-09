---
name: Clawith project architecture
description: Key architecture details for the Clawith AI agent management platform - FastAPI backend, Nginx frontend, PostgreSQL, Redis
type: project
---

Clawith is a multi-tenant AI agent management platform.

**Stack**: FastAPI (Python 3.12+, asyncpg), PostgreSQL, Redis, Nginx frontend proxy, Docker Compose orchestration.

**Key files**:
- Config: `backend/app/config.py` (pydantic-settings, env-based)
- Entry: `backend/app/main.py` (lifespan pattern, many background tasks)
- DB: `backend/app/database.py` (async SQLAlchemy)
- Agent model: `backend/app/models/agent.py` (status enum: creating/running/idle/stopped/error)

**Why:** Understanding this stack is needed to correctly review crypto, CORS, Docker, and domain code changes.

**How to apply:** Always check that domain enum values match the DB enum. Check for print() vs structured logging. Verify Docker socket exposure is intentional.
