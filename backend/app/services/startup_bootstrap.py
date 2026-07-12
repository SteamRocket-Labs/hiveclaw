"""Governed process-startup data bootstrap owners."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import enter_rls_bypass
from app.models.tenant import Tenant


async def ensure_default_tenant(db: AsyncSession) -> bool:
    """Ensure the registration fallback tenant exists under audited authority.

    Startup runs through the non-owner ``app_rls`` role.  A single PostgreSQL
    upsert is both the authority-gated write and the multi-replica race fence;
    there is no SELECT→INSERT window and a losing replica remains successful.

    Returns ``True`` only when this call created the row.  The caller owns the
    surrounding transaction commit.
    """
    async with enter_rls_bypass(db, reason="startup default tenant bootstrap") as bypass_db:
        result = await bypass_db.execute(
            insert(Tenant)
            .values(name="Default", slug="default", im_provider="web_only")
            .on_conflict_do_nothing(index_elements=[Tenant.slug])
            .returning(Tenant.id)
        )
        return result.scalar_one_or_none() is not None
