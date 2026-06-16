"""Production wiring helpers for CoordinationGateway.

Two ways to put coordination state on PostgreSQL:

  * Per-request: build a `CoordinationRepository(session, tenant_id)`
    inside a request scope where `get_db()` has already set the tenant
    GUC. Pass it as `coordination_gateway` to `delegate_async()` /
    `ToolRuntimeService`.
  * Module-level fallback: instantiate `InProcessCoordinationGateway()`
    once at startup only when `settings.COORDINATION_BACKEND == "memory"`.

`pick_gateway()` is the single decision point: it reads the settings
flag and either returns a PostgreSQL-backed repository (default) when a
tenant scope is available or an explicit in-process gateway for dev/test.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import AsyncIterator, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.coordination_gateway import (
    CoordinationGateway,
    InProcessCoordinationGateway,
)
from app.agents.coordination_repository import CoordinationRepository
from app.config import get_settings
from app.database import tenant_scoped_session

logger = logging.getLogger(__name__)


def pick_gateway(
    *,
    session: AsyncSession | None = None,
    tenant_id: uuid.UUID | None = None,
) -> CoordinationGateway:
    """Choose the right gateway for the current scope.

    Honors `settings.COORDINATION_BACKEND` (default "postgres"). When set
    to "postgres" and the caller provides session + tenant_id, returns a
    `CoordinationRepository`. In every other case, falls back to the
    in-process gateway (which is correct for single-process Hive
    deployments and for unit tests).
    """
    backend = (getattr(get_settings(), "COORDINATION_BACKEND", "postgres") or "postgres").lower()
    if backend == "postgres" and session is not None and tenant_id is not None:
        return CoordinationRepository(session, tenant_id=tenant_id)
    if backend == "postgres":
        logger.warning(
            "COORDINATION_BACKEND=postgres but session=%s tenant_id=%s — falling back to in-process gateway",
            "set" if session is not None else "missing",
            "set" if tenant_id is not None else "missing",
        )
    return InProcessCoordinationGateway()


GatewayFactory = Callable[[AsyncSession, uuid.UUID], Awaitable[CoordinationGateway]]
"""Callable signature for code paths that need a fresh per-request gateway."""


async def gateway_from_session(session: AsyncSession, tenant_id: uuid.UUID) -> CoordinationGateway:
    """Async factory: hand back a `CoordinationGateway` for the given scope."""
    return pick_gateway(session=session, tenant_id=tenant_id)


@contextlib.asynccontextmanager
async def gateway_scope(
    explicit_gateway: CoordinationGateway | None = None,
    *,
    tenant_id: uuid.UUID | str | None = None,
) -> AsyncIterator[CoordinationGateway]:
    """Yield the right gateway for the surrounding call site.

    Decision order:
      1. `explicit_gateway` wins — caller already knows what it wants.
      2. `COORDINATION_BACKEND=postgres` + tenant_id present → open a
         fresh `tenant_scoped_session(tenant_uuid)` (coordination_* are FORCE
         RLS tables — even owner is blocked without the GUC pinned) and yield a
         `CoordinationRepository`. The session is committed on clean exit,
         rolled back on error.
      3. Otherwise → in-process gateway (no session opened).

    Always returns a `CoordinationGateway` so call sites do not need to
    branch on backend choice.
    """
    if explicit_gateway is not None:
        yield explicit_gateway
        return

    backend = (getattr(get_settings(), "COORDINATION_BACKEND", "postgres") or "postgres").lower()
    if backend != "postgres":
        yield InProcessCoordinationGateway()
        return

    if tenant_id is None:
        logger.warning("COORDINATION_BACKEND=postgres but tenant_id is missing — falling back to in-process gateway")
        yield InProcessCoordinationGateway()
        return

    if isinstance(tenant_id, str):
        try:
            tenant_uuid = uuid.UUID(tenant_id)
        except ValueError:
            logger.warning(
                "COORDINATION_BACKEND=postgres but tenant_id=%r is not a valid UUID — falling back",
                tenant_id,
            )
            yield InProcessCoordinationGateway()
            return
    else:
        tenant_uuid = tenant_id

    async with tenant_scoped_session(tenant_uuid) as session:
        try:
            yield CoordinationRepository(session, tenant_id=tenant_uuid)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
