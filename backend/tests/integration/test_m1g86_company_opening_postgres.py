"""The installed/hosted console consumes configuration facts, never fake readiness."""

import uuid

import pytest
from sqlalchemy import text

from app.database import get_db, pin_rls_tenant_context
from app.models.user import User
from tests.integration.test_m1g85_employee_readiness_postgres import readiness_case, native_worker  # noqa: F401


@pytest.fixture
async def opening_case(readiness_case, owner_sessionmaker):  # noqa: F811
    from app.api import onboarding

    c = readiness_case
    async with owner_sessionmaker() as db:
        owner = await db.get(User, c.owner.id)
        owner.role = "org_admin"
        await db.commit()
        c.holder["user"] = owner
    c.app.include_router(onboarding.router, prefix="/api")
    return c


async def test_opening_is_scoped_configuration_not_a_readiness_claim(opening_case):
    c = opening_case
    result = await c.client.get("/api/enterprise/onboarding-status")
    assert result.status_code == 200, result.text
    status = result.json()
    assert status["configuration_only"] is True
    assert status["tenant_id"] == str(c.tenant)
    assert status["authenticated_user_id"] == str(c.owner.id)
    assert status["employees"] == [{"id": str(c.agent), "name": "Analyst", "agent_type": "native"}]
    items = {row["key"]: row for row in status["items"]}
    assert items["llm"]["completed"] is True and items["agent"]["completed"] is True
    assert all(items[key]["required"] is False for key in ("sso", "channel", "org"))
    assert "SYNTHETIC_PRIVATE_KEY_CANARY" not in result.text


async def test_opening_excludes_retired_and_internal_employees(opening_case, owner_sessionmaker):
    from datetime import datetime, timezone
    from tests.integration.test_m1e_business_teams_postgres import _agent

    c = opening_case
    async with owner_sessionmaker() as db:
        deleted = await _agent(db, tenant_id=c.tenant, owner_id=c.owner.id, name="Retired")
        deleted.deleted_at = datetime.now(timezone.utc)
        internal = await _agent(db, tenant_id=c.tenant, owner_id=c.owner.id, name="Internal setup")
        internal.agent_class = "internal_system"
        await db.commit()
    status = (await c.client.get("/api/enterprise/onboarding-status")).json()
    assert [row["id"] for row in status["employees"]] == [str(c.agent)]
    async with owner_sessionmaker() as db:
        from app.models.agent import Agent
        (await db.get(Agent, c.agent)).deleted_at = datetime.now(timezone.utc)
        await db.commit()
    empty = (await c.client.get("/api/enterprise/onboarding-status")).json()
    assert empty["employees"] == []
    assert next(row for row in empty["items"] if row["key"] == "agent")["completed"] is False


async def test_members_and_foreign_company_hints_cannot_read_setup(opening_case):
    c = opening_case
    foreign = await c.client.get(f"/api/enterprise/onboarding-status?tenant_id={uuid.uuid4()}")
    assert foreign.status_code == 403, foreign.text
    c.holder["user"] = c.lead
    denied = await c.client.get("/api/enterprise/onboarding-status")
    assert denied.status_code == 403, denied.text


async def test_lost_database_connection_is_unavailable_not_an_empty_checklist(opening_case, owner_sessionmaker, app_user_sessionmaker):
    c = opening_case

    async def disconnected_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, c.tenant)
            pid = await db.scalar(text("SELECT pg_backend_pid()"))
            async with owner_sessionmaker() as control:
                assert await control.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            yield db

    c.app.dependency_overrides[get_db] = disconnected_db
    result = await c.client.get("/api/enterprise/onboarding-status")
    assert result.status_code == 503, result.text
    assert result.json()["detail"]["code"] == "onboarding_status_unavailable"


async def test_company_opening_native_browser(opening_case, app_user_engine, tmp_path):
    import asyncio
    import json
    import os
    import socket
    from pathlib import Path
    import uvicorn
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from app.api import auth, enterprise, plans
    from app.core.security import create_access_token

    c = opening_case
    if "HIVE_TEST_G86_FRONTEND_DIST" not in os.environ:
        pytest.skip("Built frontend required for the native browser")
    dist = Path(os.environ["HIVE_TEST_G86_FRONTEND_DIST"])
    assert (dist / "index.html").is_file()
    for router in (auth.router, enterprise.router, plans.router):
        c.app.include_router(router, prefix="/api")
    c.app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @c.app.get("/api/{path:path}")
    async def unavailable(path: str):
        return JSONResponse(status_code=404, content={"detail": "Unsupported fixture endpoint"})

    @c.app.get("/{path:path}")
    async def page(path: str):
        return FileResponse(dist / "index.html")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(c.app, lifespan="off", access_log=False, log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    process = None
    try:
        for _ in range(500):
            if server.started:
                break
            assert not serving.done()
            await asyncio.sleep(0.01)
        assert server.started
        fixture = {"origin": f"http://127.0.0.1:{listener.getsockname()[1]}", "tenantId": str(c.tenant),
            "agentId": str(c.agent), "outputDir": str(tmp_path),
            "token": create_access_token(str(c.owner.id), "user", str(c.tenant))}
        fixture_path = tmp_path / "browser-fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        frontend = Path(__file__).resolve().parents[3] / "frontend"
        process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/company-opening-native.mjs"), str(fixture_path),
            cwd=frontend, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        line = await asyncio.wait_for(process.stdout.readline(), 60)
        if not line:
            output, error = await process.communicate()
            pytest.fail(f"Company opening browser failed: {output!r} {error!r}")
        assert json.loads(line)["event"] == "opening_check_queued"
        async with native_worker(c, app_user_engine):
            process.stdin.write(b'{"event":"worker_started"}\n')
            await process.stdin.drain()
            output, error = await asyncio.wait_for(process.communicate(), 60)
            assert process.returncode == 0, (output, error)
        assert (c.workspace / "private.txt").read_text() == "SYNTHETIC_PRIVATE_KEY_CANARY"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
