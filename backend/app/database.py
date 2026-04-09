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
