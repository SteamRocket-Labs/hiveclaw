"""Request SQL and checkout timing uses real connections, with no SQL in headers."""

import asyncio
import re

import pytest
from fastapi import BackgroundTasks, FastAPI
from httpx import ASGITransport, AsyncClient
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware.cors import CORSMiddleware

from app.core.middleware import TraceIdMiddleware
from app.config import get_settings
from app.database import _engine_pool_kwargs

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def timing_engine(app_user_engine, request):
    kwargs = {**_engine_pool_kwargs(get_settings()), "pool_size": getattr(request, "param", 1),
        "max_overflow": 0, "pool_timeout": 2, "pool_pre_ping": False}
    engine = create_async_engine(app_user_engine.url, **kwargs)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    yield engine
    await engine.dispose()


def _app():
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["https://fixture.test"])
    app.add_middleware(TraceIdMiddleware)
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://timing")


def _timing(response):
    value = response.headers["server-timing"]
    match = re.fullmatch(r'app;dur=(\d+\.\d), db;dur=(\d+\.\d);desc="(\d+)", pool;dur=(\d+\.\d)', value)
    assert match, value
    app_ms, db_ms, statements, pool_ms = match.groups()
    return float(app_ms), float(db_ms), int(statements), float(pool_ms)


async def test_known_statements_and_cors_header(timing_engine):
    app = _app()

    @app.get("/api/known")
    async def known():
        async with timing_engine.connect() as conn:
            await conn.execute(text("SELECT pg_sleep(0.01)"))
            await conn.execute(text("SELECT 42"))
        return {"ok": True}

    async with _client(app) as client:
        response = await client.get("/api/known", headers={"Origin": "https://fixture.test"})
    app_ms, db_ms, statements, pool_ms = _timing(response)
    assert response.status_code == 200 and statements == 2
    assert db_ms >= 8 and app_ms >= db_ms
    assert pool_ms >= 0
    assert response.headers["access-control-allow-origin"] == "https://fixture.test"


async def test_exhausted_pool_reports_checkout_wait(timing_engine):
    app, entered = _app(), asyncio.Event()
    held = await timing_engine.connect()

    @app.get("/api/wait")
    async def wait():
        entered.set()
        async with timing_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True}

    async def release():
        await entered.wait()
        await asyncio.sleep(0.05)
        await held.close()

    releaser = asyncio.create_task(release())
    try:
        async with _client(app) as client:
            response = await client.get("/api/wait")
    finally:
        await held.close()
        await releaser
    app_ms, _db_ms, statements, pool_ms = _timing(response)
    assert statements == 1
    assert pool_ms >= 40 and app_ms >= pool_ms


@pytest.mark.parametrize("timing_engine", [2], indirect=True)
async def test_concurrent_worker_statements_on_same_engine_are_excluded(timing_engine):
    app, started, finished = _app(), asyncio.Event(), asyncio.Event()

    async def worker():
        async with timing_engine.connect() as conn:
            started.set()
            await conn.execute(text("SELECT pg_sleep(0.06)"))
            await conn.execute(text("SELECT 777"))
        finished.set()

    @app.get("/api/overlap")
    async def overlap():
        await started.wait()
        async with timing_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            await conn.execute(text("SELECT 2"))
        await finished.wait()
        return {"ok": True}

    # Startup lanes are created outside request context, as this task is.
    lane = asyncio.create_task(worker())
    async with _client(app) as client:
        response = await client.get("/api/overlap")
    await lane
    _app_ms, _db_ms, statements, _pool_ms = _timing(response)
    assert statements == 2


async def test_response_background_statements_and_next_request_are_excluded(timing_engine):
    app = _app()

    async def background():
        async with timing_engine.connect() as conn:
            await conn.execute(text("SELECT 8"))
            await conn.execute(text("SELECT 9"))

    @app.get("/api/background")
    async def queued(tasks: BackgroundTasks):
        async with timing_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        tasks.add_task(background)
        return {"ok": True}

    @app.get("/api/empty")
    async def empty():
        return {"ok": True}

    async with _client(app) as client:
        first = await client.get("/api/background")
        second = await client.get("/api/empty")
    assert _timing(first)[2] == 1
    assert _timing(second)[1:] == (0, 0, 0)


async def test_request_error_retains_exception_detail():
    app, logs = _app(), []

    @app.get("/api/error")
    async def broken():
        raise ValueError("fixture failure detail")

    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        async with _client(app) as client:
            response = await client.get("/api/error")
    finally:
        logger.remove(sink)
    assert response.status_code == 500
    errors = [entry for entry in logs if "<-- GET /api/error ERROR " in entry]
    assert len(errors) == 1
    assert errors[0].endswith(" - fixture failure detail\n")
