"""Database connection and session management."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from contextvars import ContextVar, Token

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session as SyncSession

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _normalize_async_url(url: str) -> str:
    """Coerce a bare ``postgresql://`` URL (e.g. a Railway ``${{Postgres.DATABASE_URL}}``
    reference) to the ``+asyncpg`` driver the async engine requires. Both the app
    engine and the schema engine must use it — entrypoint runs schema steps with
    DATABASE_URL set to the (possibly bare) SCHEMA_URL."""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _engine_pool_kwargs(settings) -> dict[str, int]:
    return {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
    }


engine = create_async_engine(
    _normalize_async_url(settings.DATABASE_URL),
    echo=settings.DEBUG,
    **_engine_pool_kwargs(settings),
)


def snapshot_db_pool() -> dict:
    """Point-in-time occupancy of the shared connection pool (health surface).

    `overflow` is SQLAlchemy's raw counter — negative until the base pool has
    been fully populated once.
    """
    pool = engine.pool
    size = pool.size()
    capacity = size + settings.DB_MAX_OVERFLOW
    checked_out = pool.checkedout()
    return {
        "size": size,
        "checked_out": checked_out,
        "checked_in": pool.checkedin(),
        "overflow": pool.overflow(),
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout_seconds": settings.DB_POOL_TIMEOUT,
        "capacity": capacity,
        "saturation_pct": round(100.0 * checked_out / max(1, capacity), 1),
    }


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Owner-role engine for schema/DDL work (create_all, RLS policies, GRANTs).
# Pre-cutover SCHEMA_DATABASE_URL is unset → reuse the app engine (no second
# pool). After the stage-3 role flip the app engine connects as the non-owner
# app_rls role (NOSUPERUSER — cannot run DDL), so schema ops route through this
# owner connection instead.
_schema_url = _normalize_async_url(settings.SCHEMA_DATABASE_URL or settings.DATABASE_URL)
schema_engine = (
    engine
    if _schema_url == _normalize_async_url(settings.DATABASE_URL)
    else create_async_engine(_schema_url, echo=settings.DEBUG, pool_size=2, max_overflow=2)
)

# Context variable to carry the current tenant_id through the request lifecycle.
# Set by get_db() from request.state.tenant_id (populated by TenantMiddleware).
_current_tenant_id: ContextVar[str | None] = ContextVar("_current_tenant_id", default=None)
_RLS_TENANT_INFO_KEY = "hive_rls_tenant_id"


def _normalize_rls_tenant_value(tenant_id: str | uuid.UUID | None) -> str:
    """Return a validated tenant id string, or empty string for fail-closed scope."""
    if tenant_id:
        return str(uuid.UUID(str(tenant_id)))
    return ""


def _set_rls_tenant_context(session: AsyncSession, tenant_id: str | uuid.UUID | None) -> str:
    """Attach tenant scope to an ORM session so every new transaction re-pins RLS."""
    normalized = _normalize_rls_tenant_value(tenant_id)
    sync_session = getattr(session, "sync_session", None)
    if sync_session is not None:
        sync_session.info[_RLS_TENANT_INFO_KEY] = normalized
    return normalized


def _rls_tenant_statement(tenant_id: str) -> str:
    if tenant_id:
        return f"SET LOCAL app.current_tenant_id = '{tenant_id}'"
    return "SET LOCAL app.current_tenant_id = ''"


@event.listens_for(SyncSession, "after_begin")
def _apply_rls_tenant_for_transaction(session: SyncSession, _transaction, connection) -> None:
    """Re-apply transaction-local RLS tenant context after explicit commits.

    PostgreSQL ``SET LOCAL`` is cleared on every COMMIT. Channel runtimes save a
    user turn, commit it, then continue with the same ORM session to start a
    durable run and persist the assistant reply. Without this hook the second
    transaction runs fail-closed under enforced RLS and the agent appears
    missing even though it exists.
    """
    if _RLS_TENANT_INFO_KEY not in session.info:
        return
    tenant_id = _normalize_rls_tenant_value(session.info.get(_RLS_TENANT_INFO_KEY))
    connection.exec_driver_sql(_rls_tenant_statement(tenant_id))


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""

    pass


def set_current_tenant(tenant_id: str | None) -> Token[str | None]:
    """Set tenant context (called by TenantMiddleware)."""
    return _current_tenant_id.set(tenant_id)


def reset_current_tenant(token: Token[str | None]) -> None:
    """Restore tenant context after a request or scoped background session."""
    _current_tenant_id.reset(token)


async def pin_rls_tenant_context(session: AsyncSession, tenant_id: str | uuid.UUID | None) -> uuid.UUID | None:
    """Pin the current transaction and future transactions on this ORM session to a tenant."""
    pinned_tenant_id = _set_rls_tenant_context(session, tenant_id)
    _current_tenant_id.set(pinned_tenant_id or None)
    await session.execute(text(_rls_tenant_statement(pinned_tenant_id)))
    return uuid.UUID(pinned_tenant_id) if pinned_tenant_id else None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting async database sessions.

    Reads tenant_id from contextvar (set by TenantMiddleware) and sets
    PostgreSQL session-level variable for Row-Level Security policies.
    """
    tenant_id = _current_tenant_id.get()

    async with async_session() as session:
        try:
            # Set tenant context for PostgreSQL RLS policies.
            # Note: SET LOCAL is transaction-local. The session info hook below
            # re-applies it automatically if request code commits mid-handler.
            await pin_rls_tenant_context(session, tenant_id)

            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_current_tenant_id() -> str | None:
    """Get the current tenant_id from context (for use outside request scope)."""
    return _current_tenant_id.get()


