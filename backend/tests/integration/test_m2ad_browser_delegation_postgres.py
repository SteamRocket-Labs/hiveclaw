"""Delegation expiry remains effective at each native browser protocol step."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest

from app.models.environment_browser import EnvironmentBrowser
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_rpc_postgres import browser_redis as browser_redis
from tests.integration.test_m2ad_browser_tools_postgres import browser_tools as browser_tools
from tests.integration.test_m2ad_browser_tools_postgres import operation

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("remote", [False, True])
async def test_delegation_expiry_between_click_steps_retains_unknown_without_replay(
    browser_case, browser_tools, browser_redis, owner_sessionmaker, monkeypatch, remote
):
    from app.agents import delegation_token
    from app.services.browser_control import BrowserControlService
    from app.tools.handlers import browser

    seed, source, owner, _, _ = browser_case
    view = json.loads(str(await browser_tools("browser_manage", operation("start"))))["browser"]
    await browser_tools("browser_observe")
    if remote:
        monkeypatch.setattr(browser, "browser_control", BrowserControlService())
    clock = [delegation_token.time.time()]
    token = delegation_token.issue_delegation_token(
        parent_agent_id=uuid4(), child_agent_id=seed["agent_id"],
        granted_capabilities=frozenset({"computer.browser.control"}), ttl_seconds=60, now=clock[0],
    )
    monkeypatch.setattr(delegation_token.time, "time", lambda: clock[0])
    pipe = owner._pages[UUID(view["id"])].pipe
    original = pipe.call
    phases = []

    async def expire_after_press(method, parameters, **kwargs):
        result = await original(method, parameters, **kwargs)
        if method == "Input.dispatchMouseEvent":
            phases.append(parameters["type"])
            clock[0] = token.expires_at
        return result

    monkeypatch.setattr(pipe, "call", expire_after_press)
    trace = {}
    call_id = str(uuid4())
    args = operation("click", view, x=1, y=2)
    result = await browser_tools(
        "browser_action", args, tool_call_id=call_id, delegation_token=token, trace_metadata_sink=trace,
    )
    assert phases == ["mousePressed"]
    assert "browser_delegation_revoked" in str(result)
    assert '"effect": "unknown"' in str(result)
    assert trace["tool_execution_frame"]["status"] == "unknown"
    if remote:
        await asyncio.wait_for(asyncio.gather(*list(owner._rpc_jobs.values())), 5)
    async with owner_sessionmaker() as db:
        row = await db.get(EnvironmentBrowser, UUID(view["id"]))
        assert row.status == "lost"
        assert row.last_effect["effect"] == "unknown"
    current = await owner.read(source)
    assert current["status"] == "lost"
    repeated = await browser_tools("browser_action", args, tool_call_id=call_id, delegation_token=token)
    assert phases == ["mousePressed"]
    assert "delegation token expired" in str(repeated)
    stopped = await owner.execute(
        source, action="stop", request_id=uuid4(),
        expected_revision=current["revision"], expected_epoch=current["control_epoch"],
    )
    assert stopped["browser"]["status"] == "stopped"


async def _delegated_work(seed, owner_sessionmaker, *, owner_user_id=None):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.agents.orchestrator import AgentDelegationRequest, _delegation_authority_snapshot
    from app.core.execution_context import A2A_TOOL_AUTHORITY_FRAME_SCHEMA, A2AToolAuthorityFrame, ExecutionPrincipal
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.runtime.ccplus_contracts import build_permission_profile, permission_profile_snapshot_hash
    from app.services.execution_receipts import canonical_payload_hash

    parent_id, root_id, session_id, task_id = (uuid4() for _ in range(4))
    profile = build_permission_profile({"mode": "bypassPermissions"})
    request = AgentDelegationRequest(
        target=SimpleNamespace(id=seed["agent_id"], tenant_id=seed["tenant_id"]), target_model=None,
        conversation_messages=[], owner_id=seed["user_id"], session_id=str(session_id),
        parent_agent_id=parent_id, parent_session_id=str(root_id), runtime_task_id=str(task_id),
        tenant_id=seed["tenant_id"], trace_id=f"browser-delegation:{task_id}", permission_profile=profile,
        execution_principal=ExecutionPrincipal(
            tenant_id=seed["tenant_id"], source_agent_id=parent_id, requester_user_id=seed["user_id"],
            root_session_id=str(root_id), origin="browser-delegation-test",
        ).to_evidence(),
    )
    snapshot = _delegation_authority_snapshot(request)
    principal = ExecutionPrincipal.from_evidence(snapshot["execution_principal"])
    from app.agents.delegation_token import issue_delegation_token

    token = issue_delegation_token(parent_agent_id=parent_id, child_agent_id=seed["agent_id"])
    frame = A2AToolAuthorityFrame(
        schema=A2A_TOOL_AUTHORITY_FRAME_SCHEMA, principal=principal,
        capability_snapshot_hash=canonical_payload_hash(snapshot), capability_snapshot=snapshot,
        policy_snapshot_hash=permission_profile_snapshot_hash(profile), permission_profile=profile,
        delegation_token=token, session_id=str(session_id), parent_session_id=str(root_id),
        runtime_task_id=str(task_id), trace_id=request.trace_id, delegation_id=token.delegation_id,
        sandbox_profile=snapshot["permission_profile"]["sandbox"],
        approval_policy=snapshot["permission_profile"]["approval_policy"],
    )
    async with owner_sessionmaker() as db:
        db.add(Agent(id=parent_id, tenant_id=seed["tenant_id"], name="Browser Coordinator",
                     creator_id=seed["user_id"], owner_user_id=owner_user_id or seed["user_id"]))
        await db.flush()
        db.add(ChatSession(id=root_id, tenant_id=seed["tenant_id"], agent_id=parent_id, user_id=seed["user_id"]))
        await db.flush()
        db.add(RuntimeTask(
            id=task_id, tenant_id=seed["tenant_id"], task_type="delegation", status="running",
            parent_agent_id=parent_id, child_agent_id=seed["agent_id"], parent_session_id=str(root_id),
            child_session_id=str(session_id), root_user_id=seed["user_id"], root_session_id=str(root_id),
            delegation_chain_json=list(principal.delegation_chain), prompt="Use the delegated private browser.",
            claimed_by="kernel-worker-1", claim_version=1, attempt_count=1,
            claim_expires_at=datetime.now(UTC) + timedelta(minutes=10), metadata_json={},
        ))
        await db.flush()
        db.add(ChatSession(
            id=session_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=seed["user_id"],
            source_channel="agent", session_kind="delegation_run", runtime_source="delegation", actor_type="agent",
            peer_agent_id=parent_id, parent_session_id=root_id, root_session_id=root_id, runtime_task_id=task_id,
        ))
        await db.commit()
    seed.update(run_id=task_id, session_id=session_id)
    return frame, parent_id


@pytest.mark.parametrize("remote", [False, True])
async def test_delegated_browser_keeps_session_read_only_and_current_parent_authority(
    browser_case, browser_tools, browser_redis, owner_sessionmaker, app_user_sessionmaker, monkeypatch, remote
):
    from datetime import UTC, datetime

    from fastapi import HTTPException

    from app.database import pin_rls_tenant_context
    from app.models.agent import Agent
    from app.models.user import User
    from app.services.browser_control import BrowserControlService, BrowserSource
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.session_v2_persistence import resolve_session_mutation_authority
    from app.tools.handlers import browser

    seed, _, owner, client, root = browser_case
    frame, parent_id = await _delegated_work(seed, owner_sessionmaker)
    source = BrowserSource(*(seed[key] for key in ("tenant_id", "agent_id", "user_id", "session_id")))
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)

    async def execute(name, arguments=None, **kwargs):
        return await browser_tools(name, arguments, authority_frame=frame,
                                   permission_profile=frame.permission_profile, delegation_token=frame.delegation_token, **kwargs)

    try:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            user = await db.get(User, seed["user_id"])
            with pytest.raises(HTTPException) as read_only:
                await resolve_session_mutation_authority(db, user=user, agent_id=seed["agent_id"],
                                                        session_id=seed["session_id"], action="mutate_session_input")
            assert read_only.value.status_code == 409
            assert read_only.value.detail["code"] == "session_read_only"
        response = await execute("browser_manage", operation("start"))
        assert str(response).startswith("{"), str(response)
        started = json.loads(str(response))
        assert started.get("ok") is True, started
        view = started["browser"]
        if remote:
            monkeypatch.setattr(browser, "browser_control", BrowserControlService())
        await execute("browser_observe")
        effect = await execute("browser_action", operation("type", view, text="delegated-effect"))
        assert json.loads(str(effect))["ok"] is True, effect
        assert (root / view["id"] / "effects").read_text().splitlines() == ["delegated-effect"]
        path = f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/browser"
        scope = {"expected_user_id": str(seed["user_id"]), "expected_tenant_id": str(seed["tenant_id"])}
        read = await client.get(path, params=scope)
        assert read.status_code == 200, read.text
        current = read.json()["browser"]
        body = {**scope, "request_id": str(uuid4()), "expected_revision": current["revision"],
                "expected_epoch": current["control_epoch"]}
        taken = await client.post(path + "/take_control", json=body)
        assert taken.status_code == 200, taken.text
        current = taken.json()["browser"]
        assert current["control_mode"] == "human"
        returned = await client.post(path + "/return_control", json={
            **body, "request_id": str(uuid4()), "expected_revision": current["revision"],
            "expected_epoch": current["control_epoch"],
        })
        assert returned.status_code == 200, returned.text
        view = returned.json()["browser"]
        assert view["fresh_observation_required"] is True
        previous_monitor = owner._monitors[UUID(view["id"])]
        stopped = await owner.execute(source, action="stop", request_id=uuid4(),
                                      expected_revision=view["revision"], expected_epoch=view["control_epoch"])
        assert stopped["browser"]["status"] == "stopped"
        await asyncio.wait_for(previous_monitor, 5)
        caller = browser.browser_control
        monkeypatch.setattr(browser, "browser_control", owner)
        restarted = await execute("browser_manage", operation("start", stopped["browser"]))
        view = json.loads(str(restarted))["browser"]
        monkeypatch.setattr(browser, "browser_control", caller)
        await execute("browser_observe")
        pipe = owner._pages[UUID(view["id"])].pipe
        monitor = owner._monitors[UUID(view["id"])]
        original = pipe.call
        phases = []

        async def revoke_parent_after_press(method, parameters, **kwargs):
            result = await original(method, parameters, **kwargs)
            if method == "Input.dispatchMouseEvent":
                phases.append(parameters["type"])
                async with owner_sessionmaker() as db:
                    parent = await db.get(Agent, parent_id)
                    parent.deactivated_at = datetime.now(UTC)
                    await db.commit()
            return result

        monkeypatch.setattr(pipe, "call", revoke_parent_after_press)
        trace = {}
        denied = await execute("browser_action", operation("click", view, x=1, y=2), trace_metadata_sink=trace)
        assert phases == ["mousePressed"]
        assert "browser_delegation_revoked" in str(denied)
        assert trace["tool_execution_frame"]["status"] == "unknown"
        if remote:
            await asyncio.wait_for(asyncio.gather(*list(owner._rpc_jobs.values())), 5)
        await asyncio.wait_for(monitor, 5)
        current = await owner.read(source)
        assert current["status"] == "revoked"
        assert pipe.lifecycle.observes_closed()
        async with owner_sessionmaker() as db:
            resource = await db.get(EnvironmentBrowser, UUID(view["id"]))
            assert resource.last_effect["closure_confirmed"] is True
        stopped = await owner.execute(source, action="stop", request_id=uuid4(),
                                      expected_revision=current["revision"], expected_epoch=current["control_epoch"])
        assert stopped["browser"]["status"] == "revoked"
        assert (root / view["id"] / "effects").read_text().splitlines() == ["delegated-effect"]
    finally:
        reset_runtime_task_fence(fence)


@pytest.mark.parametrize("principal_kind", ["tenantless_admin", "foreign_home_admin", "foreign_member"])
async def test_selected_company_delegation_preserves_canonical_requester_identity(
    browser_case, browser_tools, owner_sessionmaker, app_user_sessionmaker, monkeypatch, principal_kind
):
    from fastapi import FastAPI, Request
    from httpx import ASGITransport, AsyncClient

    from app.api.browser_control import router
    from app.core.security import create_access_token, decode_access_token
    from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID
    from app.database import get_db, pin_rls_tenant_context, tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.browser_control import BrowserSource
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.session_v2_persistence import _validate_a2a_delegation_peer_binding
    from app.services import audit_logger

    seed, _, owner, client, root = browser_case
    local_owner, requester = seed["user_id"], uuid4()
    home_id = None if principal_kind == "tenantless_admin" else uuid4()
    role = "member" if principal_kind == "foreign_member" else "platform_admin"
    async with owner_sessionmaker() as db:
        if home_id is not None:
            db.add(Tenant(id=home_id, name="Requester Home", slug=f"home-{home_id.hex[:12]}"))
            await db.flush()
        db.add(User(id=requester, tenant_id=home_id, role=role, username=f"requester-{requester.hex[:12]}",
                    email=f"{requester.hex}@browser.test", display_name="Delegated Requester",
                    password_hash="synthetic-only", is_active=True))
        await db.commit()
    seed["user_id"] = requester
    frame, parent_id = await _delegated_work(seed, owner_sessionmaker, owner_user_id=local_owner)
    source = BrowserSource(*(seed[key] for key in ("tenant_id", "agent_id", "user_id", "session_id")))
    try:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            session = await db.get(ChatSession, seed["session_id"])
            agent = await db.get(Agent, seed["agent_id"])
            binding = _validate_a2a_delegation_peer_binding(
                db, session=session, agent=agent, peer_agent_id=parent_id, delegation_runtime_task_id=seed["run_id"],
            )
            if role == "member":
                with pytest.raises(PermissionError, match="a2a_delegation_peer_root_user_mismatch"):
                    await binding
            else:
                actor, task = await binding
                assert actor.id == requester and actor.tenant_id == seed["tenant_id"]
                assert task.root_user_id == requester
        path = f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/browser"
        scope = {"expected_user_id": str(requester), "expected_tenant_id": str(seed["tenant_id"])}
        app = FastAPI()
        app.include_router(router)

        async def selected_db(request: Request):
            claims = decode_access_token(request.headers["authorization"].removeprefix("Bearer "))
            selected = request.headers.get("x-tenant-id") if claims["role"] == "platform_admin" else None
            tenant = UUID(selected or claims.get("tid") or str(TENANT_SCOPE_QUARANTINE_ID))
            async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
                yield db

        app.dependency_overrides[get_db] = selected_db
        monkeypatch.setattr(audit_logger, "async_session", app_user_sessionmaker)
        headers = {"Authorization": "Bearer " + create_access_token(str(requester), role, str(home_id) if home_id else None)}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://selected-browser.test") as authenticated:
            if role == "platform_admin":
                missing = await authenticated.get(path, params=scope, headers=headers)
                assert missing.status_code == 400, missing.text
            read = await authenticated.get(path, params=scope, headers={**headers, "X-Tenant-Id": str(seed["tenant_id"])})
        if role == "member":
            assert read.status_code == 403, read.text
            async with owner_sessionmaker() as db:
                from sqlalchemy import select

                assert not list(await db.scalars(select(EnvironmentBrowser).where(
                    EnvironmentBrowser.session_id == seed["session_id"])))
            assert not list(root.glob("*/pid"))
            return
        assert read.status_code == 200, read.text
        fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
        try:
            async def execute(name, arguments=None):
                return await browser_tools(name, arguments, authority_frame=frame,
                                           permission_profile=frame.permission_profile, delegation_token=frame.delegation_token)

            response = await execute("browser_manage", operation("start"))
            assert str(response).startswith("{"), str(response)
            view = json.loads(str(response))["browser"]
            await execute("browser_observe")
            effect = await execute("browser_action", operation("type", view, text="selected-company-requester"))
            assert json.loads(str(effect))["ok"] is True, effect
            assert (root / view["id"] / "effects").read_text().splitlines() == ["selected-company-requester"]
            current = await owner.read(source)
            stopped = await owner.execute(source, action="stop", request_id=uuid4(),
                                          expected_revision=current["revision"], expected_epoch=current["control_epoch"])
            assert stopped["browser"]["status"] == "stopped"
        finally:
            reset_runtime_task_fence(fence)
    finally:
        async with owner_sessionmaker() as db:
            canonical = await db.get(User, requester)
            assert canonical.tenant_id == home_id
            assert canonical.role == role
