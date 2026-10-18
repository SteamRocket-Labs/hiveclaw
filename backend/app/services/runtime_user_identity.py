"""Exact live User recovery inside a tenant-scoped trusted runtime."""

from sqlalchemy import select

from app.models.user import User
from app.models.tenant import Tenant


async def load_runtime_user(db, user_id):
    if user_id is None:
        return None
    actor = await db.get(User, user_id, populate_existing=True)
    if actor is not None:
        return actor if actor.is_active else None
    from app.database import enter_rls_bypass

    # An ordinary cross-company identity remains invisible. Only the exact
    # canonical subject's current global role can recover a home-company row.
    async with enter_rls_bypass(db, reason="runtime exact user recovery lookup", actor_id=str(user_id)):
        row = (
            await db.execute(
                select(User, Tenant.is_active)
                .outerjoin(Tenant, Tenant.id == User.tenant_id)
                .where(User.id == user_id, User.role == "platform_admin", User.is_active.is_(True))
            )
        ).first()
        # Match canonical authentication when the principal's home company
        # was disabled after the original work or connection was accepted.
        return row[0] if row is not None and row[1] is not False else None
