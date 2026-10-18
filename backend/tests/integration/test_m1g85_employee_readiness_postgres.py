"""Readiness is an actual native worker receipt, without a provider inference."""

import asyncio
import os
import signal
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.llm import LLMModel
from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
from app.models.tenant import Tenant
from tests.integration.test_m1e_business_teams_postgres import _agent, _user


@pytest.fixture
async def readiness_case(owner_sessionmaker, app_user_sessionmaker, tmp_path):
    from app.api import agents, business_teams, chat_sessions, employee_readiness
    from app.config import get_settings

    tenant, team_id, owner_id, lead_id, model_id = [uuid.uuid4() for _ in range(5)]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name="Readiness", slug=f"readiness-{tenant}"))
        await db.flush()
        owner = _user(db, id=owner_id, tenant_id=tenant, username=f"owner-{owner_id.hex}", email=f"{owner_id}@ready.test")
        lead = _user(db, id=lead_id, tenant_id=tenant, username=f"lead-{lead_id.hex}", email=f"{lead_id}@ready.test")
        await db.flush()
        db.add(BusinessTeam(id=team_id, tenant_id=tenant, name="Delivery", status="active", created_by_user_id=lead_id))
        db.add(LLMModel(id=model_id, tenant_id=tenant, provider="openai", model="synthetic", label="Approved model",
            api_key_encrypted="SYNTHETIC_PRIVATE_KEY_CANARY", enabled=True))
        await db.flush()
        db.add(BusinessTeamMembership(tenant_id=tenant, business_team_id=team_id, user_id=lead_id, role="lead", status="active"))
        agent = await _agent(db, tenant_id=tenant, owner_id=owner_id, name="Analyst")
        agent.home_business_team_id = team_id
        agent.primary_model_id = model_id
        await db.commit()
    from pathlib import Path

    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent.id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "private.txt").write_text("SYNTHETIC_PRIVATE_KEY_CANARY")
    holder = {"user": lead}
    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, holder["user"].tenant_id)
            yield db
    app = FastAPI()
    for router in (agents.router, business_teams.router, chat_sessions.router, employee_readiness.router):
        app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: holder["user"]
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://readiness") as client:
        yield SimpleNamespace(client=client, app=app, holder=holder, tenant=tenant, team=team_id, owner=owner,
            lead=lead, agent=agent.id, model=model_id, workspace=workspace, tmp_path=tmp_path)


