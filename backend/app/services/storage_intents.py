"""Commit mechanical storage intent without committing a caller's pending work."""

import asyncio
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.database import pin_rls_tenant_context
from app.services.blob_store import BlobStoreError

_INTENT_LOCKS = WeakKeyDictionary()


@asynccontextmanager
async def durable_storage_intent(db, tenant_id):
    if not isinstance(db.bind, AsyncEngine):
        raise BlobStoreError("Independent storage persistence is unavailable")
    # One reserved short-lived connection per engine avoids exhausting a request
    # pool whose callers already hold connections. Provider I/O stays outside it.
    async with _INTENT_LOCKS.setdefault(db.bind, asyncio.Lock()):
        source = db.bind.sync_engine
        independent = AsyncEngine(Engine(source.pool.recreate(), source.dialect, source.url,
            execution_options=source.get_execution_options(), hide_parameters=source.hide_parameters))
        try:
            async with async_sessionmaker(independent, expire_on_commit=False)() as intent:
                await pin_rls_tenant_context(intent, tenant_id)
                yield intent
                await intent.commit()
        except SQLAlchemyError:
            raise BlobStoreError("Durable storage intent is unavailable") from None
        finally:
            await independent.dispose()
