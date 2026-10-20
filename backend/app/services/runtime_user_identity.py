"""Exact live User recovery inside a tenant-scoped trusted runtime."""

from sqlalchemy import select

from app.models.user import User
from app.models.tenant import Tenant


async def load_runtime_user(db, user_id, *, include_inactive=False, lock=False):
    """Default to a live principal; history-only callers can inspect inactive identity."""
    if user_id is None:
        return None
    actor = (await db.scalar(select(User).where(User.id == user_id).with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)) if lock else await db.get(User, user_id, populate_existing=True))
    if actor is not None:
        return actor if include_inactive or actor.is_active else None
    from app.database import enter_rls_bypass

    # An ordinary cross-company identity remains invisible. Only the exact
    # canonical subject's current global role can recover a home-company row.
    async with enter_rls_bypass(db, reason="runtime exact user recovery lookup", actor_id=str(user_id)):
        statement = (select(User, Tenant.is_active).outerjoin(Tenant, Tenant.id == User.tenant_id)
            .where(User.id == user_id, User.role == "platform_admin"))
        if not include_inactive:
            statement = statement.where(User.is_active.is_(True))
        if lock:
            statement = statement.with_for_update(read=True, nowait=True, of=User)
        row = (await db.execute(statement)).first()
        # Match canonical authentication when the principal's home company
        # was disabled after the original work or connection was accepted.
        return row[0] if row is not None and (include_inactive or row[1] is not False) else None