async def test_team_readiness_is_queued_before_the_real_worker_checks_it(readiness_case):
    c = readiness_case
    url = f"/api/business-teams/{c.team}/agents/{c.agent}/readiness-checks"
    detail = await c.client.get(f"/api/business-teams/{c.team}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["home_employees"] == [{"id": str(c.agent), "name": "Analyst"}]
    assert detail.json()["affiliations"] == []
    command = {"request_id": str(uuid.uuid4())}
    created = await c.client.post(url, json=command)
    assert created.status_code == 202, created.text
    receipt = created.json()
    assert receipt["execution_status"] == "pending" and receipt["checks"] == []
    replay = await c.client.post(url, json=command)
    assert replay.json()["id"] == receipt["id"]
    assert "SYNTHETIC_PRIVATE_KEY_CANARY" not in created.text
    assert receipt["request_id"] == command["request_id"]
    assert (c.workspace / "private.txt").read_text() == "SYNTHETIC_PRIVATE_KEY_CANARY"


@asynccontextmanager
async def native_worker(case, engine):
    from app.config import get_settings

    script = """import asyncio,signal,os
from app.services.secrets_provider import init_secrets_provider
init_secrets_provider(os.environ['SECRETS_MASTER_KEY'])
from app.services.runtime_task_worker import start_runtime_task_worker_loop
async def run():
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stop.set)
    worker=asyncio.create_task(start_runtime_task_worker_loop())
    await stop.wait()
    worker.cancel()
    await asyncio.gather(worker,return_exceptions=True)
asyncio.run(run())
"""
    env = {**os.environ, "DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "AGENT_DATA_DIR": str(get_settings().AGENT_DATA_DIR)}
    with (case.tmp_path / "worker.log").open("w") as log:
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c", script,
            env=env, stdout=log, stderr=log)
        try:
            yield process
        finally:
            if process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            assert process.returncode == 0, (case.tmp_path / "worker.log").read_text()[-3000:]


async def wait_checked(c, url, check_id, process):
    for _ in range(300):
        assert process.returncode is None, (c.tmp_path / "worker.log").read_text()[-3000:]
        result = await c.client.get(f"{url}/{check_id}")
        assert result.status_code == 200, result.text
        if result.json()["execution_status"] not in {"pending", "running", "resumable"}:
            return result
        await asyncio.sleep(0.1)
    pytest.fail((c.tmp_path / "worker.log").read_text()[-5000:])


async def test_separate_worker_records_actual_checks_without_reading_private_data(readiness_case, app_user_engine, owner_sessionmaker):
    from app.models.runtime_task import RuntimeTask

    c = readiness_case
    url = f"/api/business-teams/{c.team}/agents/{c.agent}/readiness-checks"
    created = await c.client.post(url, json={"request_id": str(uuid.uuid4())})
    assert created.status_code == 202, created.text
    async with native_worker(c, app_user_engine) as process:
        checked = await wait_checked(c, url, created.json()["id"], process)
    assert checked.json()["execution_status"] == "completed", checked.text
    checks = {row["key"]: row for row in checked.json()["checks"]}
    assert all(checks[key]["status"] == "passed" for key in ("identity", "worker", "storage", "model_configuration"))
    assert checks["model_configuration"]["model_label"] == "Approved model"
    assert checks["provider_inference"]["status"] == "unverified"
    assert checks["capability_effects"]["status"] == "unverified"
    assert "SYNTHETIC_PRIVATE_KEY_CANARY" not in checked.text
    assert [path.name for path in c.workspace.iterdir()] == ["private.txt"]
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, uuid.UUID(created.json()["id"]))
        assert task.root_user_id == c.lead.id and task.claim_version >= 1
        assert task.claimed_by is None and task.completed_at is not None


