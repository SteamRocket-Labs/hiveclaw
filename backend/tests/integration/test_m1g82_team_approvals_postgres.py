"""Team approvers use their own live effect grant, never the owner's authority."""

import uuid
import os
import json
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
from app.models.security_audit import ResourcePermission
from app.models.user import User
from tests.services.test_approval_ticket import _seed_approved_ticket


@pytest.mark.parametrize("revoke", [None, "membership", "team", "grant", "deny", "user", "owner", "home_team", "handover", "handover_back"])
async def test_team_lead_decides_in_team_and_ticket_rechecks_before_effect(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, revoke,
):
    from app.api.enterprise import router
    from app.services.approval_ticket import build_approval_execution_envelope, hash_approval_execution_envelope
    from app.services.approval_ticket import build_live_approval_policy_snapshot, hash_policy_snapshot
    from app.config import get_settings
    from app.models.capability_policy import CapabilityPolicy
    from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1
    from app.tools.runtime import ToolExecutionContext

    tenant_id, owner_id, agent_id, approval_id, arguments = await _seed_approved_ticket(owner_sessionmaker)
    lead_id, team_id, membership_id, grant_id = [uuid.uuid4() for _ in range(4)]
    async with owner_sessionmaker() as db:
        db.add(User(id=lead_id, tenant_id=tenant_id, username=f"lead-{lead_id.hex}",
            email=f"{lead_id.hex}@lead.test", display_name="Team Lead", password_hash="synthetic", role="member"))
        db.add(BusinessTeam(id=team_id, tenant_id=tenant_id, name="Delivery", status="active", created_by_user_id=owner_id))
        await db.flush()
        db.add(BusinessTeamMembership(id=membership_id, tenant_id=tenant_id, business_team_id=team_id,
            user_id=lead_id, role="lead", status="active"))
        db.add(CapabilityPolicy(tenant_id=tenant_id, agent_id=agent_id, capability="workspace.file.write",
            allowed=True, requires_approval=True, conditions={}))
        await db.flush()
        (await db.get(Agent, agent_id)).home_business_team_id = team_id
        approval = await db.get(ApprovalRequest, approval_id)
        approval.status, approval.execution_status, approval.resolved_by = "pending", "pending", None
        approval.action_type = "workspace.file.write"
        workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
        approval.execution_envelope = build_approval_execution_envelope(context=ToolExecutionContext(
            agent_id=agent_id, user_id=owner_id, tenant_id=str(tenant_id), workspace=workspace,
            permission_profile=PermissionProfileV1(mode=PermissionMode.ACCEPT_EDITS, allowed_tools=("write_file",))),
            tool_call_id=f"team-approved-{approval_id}", emit_runtime_hooks=False)
        approval.execution_envelope_hash = hash_approval_execution_envelope(approval.execution_envelope)
        approval.policy_snapshot = await build_live_approval_policy_snapshot(db=db, tenant_id=tenant_id,
            agent_id=agent_id, tool_name="write_file")
        approval.policy_snapshot_hash = hash_policy_snapshot(approval.policy_snapshot)
        approval.details = {**approval.details, "secret_field": "PRIVATE_APPROVAL_CANARY"}
        await db.commit()
        lead = await db.get(User, lead_id)
        owner = await db.get(User, owner_id)

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            yield db

    notifications = []
    async def no_notify(*args, **kwargs):
        notifications.append(kwargs)

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_notify)
    monkeypatch.setattr("app.services.notification_service.send_notification", no_notify)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    actor = {"user": owner}
    app.dependency_overrides[get_current_user] = lambda: actor["user"]
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncExitStack() as browser, AsyncClient(transport=ASGITransport(app=app), base_url="http://team-approval") as client:
        owner_list = (await client.get(f"/api/enterprise/approvals?business_team_id={team_id}")).json()
        assert [row["user_id"] for row in owner_list[0]["team_reviewers"]] == [str(lead_id)]
        grant_body = {"request_id": str(grant_id), "user_id": str(lead_id)}
        granted = await client.post(f"/api/enterprise/approvals/{approval_id}/reviewers", json=grant_body)
        assert granted.status_code == 201 and granted.json()["id"] == str(grant_id), granted.text
        assert (await client.post(f"/api/enterprise/approvals/{approval_id}/reviewers", json=grant_body)).json()["id"] == str(grant_id)
        assert (await client.post(f"/api/enterprise/approvals/{approval_id}/reviewers",
            json={**grant_body, "action_type": "manage"})).status_code == 422
        assert (await client.post(f"/api/enterprise/approvals/{approval_id}/reviewers",
            json={**grant_body, "user_id": str(owner_id)})).status_code == 403
        actor["user"] = lead
        assert (await client.post(f"/api/enterprise/approvals/{approval_id}/reviewers", json=grant_body)).status_code == 403
        assert (await client.delete(f"/api/enterprise/approvals/{approval_id}/reviewers/{grant_id}")).status_code == 403
        if revoke is None:
            for model, row_id, field, value in [(ResourcePermission, grant_id, "actions", ["read"]),
                (BusinessTeamMembership, membership_id, "role", "member"), (BusinessTeam, team_id, "status", "suspended"),
                (ApprovalRequest, approval_id, "details", {"tool": "write_file", "origin": {"type": "agent_session"}})]:
                async with owner_sessionmaker() as db:
                    row = await db.get(model, row_id)
                    previous = getattr(row, field)
                    setattr(row, field, value)
                    await db.commit()
                actor["user"] = owner
                refused = await client.post(f"/api/enterprise/approvals/{approval_id}/reviewers", json=grant_body)
                if model in {BusinessTeamMembership, BusinessTeam, ApprovalRequest}:
                    assert refused.status_code == (404 if model is ApprovalRequest else 403), refused.text
                actor["user"] = lead
                assert (await client.get(f"/api/enterprise/approvals?business_team_id={team_id}")).json() == []
                denied = await client.post(f"/api/enterprise/approvals/{approval_id}/resolve", json={"action": "approve"})
                assert denied.status_code == 400, denied.text
                async with owner_sessionmaker() as db:
                    assert (await db.get(ApprovalRequest, approval_id)).status == "pending"
                    setattr(await db.get(model, row_id), field, previous)
                    await db.commit()
            async with app_user_sessionmaker() as db:
                from app.services.approval_service import approval_service
                from app.core.policy import check_permission
                from app.core.permissions import check_agent_access
                from fastapi import HTTPException

                await pin_rls_tenant_context(db, tenant_id)
                with pytest.raises(HTTPException) as denied_agent:
                    await check_agent_access(db, lead, agent_id)
                assert denied_agent.value.status_code == 403
                for context in [{}, {"approval_id": str(uuid.uuid4())}]:
                    assert not await check_permission(db, principal_type="user", principal_id=lead_id,
                        resource_type="agent", resource_id=agent_id, action="workspace.file.write", context=context)
                await approval_service._notify_pending_approval(db, await db.get(Agent, agent_id), await db.get(ApprovalRequest, approval_id))
            assert {row["user_id"] for row in notifications} == {owner_id, lead_id}
            lead_notice = next(row for row in notifications if row["user_id"] == lead_id)
            assert lead_notice["link"] == "/team" and "PRIVATE_APPROVAL_CANARY" not in lead_notice["body"]
        listed = await client.get(f"/api/enterprise/approvals?business_team_id={team_id}")
        assert listed.status_code == 200, listed.text
        assert [row["id"] for row in listed.json()] == [str(approval_id)], listed.text
        assert "PRIVATE_APPROVAL_CANARY" not in listed.text and '"content": "approved"' not in listed.text
        assert (await client.get(f"/api/enterprise/approvals?business_team_id={uuid.uuid4()}")).json() == []
        if revoke is None and os.environ.get("HIVE_TEST_G82_FRONTEND_DIST"):
            actor["user"] = owner
            removed = await client.delete(f"/api/enterprise/approvals/{approval_id}/reviewers/{grant_id}")
            assert removed.status_code == 200 and removed.json()["revoked"] is True
            replayed = await client.post(f"/api/enterprise/approvals/{approval_id}/reviewers", json=grant_body)
            assert replayed.status_code == 201 and replayed.json()["revoked"] is True
            process = await browser.enter_async_context(_team_approval_browser(app, lead, team_id, agent_id, approval_id, tmp_path, owner, actor))
        else:
            resolved = await client.post(f"/api/enterprise/approvals/{approval_id}/resolve", json={"action": "approve"})
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["execution_status"] == "queued"
            assert "PRIVATE_APPROVAL_CANARY" not in resolved.text and '"content": "approved"' not in resolved.text

        await _execute_and_recheck(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
            revoke, tenant_id, owner_id, agent_id, approval_id, lead_id, membership_id, team_id, grant_id, arguments, workspace)
        if revoke is None and os.environ.get("HIVE_TEST_G82_FRONTEND_DIST"):
            process.stdin.write(b'{"event":"work_completed"}\n')
            await process.stdin.drain()
            output, error = await asyncio.wait_for(process.communicate(), 60)
            assert process.returncode == 0, (output, error)