import contextlib  # noqa: E402  (placed near usage to keep ordering local)
from collections.abc import AsyncIterator  # noqa: E402


@contextlib.asynccontextmanager
async def tenant_scoped_session(
    tenant_id: str | uuid.UUID | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """Open a session whose RLS GUC is pinned to ``tenant_id`` (§9 P0).

    Background tasks (async delegation ``_run``, ``run_in_background``
    subagents, daemons) must use this instead of bare ``async_session()`` —
    a bare session never runs ``SET LOCAL app.current_tenant_id``, so under
    enforced RLS it sees nothing (fail-closed) and under the current
    owner-bypass it sees *everything*. Falls back to the request ContextVar
    when ``tenant_id`` is omitted; empty/None pins ``''`` (matches no tenant
    rows — same safe default as ``get_db()``).

    ``session_factory`` exists for callers that hold their own engine
    (integration tests against a Testcontainers PG, future workflow engine).
    """
    factory = session_factory or async_session
    effective = tenant_id if tenant_id is not None else _current_tenant_id.get()
    previous_tenant = _current_tenant_id.get()

    async with factory() as session:
        try:
            await pin_rls_tenant_context(session, effective)

            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            _current_tenant_id.set(previous_tenant)


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
        "[RLS] Entering BYPASS scope — reason=%r actor=%r. Cross-tenant data is now visible on this session.",
        reason,
        actor_id,
    )
    await session.execute(text("SET LOCAL app.current_tenant_id = 'BYPASS'"))
    try:
        yield session
    finally:
        # Restore tenant scoping. ContextVar fallback covers the case
        # where the session entered without a tenant set. Two guards (C3):
        # a DB error inside the scope leaves the transaction failed — running
        # the restore there raises a second error that masks the first, and
        # SET LOCAL dies with the transaction anyway, so skip it and let the
        # caller's rollback clear the scope. If the restore itself fails,
        # log it rather than shadowing the body's exception.
        try:
            if not getattr(session, "is_active", True):
                logger.warning(
                    "[RLS] BYPASS scope exited with a failed transaction; skipping GUC restore — "
                    "the caller's rollback clears the scope. (reason=%r)",
                    reason,
                )
            else:
                tenant_value = _current_tenant_id.get()
                try:
                    restored = _normalize_rls_tenant_value(tenant_value)
                except ValueError:
                    logger.error("[RLS] Invalid tenant id %r after BYPASS; failing closed to ''", tenant_value)
                    restored = ""
                await session.execute(text(_rls_tenant_statement(restored)))
        except Exception as exc:  # noqa: BLE001 - never mask the body's exception from finally
            logger.error("[RLS] Failed to restore tenant scope after BYPASS: %s", exc)
        logger.info("[RLS] Exited BYPASS scope (reason=%r)", reason)
