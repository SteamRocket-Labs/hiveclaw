"""Resolve an agent's tenant via an audited bypass read (RLS migration 阶段1 DD-A).

Chicken-and-egg breaker. Daemons, detached background tasks and the tool
resolver often hold only an ``agent_id`` but need the ``tenant_id`` to open a
``tenant_scoped_session`` for the real work. Reading the ``agents`` table to
learn that tenant itself fail-closes once the app connects as a non-owner role
under enforced RLS — you cannot scope by a tenant you have not read yet. A
single-row, audited ``enter_rls_bypass`` lookup is the sanctioned escape: it
touches exactly one agent row (by primary key) and writes an audit line, so it
cannot become a silent cross-tenant convenience hatch.

This is distinct from ``app.core.tenant_scope.resolve_tenant_scope`` which maps
an authenticated request's ``current_user`` to a tenant; this one resolves a
*background* tenant from an agent identity with no request in scope.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass
from app.models.agent import Agent

_AGENT_TENANT_CACHE_MAX = 4096
_AGENT_TENANT_CACHE: OrderedDict[uuid.UUID, uuid.UUID] = OrderedDict()


def clear_tenant_resolution_cache() -> None:
    """Clear the process-local resolver cache. Intended for tests and reloads."""
    _AGENT_TENANT_CACHE.clear()


def _normalize_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if not value:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _get_cached_agent_tenant(agent_id: uuid.UUID) -> uuid.UUID | None:
    tenant_id = _AGENT_TENANT_CACHE.get(agent_id)
    if tenant_id is not None:
        _AGENT_TENANT_CACHE.move_to_end(agent_id)
    return tenant_id


def _cache_agent_tenant(agent_id: uuid.UUID, tenant_id: uuid.UUID | None) -> None:
    if tenant_id is None:
        return
    _AGENT_TENANT_CACHE[agent_id] = tenant_id
    _AGENT_TENANT_CACHE.move_to_end(agent_id)
    while len(_AGENT_TENANT_CACHE) > _AGENT_TENANT_CACHE_MAX:
        _AGENT_TENANT_CACHE.popitem(last=False)


async def resolve_tenant_for_agent(
    agent_id: uuid.UUID | str | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> uuid.UUID | None:
    """Return the tenant_id owning ``agent_id``, or None if unknown/absent.

    Uses ``enter_rls_bypass`` so it works under enforced RLS (non-owner role),
    where a bare read of ``agents`` would fail closed. ``session_factory`` is
    for tests that hold their own engine (Testcontainers); production callers
    omit it and get the app engine.
    """
    agent_uuid = _normalize_uuid(agent_id)
    if not agent_uuid:
        return None
    use_cache = session_factory is None
    if use_cache:
        cached = _get_cached_agent_tenant(agent_uuid)
        if cached is not None:
            return cached
    factory = session_factory or async_session
    async with factory() as db:
        async with enter_rls_bypass(db, reason=f"tenant resolution for agent {agent_uuid}") as bypass_db:
            result = await bypass_db.execute(select(Agent.tenant_id).where(Agent.id == agent_uuid))
            tenant_id = result.scalar_one_or_none()
            if use_cache:
                _cache_agent_tenant(agent_uuid, tenant_id)
            return tenant_id


async def resolve_tenant_for_plan(
    plan_id: uuid.UUID | str | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> uuid.UUID | None:
    """Return the tenant_id owning ``plan_id`` (agent_plan_requests), or None.

    Stage-2a sibling of :func:`resolve_tenant_for_agent`. ``agent_plan_requests``
    gained an RLS policy in stage-2a, so a ``PlanModeService`` method that holds
    only a ``plan_id`` must learn the owning tenant before it can open a
    ``tenant_scoped_session`` — but reading the plan row to learn that tenant
    itself fail-closes under enforced (non-owner) RLS. A single-row, audited
    ``enter_rls_bypass`` read by primary key is the sanctioned breaker: it
    touches exactly one plan row and writes an audit line, so it cannot become a
    silent cross-tenant convenience hatch.
    """
    if not plan_id:
        return None
    from app.models.plan_request import AgentPlanRequest

    factory = session_factory or async_session
    async with factory() as db:
        async with enter_rls_bypass(db, reason=f"tenant resolution for plan {plan_id}") as bypass_db:
            result = await bypass_db.execute(select(AgentPlanRequest.tenant_id).where(AgentPlanRequest.id == plan_id))
            return result.scalar_one_or_none()