async def _execute_and_recheck(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
    revoke, tenant_id, owner_id, agent_id, approval_id, lead_id, membership_id, team_id, grant_id, arguments, workspace):
    from app.services.approval_ticket import ApprovalTicketError, consume_approval_ticket
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        assert approval.requested_by == owner_id and approval.resolved_by == lead_id
        assert approval.normalized_arguments == arguments and approval.consumed_at is None
        if revoke == "membership":
            (await db.get(BusinessTeamMembership, membership_id)).status = "left"
        elif revoke == "team":
            (await db.get(BusinessTeam, team_id)).status = "suspended"
        elif revoke == "grant":
            await db.delete(await db.get(ResourcePermission, grant_id))
        elif revoke == "deny":
            db.add(ResourcePermission(tenant_id=tenant_id, principal_type="business_team", principal_id=team_id,
                resource_type="agent", resource_id=agent_id, actions=["workspace.file.write"], effect="deny"))
        elif revoke in {"user", "owner"}:
            (await db.get(User, lead_id if revoke == "user" else owner_id)).is_active = False
        elif revoke == "home_team":
            (await db.get(Agent, agent_id)).home_business_team_id = None
        elif revoke in {"handover", "handover_back"}:
            from app.services.agent_ownership_service import transfer_agent_owner

            successor = User(id=uuid.uuid4(), tenant_id=tenant_id, username=f"successor-{uuid.uuid4().hex}",
                email=f"{uuid.uuid4().hex}@lead.test", password_hash="synthetic", display_name="Successor")
            db.add(successor)
            await db.flush()
            owner = await db.get(User, owner_id)
            await transfer_agent_owner(db, agent_id=agent_id, new_owner_id=successor.id, actor=owner,
                expected_owner_id=owner_id, reason="Transfer the employee without carrying an old review grant")
            if revoke == "handover_back":
                await transfer_agent_owner(db, agent_id=agent_id, new_owner_id=owner_id, actor=successor,
                    expected_owner_id=successor.id, reason="A return handover must not revive an old review grant")
        await db.commit()
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    if revoke:
        with pytest.raises(ApprovalTicketError, match="approver is no longer responsible"):
            await consume_approval_ticket(approval_id=approval_id, expected_agent_id=agent_id, expected_user_id=lead_id)
        async with owner_sessionmaker() as db:
            assert (await db.get(ApprovalRequest, approval_id)).consumed_at is None
    else:
        from app.services import agent_tools
        from app.services.approval_execution_runtime import execute_claimed_approval_execution
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import run_claimed_runtime_task

        monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            claims = await RuntimeTaskClaimService(db=db, worker_id="team-approval", task_types=("approval_execution",)).claim_available()
            assert len(claims) == 1
        claim = claims[0]
        outcome = await run_claimed_runtime_task(execute_claimed_approval_execution(claim.id), task_id=claim.id,
            claim_version=claim.claim_version, worker_id="team-approval", lease_seconds=180)
        assert outcome == "succeeded"
        assert (workspace / "workspace/notes.md").read_text() == "approved"
        async with owner_sessionmaker() as db:
            approval = await db.get(ApprovalRequest, approval_id)
            assert approval.execution_status == "succeeded" and approval.requested_by == owner_id
            assert approval.execution_receipt["approved_by_user_id"] == str(lead_id)
            assert approval.execution_receipt["requested_by_user_id"] == str(owner_id)
        assert await execute_claimed_approval_execution(claim.id) == "succeeded"


