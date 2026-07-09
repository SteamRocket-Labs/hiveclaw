"""Runtime tenant admission for background agent execution paths."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.runtime.tenant_admission import RuntimeTenantAdmission, blocked_runtime_tenant_admission
from app.services.tenant_resolver import resolve_tenant_for_agent

TenantResolver = Callable[..., Awaitable[uuid.UUID | None]]


def _coerce_agent_id(agent_id: uuid.UUID | str | None) -> uuid.UUID | None:
    if isinstance(agent_id, uuid.UUID):
        return agent_id
    try:
        return uuid.UUID(str(agent_id))
    except (TypeError, ValueError, AttributeError):
        return None


async def admit_agent_runtime_tenant(
    agent_id: uuid.UUID | str | None,
    *,
    source: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    tenant_resolver: TenantResolver | None = None,
) -> RuntimeTenantAdmission:
    """Resolve the tenant precondition before a background runtime mutates state."""

    normalized_agent_id = _coerce_agent_id(agent_id)
    normalized_source = str(source or "runtime").strip() or "runtime"
    if normalized_agent_id is None:
        return blocked_runtime_tenant_admission(
            reason_code="agent_id_missing",
            message=f"{normalized_source} runtime is blocked because agent_id is missing or invalid.",
            source=normalized_source,
            agent_id=None,
        )

    resolver = tenant_resolver or resolve_tenant_for_agent
    tenant_id = await resolver(normalized_agent_id, session_factory=session_factory)
    if tenant_id is None:
        return blocked_runtime_tenant_admission(
            reason_code="agent_tenant_missing",
            message=f"{normalized_source} runtime is blocked because agent {normalized_agent_id} has no tenant.",
            source=normalized_source,
            agent_id=normalized_agent_id,
        )

    return RuntimeTenantAdmission(
        ok=True,
        tenant_id=tenant_id,
        status="allowed",
        reason_code="tenant_resolved",
        message=f"{normalized_source} runtime tenant resolved.",
        agent_id=normalized_agent_id,
        source=normalized_source,
    )