async def test_model_and_owner_failures_do_not_hide_other_check_results(readiness_case, app_user_engine, owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.user import User

    c = readiness_case
    async with owner_sessionmaker() as db:
        fallback_id = uuid.uuid4()
        db.add(LLMModel(id=fallback_id, tenant_id=c.tenant, provider="openai", model="fallback", label="Do not silently substitute",
            api_key_encrypted="SYNTHETIC_PRIVATE_KEY_CANARY", enabled=True))
        await db.flush()
        (await db.get(Agent, c.agent)).fallback_model_id = fallback_id
        (await db.get(LLMModel, c.model)).enabled = False
        (await db.get(User, c.owner.id)).is_active = False
        await db.commit()
    url = f"/api/business-teams/{c.team}/agents/{c.agent}/readiness-checks"
    created = await c.client.post(url, json={"request_id": str(uuid.uuid4())})
    assert created.status_code == 202, created.text
    async with native_worker(c, app_user_engine) as process:
        checked = await wait_checked(c, url, created.json()["id"], process)
    checks = {row["key"]: row for row in checked.json()["checks"]}
    assert checks["identity"]["status"] == "blocked", checked.text
    assert checks["model_configuration"]["status"] == "blocked" and checks["model_configuration"]["code"] == "primary_unavailable"
    assert checks["storage"]["status"] == checks["worker"]["status"] == "passed"
    assert "Do not silently substitute" not in checked.text


async def test_revoked_requester_can_cancel_own_check_and_worker_denies_the_other(readiness_case, app_user_engine, owner_sessionmaker):
    from sqlalchemy import select
    from app.models.runtime_task import RuntimeTask

    c = readiness_case
    url = f"/api/business-teams/{c.team}/agents/{c.agent}/readiness-checks"
    records = []
    for _ in range(2):
        created = await c.client.post(url, json={"request_id": str(uuid.uuid4())})
        assert created.status_code == 202, created.text
        records.append(created.json()["id"])
    async with owner_sessionmaker() as db:
        membership = await db.scalar(select(BusinessTeamMembership).where(BusinessTeamMembership.user_id == c.lead.id))
        membership.status = "left"
        await db.commit()
    assert (await c.client.get(f"{url}/{records[0]}")).status_code == 404
    cancelled = await c.client.post(f"{url}/{records[0]}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["execution_status"] == "killed", cancelled.text
    async with native_worker(c, app_user_engine):
        for _ in range(100):
            async with owner_sessionmaker() as db:
                task = await db.get(RuntimeTask, uuid.UUID(records[1]))
                if task.status == "failed":
                    break
            await asyncio.sleep(0.1)
        assert task.status == "failed" and task.metadata_json["error_code"] == "authority_unavailable"
        assert not task.metadata_json.get("checks")
    assert (c.workspace / "private.txt").read_text() == "SYNTHETIC_PRIVATE_KEY_CANARY"


async def test_request_is_idempotent_under_concurrency_and_never_grants_employee_access(readiness_case, owner_sessionmaker):
    c = readiness_case
    team_url = f"/api/business-teams/{c.team}/agents/{c.agent}/readiness-checks"
    command = {"request_id": str(uuid.uuid4())}
    replies = await asyncio.gather(*(c.client.post(team_url, json=command) for _ in range(2)))
    assert all(reply.status_code == 202 for reply in replies), [reply.text for reply in replies]
    assert replies[0].json()["id"] == replies[1].json()["id"]
    assert (await c.client.post(team_url, json={**command, "root_user_id": str(c.owner.id)})).status_code == 422
    owner_url = f"/api/agents/{c.agent}/readiness-checks"
    assert (await c.client.post(owner_url, json=command)).status_code == 403
    assert (await c.client.post(f"/api/agents/{c.agent}/sessions", json={})).status_code == 403
    async with owner_sessionmaker() as db:
        from sqlalchemy import select
        membership = await db.scalar(select(BusinessTeamMembership).where(BusinessTeamMembership.user_id == c.lead.id))
        membership.role = "member"
        await db.commit()
    assert (await c.client.post(team_url, json=command)).status_code == 403
    assert (await c.client.get(f"{team_url}/latest")).status_code == 403
    assert (await c.client.get(f"/api/business-teams/{c.team}")).json()["home_employees"] == []
    c.holder["user"] = c.owner
    created = await c.client.post(owner_url, json={"request_id": str(uuid.uuid4())})
    assert created.status_code == 202, created.text
    assert (await c.client.get(f"{owner_url}/{created.json()['id']}")).json()["id"] == created.json()["id"]


async def test_storage_check_refuses_a_symlink_without_touching_its_target(readiness_case, app_user_engine):
    c = readiness_case
    outside = c.tmp_path / "outside-workspace"
    c.workspace.rename(outside)
    c.workspace.symlink_to(outside, target_is_directory=True)
    url = f"/api/business-teams/{c.team}/agents/{c.agent}/readiness-checks"
    created = await c.client.post(url, json={"request_id": str(uuid.uuid4())})
    async with native_worker(c, app_user_engine) as process:
        checked = await wait_checked(c, url, created.json()["id"], process)
    checks = {row["key"]: row for row in checked.json()["checks"]}
    assert checks["storage"]["status"] == "blocked", checked.text
    assert checks["worker"]["status"] == "passed"
    assert [path.name for path in outside.iterdir()] == ["private.txt"]
    assert (outside / "private.txt").read_text() == "SYNTHETIC_PRIVATE_KEY_CANARY"
    c.workspace.unlink()
    outside.rename(c.workspace)


async def test_expired_claim_survives_startup_and_is_recovered_by_native_worker(readiness_case, app_user_engine, app_user_sessionmaker, owner_sessionmaker):
    from datetime import datetime, timedelta, timezone
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    c = readiness_case
    url = f"/api/business-teams/{c.team}/agents/{c.agent}/readiness-checks"
    created = await c.client.post(url, json={"request_id": str(uuid.uuid4())})
    task_id = uuid.UUID(created.json()["id"])
    async with tenant_scoped_session(c.tenant, session_factory=app_user_sessionmaker) as db:
        claimed = await RuntimeTaskClaimService(db=db, worker_id="stopped-worker", task_types=("readiness_probe",)).claim_available(batch_size=1)
        assert [task.id for task in claimed] == [task_id]
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        task.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    await reconcile_orphaned_runtime_tasks()
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, task_id)).status == "running"
    async with native_worker(c, app_user_engine) as process:
        checked = await wait_checked(c, url, str(task_id), process)
    assert checked.json()["execution_status"] == "completed", checked.text
    assert checked.json()["claim_version"] == 2
    replay = await c.client.post(url, json={"request_id": created.json()["request_id"]})
    assert replay.json()["id"] == str(task_id)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        assert task.attempt_count == 2 and task.metadata_json["reclaimed_expired_claim"] is True