@asynccontextmanager
async def _team_approval_browser(app, lead, team_id, agent_id, approval_id, tmp_path, owner, actor):
    import socket
    import uvicorn
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from app.api import auth, business_teams, agents
    from app.core.security import create_access_token

    frontend = Path(__file__).resolve().parents[3] / "frontend"
    dist = Path(os.environ["HIVE_TEST_G82_FRONTEND_DIST"])
    assert (dist / "index.html").is_file()
    for router in (auth.router, business_teams.router, agents.router):
        app.include_router(router, prefix="/api")
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
    @app.get("/api/{path:path}")
    async def unavailable(path: str):
        return JSONResponse(status_code=404, content={"detail": "Unsupported fixture endpoint"})
    @app.get("/{path:path}")
    async def page(path: str):
        return FileResponse(dist / "index.html")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    process = None
    try:
        for _ in range(500):
            if server.started:
                break
            assert not serving.done()
            await asyncio.sleep(0.01)
        assert server.started
        fixture = {"origin": f"http://127.0.0.1:{port}", "tenantId": str(lead.tenant_id), "teamId": str(team_id),
            "agentId": str(agent_id),
            "approvalId": str(approval_id), "token": create_access_token(str(lead.id), "user", str(lead.tenant_id)),
            "ownerToken": create_access_token(str(owner.id), "user", str(owner.tenant_id)), "leadId": str(lead.id),
            "receiptPath": str(tmp_path / "team-browser-receipt.json"), "screenshotPath": str(tmp_path / "team-approved.png"),
            "errorScreenshot": str(tmp_path / "team-error.png")}
        (tmp_path / "browser-fixture.json").write_text(json.dumps(fixture))
        process = await asyncio.create_subprocess_exec("node", str(frontend / "e2e/team-approval-native.mjs"),
            str(tmp_path / "browser-fixture.json"), cwd=frontend, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ready = await asyncio.wait_for(process.stdout.readline(), 60)
        if not ready:
            output, error = await process.communicate()
            pytest.fail(f"Team browser failed: {output!r} {error!r}")
        assert json.loads(ready)["event"] == "reviewer_granted", ready
        actor["user"] = lead
        process.stdin.write(b'{"event":"reviewer_ready"}\n')
        await process.stdin.drain()
        ready = await asyncio.wait_for(process.stdout.readline(), 60)
        assert ready and json.loads(ready)["event"] == "decision_recorded", ready
        yield process
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
