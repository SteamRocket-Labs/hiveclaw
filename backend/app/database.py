"""Database connection and session management."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session as SyncSession

from app.config import get_settings
from app.runtime.tenant_admission import RuntimeTenantPreconditionError

logger = logging.getLogger(__name__)
settings = get_settings()

POSTGRES_NUL_ESCAPE = "\\u0000"
_POSTGRES_TEXT_CONTRACT_STATE: dict[str, Any] = {
    "repair_events": 0,
    "repaired_codepoints": 0,
    "last_repair_at": None,
    "last_surface": None,
}
_POSTGRES_TEXT_CONTRACT_LOCK = threading.Lock()


class PostgresTextContractError(ValueError):
    """Raised when PostgreSQL-compatible repair would destroy evidence."""


def repair_postgres_nul(value: Any) -> tuple[Any, int]:
    """Encode U+0000 without silently dropping model/user/external evidence.

    PostgreSQL rejects U+0000 in Text, JSON, and JSONB. Replacing it with the
    visible literal ``\\u0000`` is an exact machine-contract repair, not a
    semantic judgment. Binary values are intentionally untouched because
    PostgreSQL BYTEA supports zero bytes.
    """
    if isinstance(value, str):
        replacement_count = value.count("\x00")
        if replacement_count:
            return value.replace("\x00", POSTGRES_NUL_ESCAPE), replacement_count
        return value, 0
    if isinstance(value, dict):
        repaired: dict[Any, Any] = {}
        replacement_count = 0
        for raw_key, raw_value in value.items():
            repaired_key, key_count = repair_postgres_nul(raw_key)
            repaired_value, value_count = repair_postgres_nul(raw_value)
            if repaired_key in repaired:
                raise PostgresTextContractError(
                    "PostgreSQL NUL repair would cause a JSON object key collision; evidence was not persisted"
                )
            repaired[repaired_key] = repaired_value
            replacement_count += key_count + value_count
        return repaired, replacement_count
    if isinstance(value, list):
        repaired_items: list[Any] = []
        replacement_count = 0
        for item in value:
            repaired_item, item_count = repair_postgres_nul(item)
            repaired_items.append(repaired_item)
            replacement_count += item_count
        return repaired_items, replacement_count
    if isinstance(value, tuple):
        repaired_items: list[Any] = []
        replacement_count = 0
        for item in value:
            repaired_item, item_count = repair_postgres_nul(item)
            repaired_items.append(repaired_item)
            replacement_count += item_count
        return tuple(repaired_items), replacement_count
    return value, 0


def _record_postgres_text_repair(*, replacement_count: int, surface: str) -> None:
    if replacement_count <= 0:
        return
    with _POSTGRES_TEXT_CONTRACT_LOCK:
        _POSTGRES_TEXT_CONTRACT_STATE["repair_events"] = int(_POSTGRES_TEXT_CONTRACT_STATE["repair_events"]) + 1
        _POSTGRES_TEXT_CONTRACT_STATE["repaired_codepoints"] = (
            int(_POSTGRES_TEXT_CONTRACT_STATE["repaired_codepoints"]) + replacement_count
        )
        _POSTGRES_TEXT_CONTRACT_STATE["last_repair_at"] = datetime.now(timezone.utc).isoformat()
        _POSTGRES_TEXT_CONTRACT_STATE["last_surface"] = surface
    logger.warning(
        "[DB] repaired %s PostgreSQL-incompatible U+0000 codepoint(s) at %s",
        replacement_count,
        surface,
    )


def snapshot_postgres_text_contract() -> dict[str, Any]:
    with _POSTGRES_TEXT_CONTRACT_LOCK:
        state = dict(_POSTGRES_TEXT_CONTRACT_STATE)
    return {
        "encoding": "literal_unicode_escape",
        "replacement": POSTGRES_NUL_ESCAPE,
        **state,
    }


def _postgres_json_serializer(value: Any) -> str:
    encoded = json.dumps(value)
    if POSTGRES_NUL_ESCAPE not in encoded:
        return encoded
    repaired, replacement_count = repair_postgres_nul(value)
    _record_postgres_text_repair(replacement_count=replacement_count, surface="json_serializer")
    return json.dumps(repaired) if replacement_count else encoded


def _compiled_bind_types(context: Any) -> tuple[list[Any], dict[str, Any]]:
    compiled = getattr(context, "compiled", None)
    if compiled is None:
        return [], {}
    binds = getattr(compiled, "binds", {}) or {}
    positional_types: list[Any] = []
    for name in getattr(compiled, "positiontup", None) or ():
        bind = binds.get(name)
        positional_types.append(getattr(bind, "type", None))
    named_types = {str(name): getattr(bind, "type", None) for name, bind in binds.items()}
    return positional_types, named_types


def _repair_dbapi_value(value: Any, bind_type: Any) -> tuple[Any, int]:
    if isinstance(bind_type, JSON) and isinstance(value, str):
        if POSTGRES_NUL_ESCAPE not in value:
            return value, 0
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return repair_postgres_nul(value)
        repaired, replacement_count = repair_postgres_nul(decoded)
        if replacement_count:
            return json.dumps(repaired), replacement_count
        return value, 0
    return repair_postgres_nul(value)


def _repair_dbapi_parameter_set(
    parameters: Any,
    *,
    positional_types: list[Any],
    named_types: dict[str, Any],
) -> tuple[Any, int]:
    if isinstance(parameters, tuple):
        repaired: list[Any] = []
        replacement_count = 0
        for index, value in enumerate(parameters):
            bind_type = positional_types[index] if index < len(positional_types) else None
            repaired_value, value_count = _repair_dbapi_value(value, bind_type)
            repaired.append(repaired_value)
            replacement_count += value_count
        return tuple(repaired), replacement_count
    if isinstance(parameters, dict):
        repaired: dict[Any, Any] = {}
        replacement_count = 0
        for key, value in parameters.items():
            repaired_value, value_count = _repair_dbapi_value(value, named_types.get(str(key)))
            repaired[key] = repaired_value
            replacement_count += value_count
        return repaired, replacement_count
    return _repair_dbapi_value(parameters, None)


@event.listens_for(Engine, "before_cursor_execute", retval=True)
def _repair_postgres_dbapi_parameters(
    _connection,
    _cursor,
    statement,
    parameters,
    context,
    executemany,
):
    """Last authoritative guard for ORM and Core Text/JSON/JSONB writes."""
    positional_types, named_types = _compiled_bind_types(context)
    if executemany and isinstance(parameters, list):
        repaired_batches: list[Any] = []
        replacement_count = 0
        for parameter_set in parameters:
            repaired_set, set_count = _repair_dbapi_parameter_set(
                parameter_set,
                positional_types=positional_types,
                named_types=named_types,
            )
            repaired_batches.append(repaired_set)
            replacement_count += set_count
        repaired_parameters: Any = repaired_batches
    else:
        repaired_parameters, replacement_count = _repair_dbapi_parameter_set(
            parameters,
            positional_types=positional_types,
            named_types=named_types,
        )
    _record_postgres_text_repair(replacement_count=replacement_count, surface="dbapi_parameters")
    return statement, repaired_parameters


def _normalize_async_url(url: str) -> str:
    """Coerce a bare ``postgresql://`` URL (e.g. a Railway ``${{Postgres.DATABASE_URL}}``
    reference) to the ``+asyncpg`` driver the async engine requires. Both the app
    engine and the schema engine must use it — entrypoint runs schema steps with
    DATABASE_URL set to the (possibly bare) SCHEMA_URL."""
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _engine_pool_kwargs(settings) -> dict[str, int | bool]:
    return {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
        "pool_pre_ping": True,
    }


def _engine_connect_args(settings) -> dict[str, dict[str, str]]:
    """Build asyncpg startup settings that a non-superuser may set itself.

    ``temp_file_limit`` and ``log_temp_files`` are privileged PostgreSQL
    parameters. They are intentionally installed as ``app_rls`` role defaults
    by ``grant_rls_app_role`` instead of being sent here, which would make every
    production connection fail during authentication.
    """

    process_role = str(getattr(settings, "HIVE_PROCESS_ROLE", "runtime") or "runtime").strip().lower()
    return {
        "server_settings": {
            "application_name": f"hive-{process_role}",
            "statement_timeout": str(max(1, int(settings.DB_STATEMENT_TIMEOUT_MS))),
            "idle_in_transaction_session_timeout": str(max(1, int(settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS))),
        }
    }


engine = create_async_engine(
    _normalize_async_url(settings.DATABASE_URL),
    echo=settings.DEBUG,
    json_serializer=_postgres_json_serializer,
    connect_args=_engine_connect_args(settings),
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
        "query_guards": {
            "statement_timeout_ms": settings.DB_STATEMENT_TIMEOUT_MS,
            "idle_in_transaction_timeout_ms": settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS,
            "temp_file_limit_kb": settings.DB_TEMP_FILE_LIMIT_KB,
            "log_temp_files_kb": settings.DB_LOG_TEMP_FILES_KB,
        },
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
    else create_async_engine(
        _schema_url,
        echo=settings.DEBUG,
        pool_size=2,
        max_overflow=2,
        json_serializer=_postgres_json_serializer,
    )
)

# Context variable to carry the current tenant_id through the request lifecycle.
# Set by get_db() from request.state.tenant_id (populated by TenantMiddleware).
_current_tenant_id: ContextVar[str | None] = ContextVar("_current_tenant_id", default=None)
_RLS_TENANT_INFO_KEY = "hive_rls_tenant_id"
_RLS_BYPASS_VALUE = "BYPASS"
_AFTER_COMMIT_CALLBACKS_KEY = "hive_after_commit_callbacks"
AfterCommitCallback = Callable[[], Awaitable[None]]
AfterCommitEntry = tuple[object, str, AfterCommitCallback]
_after_commit_tasks: set[asyncio.Task[None]] = set()


async def _run_after_commit_callback(callback: AfterCommitCallback, description: str) -> None:
    try:
        await callback()
    except Exception as exc:  # noqa: BLE001 - committed truth is recovered by each consumer's sweeper/outbox.
        logger.warning("[DB] after-commit callback failed (%s): %s", description, exc)


def schedule_after_commit(
    session: AsyncSession,
    callback: AfterCommitCallback,
    *,
    description: str,
) -> bool:
    """Run an async side effect only after the caller's outer commit succeeds.

    The callback must carry immutable identifiers, never ORM instances or the
    live session. Outer rollback discards it. Nested savepoint commits do not
    dispatch it, which prevents consumers from observing uncommitted rows.
    """
    sync_session = getattr(session, "sync_session", None)
    if sync_session is None:
        # The durable pending/outbox row remains the recovery authority. This
        # path also lets structural test doubles exercise transcript assembly
        # without pretending they implement SQLAlchemy transaction events.
        logger.debug("[DB] after-commit wake-up unavailable for %s; durable sweeper will recover", description)
        return False
    transaction = sync_session.get_nested_transaction() or sync_session.get_transaction()
    if transaction is None:
        logger.warning(
            "[DB] after-commit wake-up has no active transaction for %s; durable sweeper will recover", description
        )
        return False
    callbacks: list[AfterCommitEntry] = sync_session.info.setdefault(_AFTER_COMMIT_CALLBACKS_KEY, [])
    callbacks.append((transaction, str(description), callback))
    return True


@event.listens_for(SyncSession, "after_commit")
def _dispatch_after_outer_commit(session: SyncSession) -> None:
    # SQLAlchemy fires after_commit for RELEASE SAVEPOINT too. At that point
    # in_nested_transaction() is still true; only the outer commit may publish.
    if session.in_nested_transaction():
        return
    callbacks = session.info.pop(_AFTER_COMMIT_CALLBACKS_KEY, [])
    if not callbacks:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("[DB] cannot dispatch %s after-commit callback(s): no running event loop", len(callbacks))
        return
    for _transaction, description, callback in callbacks:
        task = loop.create_task(
            _run_after_commit_callback(callback, description),
            name=f"db-after-commit:{description}"[:100],
        )
        _after_commit_tasks.add(task)
        task.add_done_callback(_after_commit_tasks.discard)


@event.listens_for(SyncSession, "after_soft_rollback")
def _discard_after_outer_rollback(session: SyncSession, previous_transaction) -> None:
    if previous_transaction.parent is None:
        session.info.pop(_AFTER_COMMIT_CALLBACKS_KEY, None)
        return

    callbacks: list[AfterCommitEntry] = session.info.get(_AFTER_COMMIT_CALLBACKS_KEY, [])

    def _belongs_to_rolled_back_savepoint(transaction: object) -> bool:
        current = transaction
        while current is not None:
            if current is previous_transaction:
                return True
            current = getattr(current, "parent", None)
        return False

    session.info[_AFTER_COMMIT_CALLBACKS_KEY] = [
        entry for entry in callbacks if not _belongs_to_rolled_back_savepoint(entry[0])
    ]


def stamp_new_tenant_owned_rows(session: SyncSession) -> None:
    """Bind new tenant-owned ORM rows to the already pinned session scope.

    The session GUC is the trusted persistence authority. This hook removes a
    class of call-site omissions without inventing scope for bare/BYPASS
    sessions or for intentionally platform-shared/operator-nullable rows.
    """

    scope = session.info.get(_RLS_TENANT_INFO_KEY)
    if not scope or scope == _RLS_BYPASS_VALUE:
        return
    tenant_id = uuid.UUID(str(scope))
    from app.db_bootstrap import STRICT_TENANT_RLS_TABLES

    strict_tables = set(STRICT_TENANT_RLS_TABLES)
    for row in session.new:
        mapper = getattr(row, "__mapper__", None) or getattr(type(row), "__mapper__", None)
        table = getattr(mapper, "local_table", None)
        if getattr(table, "name", None) not in strict_tables:
            continue
        if hasattr(row, "tenant_id") and getattr(row, "tenant_id", None) is None:
            setattr(row, "tenant_id", tenant_id)


@event.listens_for(SyncSession, "before_flush")
def _stamp_new_tenant_owned_rows(session: SyncSession, _flush_context, _instances) -> None:
    stamp_new_tenant_owned_rows(session)


def _normalize_rls_tenant_value(tenant_id: str | uuid.UUID | None) -> str:
    """Return a validated tenant id string, or empty string for fail-closed scope."""
    if tenant_id:
        return str(uuid.UUID(str(tenant_id)))
    return ""


def _normalize_rls_transaction_scope_value(value: str | uuid.UUID | None) -> str:
    """Normalize a persisted ORM-session RLS scope.

    Regular tenant pinning must remain UUID-only. ``BYPASS`` is accepted only
    from the sanctioned ``enter_rls_bypass`` session-info path so the
    transaction re-pin hook can survive explicit commits inside that audited
    scope.
    """
    if value == _RLS_BYPASS_VALUE:
        return _RLS_BYPASS_VALUE
    return _normalize_rls_tenant_value(value)


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
    tenant_id = _normalize_rls_transaction_scope_value(session.info.get(_RLS_TENANT_INFO_KEY))
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
    require_tenant: bool = False,
    source: str = "tenant_scoped_session",
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
    effective = tenant_id if tenant_id is not None else _current_tenant_id.get()
    if require_tenant and not effective:
        raise RuntimeTenantPreconditionError(
            reason_code="tenant_required",
            message=f"{source} requires a tenant before opening a mutating runtime session.",
            source=source,
        )

    factory = session_factory or async_session
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
    sync_session = getattr(session, "sync_session", None)
    had_previous_session_info = False
    previous_session_info: str | uuid.UUID | None = None
    if sync_session is not None:
        had_previous_session_info = _RLS_TENANT_INFO_KEY in sync_session.info
        previous_session_info = sync_session.info.get(_RLS_TENANT_INFO_KEY)
        sync_session.info[_RLS_TENANT_INFO_KEY] = _RLS_BYPASS_VALUE

    def restore_session_info() -> None:
        if sync_session is None:
            return
        if had_previous_session_info:
            sync_session.info[_RLS_TENANT_INFO_KEY] = previous_session_info
        else:
            sync_session.info.pop(_RLS_TENANT_INFO_KEY, None)

    bypass_guc_set = False
    try:
        await session.execute(text(_rls_tenant_statement(_RLS_BYPASS_VALUE)))
        bypass_guc_set = True
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
            if not bypass_guc_set:
                restore_session_info()
            elif not getattr(session, "is_active", True):
                restore_session_info()
                logger.warning(
                    "[RLS] BYPASS scope exited with a failed transaction; skipping GUC restore — "
                    "the caller's rollback clears the scope. (reason=%r)",
                    reason,
                )
            else:
                # Restore the scope that actually existed before entry. When
                # this scope was nested on a session that already persisted a
                # scope (an outer BYPASS or a pinned tenant), that persisted
                # scope is the restore target so the GUC and session info
                # agree for the rest of the outer scope. The ContextVar is a
                # fallback only for sessions that entered with no scope.
                if had_previous_session_info:
                    try:
                        restored = _normalize_rls_transaction_scope_value(previous_session_info)
                    except ValueError:
                        logger.error(
                            "[RLS] Invalid persisted scope %r after BYPASS; failing closed to ''",
                            previous_session_info,
                        )
                        restored = ""
                else:
                    tenant_value = _current_tenant_id.get()
                    try:
                        restored = _normalize_rls_tenant_value(tenant_value)
                    except ValueError:
                        logger.error("[RLS] Invalid tenant id %r after BYPASS; failing closed to ''", tenant_value)
                        restored = ""
                restore_session_info()
                await session.execute(text(_rls_tenant_statement(restored)))
        except Exception as exc:  # noqa: BLE001 - never mask the body's exception from finally
            logger.error("[RLS] Failed to restore tenant scope after BYPASS: %s", exc)
        finally:
            restore_session_info()
        logger.info("[RLS] Exited BYPASS scope (reason=%r)", reason)
