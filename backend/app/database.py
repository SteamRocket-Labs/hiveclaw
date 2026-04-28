"""Database connection and session management."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=20,
    max_overflow=10,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Context variable to carry the current tenant_id through the request lifecycle.
# Set by get_db() from request.state.tenant_id (populated by TenantMiddleware).
_current_tenant_id: ContextVar[str | None] = ContextVar("_current_tenant_id", default=None)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""

    pass


def set_current_tenant(tenant_id: str | None) -> None:
    """Set tenant context (called by TenantMiddleware)."""
    _current_tenant_id.set(tenant_id)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting async database sessions.

    Reads tenant_id from contextvar (set by TenantMiddleware) and sets
    PostgreSQL session-level variable for Row-Level Security policies.
    """
    tenant_id = _current_tenant_id.get()

    async with async_session() as session:
        try:
            # Set tenant context for PostgreSQL RLS policies.
            # Note: SET LOCAL does not support parameterized queries in PostgreSQL,
            # so we validate the tenant_id as UUID before interpolation to prevent injection.
            if tenant_id:
                import uuid as _uuid
                _uuid.UUID(str(tenant_id))  # Raises ValueError if not a valid UUID
                await session.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_id}'"))
            else:
                await session.execute(text("SET LOCAL app.current_tenant_id = ''"))

            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_current_tenant_id() -> str | None:
    """Get the current tenant_id from context (for use outside request scope)."""
    return _current_tenant_id.get()


# ── P1-W3-7 — RLS BYPASS auditing ─────────────────────────────
# The RLS policy on tenant tables allows two escape hatches: a session
# GUC value of 'BYPASS' and tenant_id IS NULL. Both are intentional but
# need explicit, auditable entry points so a stray `SET LOCAL ... =
# 'BYPASS'` somewhere can't quietly disable cross-tenant isolation.
#
# `enter_rls_bypass` is the *only* sanctioned path: it requires a typed
# reason, logs to the audit pipeline, and yields a session that already
# has the GUC set. Direct interpolation of 'BYPASS' anywhere else in the
# codebase is forbidden (enforced by tests/api/test_rls_bypass_audit.py).

import contextlib  # noqa: E402  (placed near usage to keep ordering local)
from collections.abc import AsyncIterator  # noqa: E402


@contextlib.asynccontextmanager
async def enter_rls_bypass(
    session: AsyncSession,
    *,
    reason: str,
    actor_id: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """Open an RLS-bypass scope on `session`. Audit log is written first.

    Usage is deliberately verbose so future readers see the intent at the
    call site:

        async with enter_rls_bypass(db, reason="platform-admin migration") as bypass_db:
            await bypass_db.execute(...)

    The GUC is reset on exit (success or failure). `reason` cannot be
    empty — that's how we keep operators from silently using the escape
    hatch as a convenience hack.
    """
    if not reason or not reason.strip():
        raise ValueError("enter_rls_bypass requires a non-empty `reason` for audit purposes")

    logger.warning(
        "[RLS] Entering BYPASS scope — reason=%r actor=%r. "
        "Cross-tenant data is now visible on this session.",
        reason,
        actor_id,
    )
    await session.execute(text("SET LOCAL app.current_tenant_id = 'BYPASS'"))
    try:
        yield session
    finally:
        # Restore tenant scoping. ContextVar fallback covers the case
        # where the session entered without a tenant set.
        tenant_id = _current_tenant_id.get()
        if tenant_id:
            import uuid as _uuid

            try:
                _uuid.UUID(str(tenant_id))
                await session.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_id}'"))
            except (ValueError, Exception) as exc:
                logger.error("[RLS] Failed to restore tenant scope after BYPASS: %s", exc)
                await session.execute(text("SET LOCAL app.current_tenant_id = ''"))
        else:
            await session.execute(text("SET LOCAL app.current_tenant_id = ''"))
        logger.info("[RLS] Exited BYPASS scope (reason=%r)", reason)