async def test_readiness_native_browser(readiness_case, app_user_engine, tmp_path):
    import json
    import socket
    from pathlib import Path
    import uvicorn
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from app.api import auth, plans
    from app.core.security import create_access_token

    c = readiness_case
    if "HIVE_TEST_G85_FRONTEND_DIST" not in os.environ:
        pytest.skip("Built frontend required for the native browser")
    dist = Path(os.environ["HIVE_TEST_G85_FRONTEND_DIST"])
    assert (dist / "index.html").is_file()
    for router in (auth.router, plans.router):
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
            "agentId": str(c.agent), "teamId": str(c.team), "outputDir": str(tmp_path),
            "leadToken": create_access_token(str(c.lead.id), "user", str(c.tenant)),
            "ownerToken": create_access_token(str(c.owner.id), "user", str(c.tenant))}
        fixture_path = tmp_path / "browser-fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        frontend = Path(__file__).resolve().parents[3] / "frontend"
        process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/employee-readiness-native.mjs"), str(fixture_path),
            cwd=frontend, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def receive(expected):
            line = await asyncio.wait_for(process.stdout.readline(), 60)
            if not line:
                output, error = await process.communicate()
                pytest.fail(f"Readiness browser failed: {output!r} {error!r}")
            assert json.loads(line)["event"] == expected

        async def send(event):
            process.stdin.write((json.dumps({"event": event}) + "\n").encode())
            await process.stdin.drain()

        await receive("team_check_queued")
        async with native_worker(c, app_user_engine):
            await send("worker_started")
            await receive("team_check_recorded")
        c.holder["user"] = c.owner
        await send("owner_ready")
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


async def test_downgrade_preserves_existing_readiness_receipts(readiness_case, owner_engine, owner_sessionmaker):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.models.runtime_task import RuntimeTask

    c = readiness_case
    url = f"/api/business-teams/{c.team}/agents/{c.agent}/readiness-checks"
    receipt = (await c.client.post(url, json={"request_id": str(uuid.uuid4())})).json()
    path = Path(__file__).resolve().parents[2] / "alembic/versions/employee_readiness_probe_1018.py"
    spec = importlib.util.spec_from_file_location("employee_readiness_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()

    async with owner_engine.begin() as connection:
        with pytest.raises(RuntimeError, match="does not delete or relabel work"):
            await connection.run_sync(downgrade)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, uuid.UUID(receipt["id"]))
        assert task.task_type == "readiness_probe" and task.status == "pending"
