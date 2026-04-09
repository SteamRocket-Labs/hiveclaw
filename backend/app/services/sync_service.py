"""Desktop sync version management (ARCHITECTURE.md §6.6).

The tenant-level sync_version is bumped whenever any Desktop-visible resource
changes (Agent definitions, Role Templates, Guard policies, LLM defaults).
Desktop polls /api/desktop/sync?v={n} and only re-fetches when the version advances.
"""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant


async def bump_sync_version(db: AsyncSession, tenant_id) -> int:
    """Atomically increment sync_version for a tenant.

    Returns the new version number.  Uses UPDATE ... RETURNING to avoid a
    separate SELECT round-trip.
    """
    stmt = (
        update(Tenant)
        .where(Tenant.id == tenant_id)
        .values(sync_version=Tenant.sync_version + 1)
        .returning(Tenant.sync_version)
    )
    result = await db.execute(stmt)
    new_version = result.scalar_one()
    return new_version
