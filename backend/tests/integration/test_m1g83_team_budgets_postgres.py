"""Team budget requests do not approve themselves or alter company policy."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.database import get_db, pin_rls_tenant_context
from app.core.security import get_current_user
from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
from app.models.runtime_budget import RuntimeBudgetPolicy, RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from tests.integration.test_m1e_business_teams_postgres import _agent, _user


@pytest.fixture
async def budget_case(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.api.business_teams import router as team_router
    from app.api.runtime_budgets import router as budget_router, get_runtime_budget_service
    from app.services.runtime_budget_service import (
        RuntimeBudgetService, RuntimeBudgetRunCreate, RuntimeBudgetReservation, RuntimeBudgetApprovalRequired,
    )
    from app.services.session_writer_epoch import assign_runtime_task_writer_generation

    tenant_id, owner_id, lead_id, admin_id, team_id, task_id = [uuid.uuid4() for _ in range(6)]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Budget team", slug=f"budget-{tenant_id.hex}"))
        await db.flush()
        owner = _user(db, id=owner_id, tenant_id=tenant_id, username=f"owner-{owner_id.hex}", email=f"{owner_id.hex}@budget.test")
        lead = _user(db, id=lead_id, tenant_id=tenant_id, username=f"lead-{lead_id.hex}", email=f"{lead_id.hex}@budget.test")
        admin = _user(db, id=admin_id, tenant_id=tenant_id, username=f"admin-{admin_id.hex}", email=f"{admin_id.hex}@budget.test", role="org_admin")
        await db.flush()
        db.add(BusinessTeam(id=team_id, tenant_id=tenant_id, name="Delivery", created_by_user_id=owner_id, status="active"))
        await db.flush()
        db.add(BusinessTeamMembership(tenant_id=tenant_id, business_team_id=team_id, user_id=lead_id, role="lead", status="active"))
        agent = await _agent(db, tenant_id=tenant_id, owner_id=owner_id, name="Analyst")
        agent.home_business_team_id = team_id
        from app.models.chat_session import ChatSession

        session_id = uuid.uuid4()
        db.add(ChatSession(id=session_id, tenant_id=tenant_id, agent_id=agent.id, user_id=owner_id))
        await db.flush()
        task = RuntimeTask(id=task_id, tenant_id=tenant_id, parent_agent_id=agent.id, task_type="web_chat_turn",
            status="pending", root_user_id=owner_id, root_idempotency_key=f"budget-work:{task_id}",
            parent_session_id=str(session_id), root_session_id=str(session_id),
            prompt="PRIVATE_BUDGET_CANARY", config_snapshot_hash="synthetic", policy_snapshot_hash="synthetic")
        await assign_runtime_task_writer_generation(db, task)
        db.add(task)
        await db.commit()

    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    policy = await service.create_policy(tenant_id=tenant_id, name="Company bound", scope_type="agent",
        agent_id=agent.id, max_tokens=10, max_subagents=1, fail_mode="require_confirmation")
    run = await service.create_run(RuntimeBudgetRunCreate(tenant_id=tenant_id, root_run_kind="web_chat_turn",
        root_run_key="PRIVATE_BUDGET_CANARY", root_agent_id=agent.id, root_user_id=owner_id,
        root_runtime_task_id=task_id, max_tokens=10, max_subagents=1, policy_id=policy.id, fail_mode="require_confirmation",
        policy_snapshot={"private": "PRIVATE_BUDGET_CANARY"}))
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, task_id)).budget_run_id = run.id
        await db.commit()
    with pytest.raises(RuntimeBudgetApprovalRequired):
        await service.reserve(RuntimeBudgetReservation(budget_run_id=run.id, reservation_key="exact-work",
            tokens=20, subagents=2, runtime_task_id=task_id))
    current = await service.get_run(tenant_id=tenant_id, budget_run_id=run.id)
    assert current.status == "waiting_budget_approval" and current.approval_episode_id
    actor = {"user": lead}

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, actor["user"].tenant_id)
            yield db

    async def no_wakeup(**kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    app = FastAPI()
    app.include_router(team_router, prefix="/api")
    app.include_router(budget_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor["user"]
    app.dependency_overrides[get_db] = scoped_db
    app.dependency_overrides[get_runtime_budget_service] = lambda: service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://team-budget") as client:
        yield SimpleNamespace(client=client, actor=actor, lead=lead, owner=owner, admin=admin, tenant_id=tenant_id,
            team_id=team_id, task_id=task_id, agent_id=agent.id, run=run, current=current, policy=policy,
            service=service, app=app, path=f"/api/business-teams/{team_id}/runtime-budget-runs")


async def test_team_lead_requests_and_admin_approves_the_same_frozen_budget_work(budget_case, owner_sessionmaker, app_user_sessionmaker):
    from app.services.runtime_budget_service import RuntimeBudgetReservation

    c = budget_case
    client, run, current, policy, task_id, owner_id, service, actor, lead, admin, path = (
        c.client, c.run, c.current, c.policy, c.task_id, c.owner.id, c.service, c.actor, c.lead, c.admin, c.path)
    listed = await client.get(path)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["runs"]] == [str(run.id)]
    assert "PRIVATE_BUDGET_CANARY" not in listed.text and "root_user_id" not in listed.text
    assert (await client.get("/api/runtime-budgets/policies")).status_code == 403
    body = {"request_id": str(uuid.uuid4()), "approval_episode_id": str(current.approval_episode_id),
        "limits": {"max_tokens": 30, "max_subagents": 3}, "reason": "Continue the approved team analysis"}
    created = await client.post(path + f"/{run.id}/increase-requests", json=body)
    assert created.status_code == 201 and created.json()["status"] == "pending", created.text
    assert (await client.post(path + f"/{run.id}/increase-requests", json=body)).json()["id"] == body["request_id"]
    assert (await client.post(path + f"/{run.id}/increase-requests", json={**body, "limits": {"max_tokens": 40}})).status_code == 409
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeBudgetRun, run.id)).max_tokens == 10
        assert (await db.get(RuntimeBudgetPolicy, policy.id)).max_tokens == 10
        assert (await db.get(RuntimeTask, task_id)).budget_admission_status == "waiting_budget_approval"
    decision = {"approval_episode_id": body["approval_episode_id"], "increase_request_id": body["request_id"],
        "max_tokens": 30, "max_subagents": 3, "reason": "Reviewed exact team request"}
    url = f"/api/runtime-budgets/runs/{run.id}/approve-overrun"
    assert (await client.post(url, json=decision)).status_code == 403
    actor["user"] = admin
    approved = await client.post(url, json=decision)
    assert approved.status_code == 200 and approved.json()["status"] == "active", approved.text
    actor["user"] = lead
    returned = await client.get(path)
    assert returned.json()["runs"][0]["increase_requests"][0]["status"] == "approved", returned.text
    assert "PRIVATE_BUDGET_CANARY" not in returned.text
    async with owner_sessionmaker() as db:
        frozen = await db.get(RuntimeTask, task_id)
        assert frozen.root_user_id == owner_id and frozen.prompt == "PRIVATE_BUDGET_CANARY"
        assert frozen.budget_admission_status == "approved"
        assert (await db.get(RuntimeBudgetPolicy, policy.id)).max_tokens == 10
    replay = await service.reserve(RuntimeBudgetReservation(budget_run_id=run.id, reservation_key="exact-work",
        tokens=20, subagents=2, runtime_task_id=task_id))
    assert replay.allowed and replay.idempotent
    assert (await client.post(path + f"/{run.id}/increase-requests", json=body)).json()["status"] == "approved"
    assert (await client.post(path + f"/{run.id}/increase-requests", json={**body, "request_id": str(uuid.uuid4())})).status_code == 409
    actor["user"] = admin
    assert (await client.post(url, json=decision)).status_code == 200
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, c.tenant_id)
        claims = await RuntimeTaskClaimService(db=db, worker_id="budget-resume", task_types=("web_chat_turn",)).claim_available()
        assert [claim.id for claim in claims] == [task_id]
        assert not await RuntimeTaskClaimService(db=db, worker_id="other-worker", task_types=("web_chat_turn",)).claim_available()


def proposal(c, value=30):
    return {"request_id": str(uuid.uuid4()), "approval_episode_id": str(c.current.approval_episode_id),
        "limits": {"max_tokens": value, "max_subagents": 3}, "reason": "Continue team work"}


async def test_budget_request_withdrawal_exact_decisions_and_rejection(budget_case, owner_sessionmaker):
    from app.models.runtime_budget import RuntimeBudgetEvent
    from sqlalchemy import select, func

    c = budget_case
    endpoint = c.path + f"/{c.run.id}/increase-requests"
    body = proposal(c, 11)
    assert (await c.client.post(endpoint, json=body)).status_code == 201
    c.actor["user"] = c.admin
    decision_url = f"/api/runtime-budgets/runs/{c.run.id}/approve-overrun"
    decision = {"approval_episode_id": body["approval_episode_id"], "increase_request_id": body["request_id"], "max_tokens": 11, "max_subagents": 3}
    # Exact proposals cannot silently expand to cover the denied twenty-token reservation.
    assert (await c.client.post(decision_url, json=decision)).status_code == 409
    assert (await c.client.post(decision_url, json={**decision, "max_tokens": 30})).status_code == 409
    assert (await c.client.delete(endpoint + "/" + body["request_id"])).status_code == 404
    c.actor["user"] = c.lead
    for _ in range(2):
        result = await c.client.delete(endpoint + "/" + body["request_id"])
        assert result.status_code == 200 and result.json()["status"] == "withdrawn", result.text
    assert (await c.client.post(endpoint, json=body)).json()["status"] == "withdrawn"
    c.actor["user"] = c.admin
    assert (await c.client.post(decision_url, json=decision)).status_code == 409
    c.actor["user"] = c.lead
    body = proposal(c)
    assert (await c.client.post(endpoint, json=body)).status_code == 201
    c.actor["user"] = c.admin
    reject = {"approval_episode_id": body["approval_episode_id"], "increase_request_id": body["request_id"]}
    reject_url = decision_url.replace("approve-overrun", "reject-overrun")
    for _ in range(2):
        assert (await c.client.post(reject_url, json=reject)).status_code == 200
    c.actor["user"] = c.lead
    assert (await c.client.post(endpoint, json=body)).json()["status"] == "rejected"
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeBudgetRun, c.run.id)
        assert run.status == "stopped" and run.max_tokens == 10 and run.reserved_tokens == 0
        assert (await db.get(RuntimeBudgetPolicy, c.policy.id)).max_tokens == 10
        assert (await db.get(RuntimeTask, c.task_id)).budget_admission_status == "rejected"
        assert await db.scalar(select(func.count()).select_from(RuntimeBudgetEvent).where(
            RuntimeBudgetEvent.budget_run_id == run.id, RuntimeBudgetEvent.event_type == "increase_withdrawn")) == 1
        assert await db.scalar(select(func.count()).select_from(RuntimeBudgetEvent).where(
            RuntimeBudgetEvent.budget_run_id == run.id, RuntimeBudgetEvent.event_type == "overrun_rejected")) == 1


async def test_budget_team_source_membership_and_policy_isolation(budget_case, owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.user import User
    from sqlalchemy import select

    c = budget_case
    c.actor["user"] = c.owner
    assert (await c.client.get(c.path)).status_code == 404
    c.actor["user"] = c.lead
    for changed in ("member", "left", "suspended_team", "inactive"):
        async with owner_sessionmaker() as db:
            membership = await db.scalar(select(BusinessTeamMembership).where(BusinessTeamMembership.user_id == c.lead.id))
            membership.role = "member" if changed == "member" else "lead"
            membership.status = "left" if changed == "left" else "active"
            (await db.get(BusinessTeam, c.team_id)).status = "suspended" if changed == "suspended_team" else "active"
            (await db.get(User, c.lead.id)).is_active = changed != "inactive"
            await db.commit()
        expected = {"member": 403, "left": 404, "suspended_team": 403, "inactive": 401}[changed]
        assert (await c.client.get(c.path)).status_code == expected
        assert (await c.client.post(c.path + f"/{c.run.id}/increase-requests", json=proposal(c))).status_code == expected
    async with owner_sessionmaker() as db:
        (await db.get(User, c.lead.id)).is_active = True
        other = BusinessTeam(id=uuid.uuid4(), tenant_id=c.tenant_id, name="Other team", created_by_user_id=c.owner.id)
        db.add(other)
        await db.flush()
        db.add(BusinessTeamMembership(tenant_id=c.tenant_id, business_team_id=other.id, user_id=c.lead.id, role="lead", status="active"))
        await db.commit()
    assert (await c.client.get(c.path.replace(str(c.team_id), str(other.id)) + f"/{c.run.id}")).status_code == 404
    request = proposal(c)
    request["reason"] = "ORIGINAL_TEAM_REQUEST_CANARY"
    assert (await c.client.post(c.path + f"/{c.run.id}/increase-requests", json=request)).status_code == 201
    async with owner_sessionmaker() as db:
        (await db.get(Agent, c.agent_id)).home_business_team_id = other.id
        await db.commit()
    moved = await c.client.get(c.path.replace(str(c.team_id), str(other.id)))
    assert moved.status_code == 200 and len(moved.json()["runs"]) == 1
    assert "ORIGINAL_TEAM_REQUEST_CANARY" not in moved.text
    c.actor["user"] = c.admin
    assert (await c.client.post(f"/api/runtime-budgets/runs/{c.run.id}/approve-overrun", json={
        "approval_episode_id": request["approval_episode_id"], "increase_request_id": request["request_id"],
        "max_tokens": 30, "max_subagents": 3})).status_code == 409


async def test_budget_increase_rejects_unstorable_and_noninteger_limits(budget_case):
    c = budget_case
    for limits in ({"max_tokens": 2**63}, {"max_tokens": True}, {"max_tokens": "30"}, {"max_tokens": -1},
        {"unknown": 30}, {"max_tokens": 10}, {"max_tokens": 30.1}, {"max_subagents": 2**31}):
        result = await c.client.post(c.path + f"/{c.run.id}/increase-requests", json={**proposal(c), "limits": limits})
        assert result.status_code == 422, result.text


async def test_budget_task_team_uses_exact_canonical_task_binding(budget_case, owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.task import Task
    from app.services.business_task_runtime import business_task_runtime_root_key

    c = budget_case
    async with owner_sessionmaker() as db:
        (await db.get(Agent, c.agent_id)).home_business_team_id = None
        task = Task(id=uuid.uuid4(), tenant_id=c.tenant_id, agent_id=c.agent_id, title="Team work",
            created_by=c.owner.id, business_team_id=c.team_id, request_id="canonical-task", request_hash="synthetic")
        db.add(task)
        runtime = await db.get(RuntimeTask, c.task_id)
        runtime.task_type = "business_task"
        metadata = {"schema": "hive.business_task_run.v1", "business_task_id": str(task.id),
            "requester_user_id": str(c.owner.id), "request_id": "exact-attempt"}
        runtime.metadata_json = metadata
        runtime.root_idempotency_key = business_task_runtime_root_key(task_id=task.id, request_id="exact-attempt")
        await db.commit()
    result = await c.client.get(c.path + "?limit=1")
    assert [row["id"] for row in result.json()["runs"]] == [str(c.run.id)]
    assert result.json()["next_offset"] == 1
    assert (await c.client.get(c.path + "?offset=1&limit=1")).json()["next_offset"] is None
    for field, value in (("schema", "forged"), ("requester_user_id", str(c.lead.id)), ("request_id", "different-attempt")):
        async with owner_sessionmaker() as db:
            (await db.get(RuntimeTask, c.task_id)).metadata_json = {**metadata, field: value}
            await db.commit()
        listed = await c.client.get(c.path + "?limit=1")
        assert listed.status_code == 200 and listed.json()["runs"] == []
        assert listed.json()["next_offset"] == 1  # Unknown bindings do not hide a later valid page.
        assert (await c.client.get(c.path + f"/{c.run.id}")).status_code == 404
    foreign_tenant, foreign_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=foreign_tenant, name="Foreign", slug=f"foreign-{foreign_tenant}"))
        await db.flush()
        foreign = _user(db, id=foreign_id, tenant_id=foreign_tenant, username=f"foreign-{foreign_id}", email=f"{foreign_id}@budget.test", role="org_admin")
        await db.commit()
    c.actor["user"] = foreign
    assert (await c.client.get(c.path)).status_code == 404
    assert (await c.client.get(f"/api/runtime-budgets/runs/{c.run.id}")).status_code == 404


async def test_team_budget_native_browser(budget_case, owner_sessionmaker, app_user_sessionmaker, tmp_path):
    import asyncio
    import json
    import os
    import socket
    from pathlib import Path
    import uvicorn
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from app.api import agents, auth
    from app.core.security import create_access_token
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    c = budget_case
    if "HIVE_TEST_G83_FRONTEND_DIST" not in os.environ:
        pytest.skip("Built frontend is required for the native browser case")
    dist = Path(os.environ["HIVE_TEST_G83_FRONTEND_DIST"])
    assert (dist / "index.html").is_file()
    for router in (auth.router, agents.router):
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
        fixture = {"origin": f"http://127.0.0.1:{listener.getsockname()[1]}", "tenantId": str(c.tenant_id),
            "teamId": str(c.team_id), "runId": str(c.run.id), "token": create_access_token(str(c.lead.id), "user", str(c.tenant_id)),
            "adminToken": create_access_token(str(c.admin.id), "user", str(c.tenant_id)), "outputDir": str(tmp_path)}
        fixture_path = tmp_path / "browser-fixture.json"
        fixture_path.write_text(json.dumps(fixture))
        frontend = Path(__file__).resolve().parents[3] / "frontend"
        process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/team-budget-native.mjs"), str(fixture_path),
            cwd=frontend, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def receive(expected):
            message = await asyncio.wait_for(process.stdout.readline(), 60)
            if not message:
                output, error = await process.communicate()
                pytest.fail(f"Browser failed: {output!r} {error!r}")
            assert json.loads(message)["event"] == expected, message

        async def send(event):
            process.stdin.write((json.dumps({"event": event}) + "\n").encode())
            await process.stdin.drain()

        await receive("increase_requested")
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeBudgetRun, c.run.id)).max_tokens == 10
            assert (await db.get(RuntimeTask, c.task_id)).budget_admission_status == "waiting_budget_approval"
        c.actor["user"] = c.admin
        await send("admin_ready")
        await receive("increase_approved")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, c.tenant_id)
            claims = await RuntimeTaskClaimService(db=db, worker_id="browser-budget", task_types=("web_chat_turn",)).claim_available()
            assert [claim.id for claim in claims] == [c.task_id]
        async with owner_sessionmaker() as db:
            frozen = await db.get(RuntimeTask, c.task_id)
            assert frozen.root_user_id == c.owner.id and frozen.prompt == "PRIVATE_BUDGET_CANARY"
            assert (await db.get(RuntimeBudgetPolicy, c.policy.id)).max_tokens == 10
        c.actor["user"] = c.lead
        await send("lead_ready")
        output, error = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, (output, error)
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
