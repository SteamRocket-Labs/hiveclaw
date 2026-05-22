"""Production wiring helpers for CoordinationGateway.

Two ways to put coordination state on PostgreSQL:

  * Per-request: build a `CoordinationRepository(session, tenant_id)`
    inside a request scope where `get_db()` has already set the tenant
    GUC. Pass it as `coordination_gateway` to `delegate_async()` /
    `ToolRuntimeService`.
  * Module-level fallback: instantiate `InProcessCoordinationGateway()`
    once at startup and inject it into the shared `ToolRuntimeService`
    singleton when `settings.COORDINATION_BACKEND == "memory"`.

`pick_gateway()` is the single decision point: it reads the settings
flag and either returns an in-process gateway (default) or a
PostgreSQL-backed repository when a session + tenant_id are available.
"""

from __future__ import annotations

import logging
import uuid
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.coordination_gateway import (
    CoordinationGateway,
    InProcessCoordinationGateway,
)
from app.agents.coordination_repository import CoordinationRepository
from app.config import get_settings

logger = logging.getLogger(__name__)


def pick_gateway(
    *,
    session: AsyncSession | None = None,
    tenant_id: uuid.UUID | None = None,
) -> CoordinationGateway:
    """Choose the right gateway for the current scope.

    Honors `settings.COORDINATION_BACKEND` (default "memory"). When set
    to "postgres" and the caller provides session + tenant_id, returns a
    `CoordinationRepository`. In every other case, falls back to the
    in-process gateway (which is correct for single-process Hive
    deployments and for unit tests).
    """
    backend = (getattr(get_settings(), "COORDINATION_BACKEND", "memory") or "memory").lower()
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
