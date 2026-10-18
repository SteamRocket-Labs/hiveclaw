"""Explicit team task disclosure does not transfer private work or effect authority."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
from app.models.runtime_task import RuntimeTask
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from tests.integration.test_m1e_business_teams_postgres import _agent, _user


@pytest.fixture
async def handover_case(owner_sessionmaker, app_user_sessionmaker):
    tenant, team_id, session_id = [uuid.uuid4() for _ in range(3)]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name="Task team", slug=f"handover-{tenant}"))
        await db.flush()
        people = {}
        for name in ("Owner", "Lead", "Successor", "Outsider"):
            person_id = uuid.uuid4()
            people[name] = _user(db, id=person_id, tenant_id=tenant, username=f"{name}-{person_id.hex}", email=f"{person_id}@task.test")
        await db.flush()
        owner, lead, successor = (people[key] for key in ("Owner", "Lead", "Successor"))
        db.add(BusinessTeam(id=team_id, tenant_id=tenant, name="Delivery", created_by_user_id=lead.id, status="active"))
        await db.flush()
        for person in (owner, lead, successor):
            db.add(BusinessTeamMembership(tenant_id=tenant, business_team_id=team_id, user_id=person.id,
                role="lead" if person.id == lead.id else "member", status="active"))
        agent = await _agent(db, tenant_id=tenant, owner_id=owner.id, name="Analyst")
        db.add(ChatSession(id=session_id, tenant_id=tenant, agent_id=agent.id, user_id=owner.id))
        await db.flush()
        task = Task(id=uuid.uuid4(), tenant_id=tenant, agent_id=agent.id, title="Deliver analysis",
            description="Explicit shared goal", created_by=owner.id, root_session_id=session_id,
            request_id="original-task", request_hash="original", authority_state="owned")
        db.add(task)
        await db.flush()
        runtime = RuntimeTask(id=uuid.uuid4(), tenant_id=tenant, parent_agent_id=agent.id, task_type="business_task",
            status="pending", root_user_id=owner.id, parent_session_id=str(session_id), prompt="PRIVATE_TASK_CANARY",
            root_idempotency_key=f"private:{task.id}", metadata_json={"business_task_id": str(task.id), "requester_user_id": str(owner.id)})
        db.add(runtime)
        await db.flush()
        task.active_runtime_task_id = runtime.id
        db.add(TaskLog(tenant_id=tenant, task_id=task.id, content="PRIVATE_TASK_CANARY"))
        triggers = []
        for person in (owner, successor):
            row = AgentTrigger(id=uuid.uuid4(), tenant_id=tenant, agent_id=agent.id, name=f"Private routine {person.id}",
                type="cron", is_enabled=True, config={"created_by": str(person.id), "authority_state": "owned", "cron": "0 9 * * *"})
            db.add(row)
            triggers.append(row.id)
        await db.commit()
        await db.refresh(task)
    from app.api import business_teams, tasks, chat_sessions

    actor = {"user": owner}
    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, actor["user"].tenant_id)
            yield db
    app = FastAPI()
    for router in (business_teams.router, tasks.router, chat_sessions.router):
        app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = scoped_db
    app.dependency_overrides[get_current_user] = lambda: actor["user"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://task-handover") as client:
        yield SimpleNamespace(client=client, app=app, actor=actor, owner=owner, lead=lead, successor=successor,
            outsider=people["Outsider"], tenant=tenant, team=team_id, agent=agent.id, task=task, runtime=runtime.id,
            session=session_id, triggers=triggers)


async def test_explicit_task_share_team_handover_and_revoke_preserve_original_authority(handover_case, owner_sessionmaker):
    c = handover_case
    share_url = f"/api/agents/{c.agent}/tasks/{c.task.id}/team-sharing"
    queue_url = f"/api/business-teams/{c.team}/tasks"
    body = {"request_id": str(uuid.uuid4()), "business_team_id": str(c.team), "responsible_user_id": str(c.owner.id),
        "expected_updated_at": c.task.updated_at.isoformat(), "reason": "Share this task goal and responsibility"}
    created = await c.client.post(share_url, json=body)
    assert created.status_code == 200, created.text
    assert (await c.client.post(share_url, json=body)).json()["receipt_id"] == body["request_id"]
    async with owner_sessionmaker() as db:
        (await db.get(Task, c.task.id)).description = "PRIVATE_TASK_CANARY added after the explicit disclosure"
        await db.commit()
    c.actor["user"] = c.lead
    listed = await c.client.get(queue_url)
    assert listed.status_code == 200 and [row["id"] for row in listed.json()["tasks"]] == [str(c.task.id)], listed.text
    assert "PRIVATE_TASK_CANARY" not in listed.text and "root_session_id" not in listed.text
    assert listed.json()["tasks"][0]["description"] == "Explicit shared goal"
    current = listed.json()["tasks"][0]
    handover = {"request_id": str(uuid.uuid4()), "responsible_user_id": str(c.successor.id),
        "expected_updated_at": current["updated_at"], "reason": "Continue with the next responsible teammate"}
    moved = await c.client.post(queue_url + f"/{c.task.id}/handover", json=handover)
    assert moved.status_code == 200 and moved.json()["paused_routine_count"] == 1, moved.text
    assert (await c.client.post(queue_url + f"/{c.task.id}/handover", json=handover)).json()["receipt_id"] == handover["request_id"]
    c.actor["user"] = c.successor
    own_queue = await c.client.get(queue_url)
    assert own_queue.status_code == 200 and len(own_queue.json()["tasks"]) == 1
    # Team responsibility is not use/manage access to the employee or an old private task/session.
    assert (await c.client.get(f"/api/agents/{c.agent}/tasks/{c.task.id}")).status_code in (403, 404)
    async with owner_sessionmaker() as db:
        task = await db.get(Task, c.task.id)
        frozen = await db.get(RuntimeTask, c.runtime)
        assert task.created_by == c.owner.id and task.responsible_user_id == c.successor.id
        assert frozen.root_user_id == c.owner.id and frozen.prompt == "PRIVATE_TASK_CANARY"
        assert (await db.get(Agent, c.agent)).owner_user_id == c.owner.id
        assert not (await db.get(AgentTrigger, c.triggers[0])).is_enabled
        assert (await db.get(AgentTrigger, c.triggers[1])).is_enabled
    c.actor["user"] = c.owner
    revoke = {"request_id": str(uuid.uuid4()), "business_team_id": None, "responsible_user_id": None,
        "expected_updated_at": moved.json()["task"]["updated_at"], "reason": "Return this task to private work"}
    revoked = await c.client.post(share_url, json=revoke)
    assert revoked.status_code == 200, revoked.text
    c.actor["user"] = c.lead
    assert (await c.client.get(queue_url)).json()["tasks"] == []
    c.actor["user"] = c.successor
    assert (await c.client.get(queue_url)).json()["tasks"] == []


async def test_team_task_handover_scope_versions_and_unknown_disclosure(handover_case, owner_sessionmaker):
    from sqlalchemy import select
    from app.models.audit import AuditLog
    from app.models.security_audit import ResourcePermission
    from app.models.user import User

    c = handover_case
    queue = f"/api/business-teams/{c.team}/tasks"
    share = f"/api/agents/{c.agent}/tasks/{c.task.id}/team-sharing"
    c.actor["user"] = c.lead
    assert (await c.client.get(queue)).json()["tasks"] == []
    # Typed legacy fields alone cannot manufacture a disclosure receipt.
    async with owner_sessionmaker() as db:
        row = await db.get(Task, c.task.id)
        row.business_team_id, row.audience_kind = c.team, "business_team"
        await db.commit()
    assert (await c.client.get(queue)).json()["tasks"] == []
    c.actor["user"] = c.owner
    current = (await c.client.get(f"/api/agents/{c.agent}/tasks/{c.task.id}")).json()["task"]
    body = {"request_id": str(uuid.uuid4()), "business_team_id": str(c.team), "responsible_user_id": str(c.owner.id),
        "expected_updated_at": current["updated_at"], "reason": "Share reviewed goal"}
    result = await c.client.post(share, json=body)
    assert result.status_code == 200, result.text
    handover = {"request_id": str(uuid.uuid4()), "responsible_user_id": str(c.successor.id),
        "expected_updated_at": result.json()["task"]["updated_at"], "reason": "Transfer responsibility"}
    c.actor["user"] = c.owner
    assert (await c.client.post(queue + f"/{c.task.id}/handover", json=handover)).status_code == 403
    c.actor["user"] = c.lead
    assert (await c.client.post(queue + f"/{c.task.id}/handover", json={**handover, "responsible_user_id": str(c.outsider.id)})).status_code == 409
    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(tenant_id=c.tenant, principal_type="user", principal_id=c.lead.id,
            resource_type="agent", resource_id=c.agent, actions=["read", "execute"], effect="allow"))
        db.add(ResourcePermission(tenant_id=c.tenant, principal_type="user", principal_id=c.lead.id,
            resource_type="task", resource_id=c.task.id, actions=["read", "write"], effect="allow"))
        await db.commit()
    # A write grant permits editing, not publishing another creator's private task.
    assert (await c.client.post(share, json={**body, "request_id": str(uuid.uuid4())})).status_code == 403
    moved = await c.client.post(queue + f"/{c.task.id}/handover", json=handover)
    assert moved.status_code == 200, moved.text
    assert (await c.client.post(queue + f"/{c.task.id}/handover", json={**handover, "request_id": str(uuid.uuid4())})).status_code == 409
    assert (await c.client.post(queue + f"/{c.task.id}/handover", json={**handover, "responsible_user_id": str(c.owner.id)})).status_code == 409
    async with owner_sessionmaker() as db:
        (await db.get(User, c.successor.id)).is_active = False
        await db.commit()
    assert (await c.client.post(queue + f"/{c.task.id}/handover", json={**handover, "request_id": str(uuid.uuid4()),
        "expected_updated_at": moved.json()["task"]["updated_at"]})).status_code == 409
    async with owner_sessionmaker() as db:
        member = await db.scalar(select(BusinessTeamMembership).where(BusinessTeamMembership.user_id == c.lead.id))
        member.status = "left"
        await db.commit()
        events = (await db.scalars(select(AuditLog).where(AuditLog.action == "task:handover", AuditLog.tenant_id == c.tenant))).all()
        assert len(events) == 1
    assert (await c.client.get(queue)).status_code == 404
    assert (await c.client.post(queue + f"/{c.task.id}/handover", json=handover)).status_code == 404


async def test_task_handover_native_browser(handover_case, owner_sessionmaker, tmp_path):
    import asyncio
    import json
    import os
    import socket
    from pathlib import Path
    import uvicorn
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from app.api import agents, auth, plans
    from app.core.security import create_access_token
    from app.services.business_teams import affiliate_agent

    c = handover_case
    if "HIVE_TEST_G84_FRONTEND_DIST" not in os.environ:
        pytest.skip("Built frontend required for the native browser")
    dist = Path(os.environ["HIVE_TEST_G84_FRONTEND_DIST"])
    assert (dist / "index.html").is_file()
    for router in (agents.router, auth.router, plans.router):
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
            "agentId": str(c.agent), "taskId": str(c.task.id), "teamId": str(c.team), "ownerId": str(c.owner.id),
            "successorId": str(c.successor.id), "outputDir": str(tmp_path),
            **{f"{key}Token": create_access_token(str(person.id), "user", str(c.tenant))
                for key, person in (("owner", c.owner), ("lead", c.lead), ("successor", c.successor))}}
        fixture_path = tmp_path / "browser-fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        frontend = Path(__file__).resolve().parents[3] / "frontend"
        process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/task-handover-native.mjs"), str(fixture_path),
            cwd=frontend, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def receive(expected):
            line = await asyncio.wait_for(process.stdout.readline(), 60)
            if not line:
                output, error = await process.communicate()
                pytest.fail(f"Task browser failed: {output!r} {error!r}")
            message = json.loads(line)
            assert message["event"] == expected, message
            return message

        async def send(event):
            process.stdin.write((json.dumps({"event": event}) + "\n").encode())
            await process.stdin.drain()

        await receive("task_shared")
        async with owner_sessionmaker() as db:
            (await db.get(Task, c.task.id)).description = "PRIVATE_TASK_CANARY added after disclosure"
            await db.commit()
        c.actor["user"] = c.lead
        await send("lead_ready")
        await receive("task_transferred")
        async with owner_sessionmaker() as db:
            task = await db.get(Task, c.task.id)
            assert task.created_by == c.owner.id and task.responsible_user_id == c.successor.id
            assert (await db.get(Agent, c.agent)).owner_user_id == c.owner.id
            assert not (await db.get(AgentTrigger, c.triggers[0])).is_enabled
        c.actor["user"] = c.successor
        await send("successor_ready")
        await receive("access_required")
        async with owner_sessionmaker() as db:
            await affiliate_agent(db, c.owner, c.team, c.agent)
            await db.commit()
        await send("access_ready")
        opened = await receive("private_work_opened")
        async with owner_sessionmaker() as db:
            session = await db.get(ChatSession, uuid.UUID(opened["session_id"]))
            assert session.id != c.session and session.user_id == c.successor.id and session.audience_kind in (None, "private")
            assert session.audience_ref is None and session.parent_session_id is None and session.group_installation_ref is None
        assert (await c.client.get(f"/api/agents/{c.agent}/sessions/{c.session}")).status_code in (403, 404)
        assert (await c.client.get(f"/api/agents/{c.agent}/tasks/{c.task.id}")).status_code in (403, 404)
        c.actor["user"] = c.owner
        await send("owner_ready")
        await receive("task_unshared")
        async with owner_sessionmaker() as db:
            task = await db.get(Task, c.task.id)
            runtime = await db.get(RuntimeTask, c.runtime)
            assert task.business_team_id is None and task.responsible_user_id is None and task.created_by == c.owner.id
            assert runtime.root_user_id == c.owner.id and runtime.prompt == "PRIVATE_TASK_CANARY"
        c.actor["user"] = c.successor
        await send("revocation_ready")
        output, error = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, (output, error)
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()


async def test_handover_rechecks_lead_after_waiting_for_the_task_lock(handover_case, owner_sessionmaker):
    import asyncio
    from sqlalchemy import select, text

    c = handover_case
    share = await c.client.post(f"/api/agents/{c.agent}/tasks/{c.task.id}/team-sharing", json={
        "request_id": str(uuid.uuid4()), "business_team_id": str(c.team), "responsible_user_id": str(c.owner.id),
        "expected_updated_at": c.task.updated_at.isoformat(), "reason": "Publish this reviewed task"})
    assert share.status_code == 200, share.text
    c.actor["user"] = c.lead
    request = None
    async with owner_sessionmaker() as lock_db:
        await lock_db.get(Task, c.task.id, with_for_update=True)
        blocker_pid = await lock_db.scalar(text("SELECT pg_backend_pid()"))
        try:
            request = asyncio.create_task(c.client.post(f"/api/business-teams/{c.team}/tasks/{c.task.id}/handover", json={
                "request_id": str(uuid.uuid4()), "responsible_user_id": str(c.successor.id),
                "expected_updated_at": share.json()["task"]["updated_at"], "reason": "Transfer the reviewed task"}))
            async with owner_sessionmaker() as control_db:
                for _ in range(500):
                    await control_db.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = await control_db.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE :blocker = ANY(pg_blocking_pids(pid))"), {"blocker": blocker_pid})
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
                assert waiting and not request.done(), f"The task lock must block the request: {request.result().text if request.done() else 'pending'}"
                member = await control_db.scalar(select(BusinessTeamMembership).where(BusinessTeamMembership.user_id == c.lead.id))
                member.status = "left"
                await control_db.commit()
            await lock_db.commit()
            result = await asyncio.wait_for(request, 10)
            assert result.status_code == 404, result.text
        finally:
            await lock_db.rollback()
            if request is not None and not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)


async def test_scoped_admin_task_disclosure_and_suspended_team_recovery(handover_case, owner_sessionmaker):
    c = handover_case
    async with owner_sessionmaker() as db:
        admin_id, foreign_tenant, foreign_id = [uuid.uuid4() for _ in range(3)]
        db.add(Tenant(id=foreign_tenant, name="Other company", slug=f"other-{foreign_tenant}"))
        await db.flush()
        admin = _user(db, id=admin_id, tenant_id=c.tenant, username=f"admin-{admin_id}", email=f"{admin_id}@task.test", role="org_admin")
        foreign = _user(db, id=foreign_id, tenant_id=foreign_tenant, username=f"foreign-{foreign_id}", email=f"{foreign_id}@task.test", role="org_admin")
        await db.commit()
    share = f"/api/agents/{c.agent}/tasks/{c.task.id}/team-sharing"
    queue = f"/api/business-teams/{c.team}/tasks"
    body = {"request_id": str(uuid.uuid4()), "business_team_id": str(c.team), "responsible_user_id": str(c.successor.id),
        "expected_updated_at": c.task.updated_at.isoformat(), "reason": "Administrator approved this task disclosure"}
    c.actor["user"] = foreign
    assert (await c.client.get(queue)).status_code == 404
    assert (await c.client.post(share, json=body)).status_code in (403, 404)
    c.actor["user"] = admin
    shared = await c.client.post(share, json=body)
    assert shared.status_code == 200, shared.text
    async with owner_sessionmaker() as db:
        assert (await db.get(Task, c.task.id)).created_by == c.owner.id
        (await db.get(BusinessTeam, c.team)).status = "suspended"
        await db.commit()
    assert (await c.client.get(queue)).status_code == 200
    assert (await c.client.post(queue + f"/{c.task.id}/handover", json={
        "request_id": str(uuid.uuid4()), "responsible_user_id": str(c.owner.id),
        "expected_updated_at": shared.json()["task"]["updated_at"], "reason": "No new assignment in a suspended team"})).status_code == 409
    c.actor["user"] = c.owner
    assert (await c.client.post(share, json={"request_id": str(uuid.uuid4()), "business_team_id": None,
        "responsible_user_id": None, "expected_updated_at": shared.json()["task"]["updated_at"],
        "reason": "Recover private work from the suspended team"})).status_code == 200
