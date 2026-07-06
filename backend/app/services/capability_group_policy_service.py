"""Runtime capability-group policy facade.

The persisted storage is still the legacy pack policy setting during migration.
Runtime code should depend on capability groups so tool authorization does not
expose packs as the user/model-facing permission concept.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.governance_capability_taxonomy import taxonomy_policy_capability_group_names_for_tool
from app.services.pack_policy_service import (
    get_agent_pack_policies,
    get_tenant_pack_policies,
    is_pack_enabled,
    set_tenant_pack_policy,
    tenant_pack_policy_key,
)


def tenant_capability_group_policy_key(tenant_id: uuid.UUID) -> str:
    """Return the migration-compatible backing key for tenant policies."""
    return tenant_pack_policy_key(tenant_id)


async def get_tenant_capability_group_policies(db: AsyncSession, tenant_id: uuid.UUID | None) -> dict[str, bool]:
    """Return tenant capability-group policies from the legacy pack backing store."""
    return await get_tenant_pack_policies(db, tenant_id)


async def get_agent_capability_group_policies(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
) -> dict[str, bool]:
    """Return agent-visible capability-group policies.

    Agent plugin assignments and tenant overrides are still resolved by the
    migration-compatible pack policy layer, but Runtime callers should only use
    this capability-group facade.
    """
    return await get_agent_pack_policies(db, tenant_id, agent_id)


async def set_tenant_capability_group_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    capability_group_name: str,
    *,
    enabled: bool,
) -> dict[str, bool]:
    """Persist a tenant capability-group override in the legacy pack backing store."""
    return await set_tenant_pack_policy(db, tenant_id, capability_group_name, enabled=enabled)


def is_capability_group_enabled(
    capability_group_policies: dict[str, bool],
    capability_group_name: str,
) -> bool:
    """Return whether a capability group is enabled for the current Runtime surface."""
    return is_pack_enabled(capability_group_policies, capability_group_name)


def policy_capability_group_names_for_tool(tool_name: str) -> tuple[str, ...]:
    """Return policy-relevant capability group names for a tool."""
    return taxonomy_policy_capability_group_names_for_tool(tool_name)
