"""Connection-owned execution admission for one accepted tool permission."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool


class PermissionExecutionUnavailable(RuntimeError):
    """Execution ownership could not be established or was lost before an effect."""

    code = "permission_execution_unavailable"


class PermissionExecutionLease:
    def __init__(self, connection, *, acquired: bool, pid: int) -> None:
        self.connection = connection
        self.acquired = acquired
        self.pid = pid

    async def ensure_owned(self) -> None:
        if not self.acquired or self.connection.closed or self.connection.invalidated:
            raise PermissionExecutionUnavailable(PermissionExecutionUnavailable.code)
        try:
            pid = await self.connection.scalar(text("SELECT pg_backend_pid()"))
            await self.connection.commit()
        except (SQLAlchemyError, TimeoutError, OSError) as exc:
            raise PermissionExecutionUnavailable(PermissionExecutionUnavailable.code) from exc
        if pid != self.pid:
            raise PermissionExecutionUnavailable(PermissionExecutionUnavailable.code)


@asynccontextmanager
async def permission_execution_lease(db: AsyncSession, permission_request_id: uuid.UUID):
    from app.config import get_settings
    from app.database import _engine_connect_args

    # A dedicated non-pooled connection holds the session lock across the
    # business session's commits without reserving request/runtime pool slots.
    settings = get_settings()
    engine = create_async_engine(
        db.get_bind().engine.url, poolclass=NullPool,
        connect_args={
            **_engine_connect_args(settings), "timeout": settings.DB_POOL_TIMEOUT,
            "command_timeout": max(1, settings.DB_STATEMENT_TIMEOUT_MS) / 1000,
        },
    )
    try:
        try:
            connection = await engine.connect()
        except (SQLAlchemyError, TimeoutError, OSError) as exc:
            raise PermissionExecutionUnavailable(PermissionExecutionUnavailable.code) from exc
        try:
            try:
                row = (await connection.execute(text(
                    "SELECT pg_try_advisory_lock(hashtextextended(:key, 0)), pg_backend_pid()"
                ), {"key": f"session-permission-execution:{permission_request_id}"})).one()
                await connection.commit()
            except (SQLAlchemyError, TimeoutError, OSError) as exc:
                raise PermissionExecutionUnavailable(PermissionExecutionUnavailable.code) from exc
            lease = PermissionExecutionLease(connection, acquired=bool(row[0]), pid=int(row[1]))
            if not lease.acquired:
                await connection.close()
            yield lease
        finally:
            await connection.close()
    finally:
        await engine.dispose()
