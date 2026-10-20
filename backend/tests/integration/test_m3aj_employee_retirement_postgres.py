"""Retirement withdraws external work before file archival and preserves recovery."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.runtime_task import RuntimeTask
from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError
from tests.integration import test_m3ai_remote_withdrawal_postgres as withdrawal
from tests.integration import test_m3o_remote_a2a_tasks_postgres as remote_tasks

app_user_engine = withdrawal.app_user_engine
app_user_sessionmaker = withdrawal.app_user_sessionmaker
machine_api = withdrawal.machine_api
migrated_pg_url = withdrawal.migrated_pg_url
owner_engine = withdrawal.owner_engine
owner_sessionmaker = withdrawal.owner_sessionmaker
pg_container = withdrawal.pg_container
remote_work = withdrawal.remote_work
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("flow", ["queued", "working", "lost_cancel"])
async def test_delete_withdraws_the_actual_remote_work_and_keeps_unknown_recoverable(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, flow,
):
    from app.api import chat_sessions
    from app.services import agent_manager

    seed, api, session_id = remote_work
    api._transport.app.include_router(chat_sessions.router)
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(seed["agent"])
    directory.mkdir()
    (directory / "retained.txt").write_text("Original employee files")
    with withdrawal.remote_server() as remote:
        path = await remote_tasks.configure(seed, api, remote)
        remote.reply = lambda request: ({"id": "retiring-task", "status": {"state": "TASK_STATE_CANCELED"}}
            if request["method"] == "CancelTask" else {"task": {"id": "retiring-task", "status": {"state": "TASK_STATE_WORKING"}}})
        created = await api.post(path + "/tasks", json=remote_tasks.command(seed, session_id))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        if flow != "queued":
            await remote_tasks.execute(seed, task_id, app_user_sessionmaker)
        rpc = RemoteA2AEndpoint.rpc

        async def lose_cancel(endpoint, method, *args, **kwargs):
            result = await rpc(endpoint, method, *args, **kwargs)
            if method == "CancelTask":
                raise RemoteA2AError("remote_a2a_response_lost", execution_status="unknown")
            return result

        if flow == "lost_cancel":
            monkeypatch.setattr(RemoteA2AEndpoint, "rpc", lose_cancel)
        result = await api.delete(f"/agents/{seed['agent']}")
        assert result.status_code == (409 if flow == "lost_cancel" else 204), result.text
        methods = [item[3]["method"] for item in remote.requests if item[0] == "POST"]
        assert methods == ([] if flow == "queued" else ["SendMessage", "CancelTask"])
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            agent = await db.get(Agent, seed["agent"])
            task = await db.get(RuntimeTask, UUID(task_id))
            assert agent.deactivated_at is not None
            assert (agent.deleted_at is None) == (flow == "lost_cancel")
            assert task.status == ("needs_reconciliation" if flow == "lost_cancel" else "killed")
            assert task.root_user_id == seed["owner"] and task.parent_session_id == str(session_id)
        if flow == "lost_cancel":
            assert result.json()["detail"]["code"] == "agent_retirement_pending" and directory.is_dir()
            from fastapi import HTTPException
            from app.services.agent_identity_lifecycle import soft_delete_agent

            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                retained = await db.get(Agent, seed["agent"])
                with pytest.raises(HTTPException) as refused:
                    await soft_delete_agent(db, retained, actor_id=seed["owner"])
                assert refused.value.detail["code"] == "agent_retirement_pending" and retained.deleted_at is None
                await db.rollback()

            profile = await api.get(f"/agents/{seed['agent']}")
            assert profile.status_code == 200 and profile.json()["access_level"] == "read", profile.text
            assert not profile.json()["action_capabilities"]["can_use"]
            bench = await api.get(f"/agents/{seed['agent']}/sessions/{session_id}/workbench")
            assert bench.status_code == 200, bench.text
            transcript = await api.get(f"/agents/{seed['agent']}/sessions/{session_id}/transcript")
            assert transcript.status_code == 200, transcript.text
            assert (await api.post(path + "/tasks", json=remote_tasks.command(seed, session_id))).status_code == 410
            assert (await api.post(f"/agents/{seed['agent']}/sessions/{session_id}/approvals/{uuid4()}/resolve",
                json={"action": "approve"})).status_code == 410

            replay = await api.delete(f"/agents/{seed['agent']}")
            assert replay.status_code == 409 and len([r for r in remote.requests if r[0] == "POST"]) == len(methods)
            view = await api.get(path + "/tasks/" + task_id)
            assert view.status_code == 200 and "abandon" in view.json()["recovery_actions"], view.text
            ended = await api.post(path + f"/tasks/{task_id}/operations", json={
                **withdrawal.request_frame(seed, 1), "operation_id": str(uuid4()), "action": "abandon", "params": {}})
            assert ended.status_code == 200 and ended.json()["local_abandoned"], ended.text
            assert (await api.delete(f"/agents/{seed['agent']}")).status_code == 204
        assert not directory.exists()
        assert (await api.get(f"/agents/{seed['agent']}")).status_code == 404
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            audits = list(await db.scalars(select(AuditLog).where(AuditLog.agent_id == seed["agent"], AuditLog.action == "agent:retirement_requested")))
            assert len(audits) == 1 and audits[0].user_id == seed["owner"]


async def test_delete_during_the_http_gap_keeps_the_real_worker_and_original_late_reply(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services import agent_manager

    seed, api, session_id = remote_work
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(seed["agent"])
    directory.mkdir()
    received, release = asyncio.Event(), asyncio.Event()
    rpc = RemoteA2AEndpoint.rpc

    async def hold(endpoint, method, *args, **kwargs):
        result = await rpc(endpoint, method, *args, **kwargs)
        if method == "SendMessage":
            received.set()
            await release.wait()
        return result

    monkeypatch.setattr(RemoteA2AEndpoint, "rpc", hold)
    with withdrawal.remote_server() as remote:
        path = await remote_tasks.configure(seed, api, remote)
        remote.reply = lambda request: ({"id": "late-retirement", "status": {"state": "TASK_STATE_CANCELED"}}
            if request["method"] == "CancelTask" else {"task": {"id": "late-retirement", "status": {"state": "TASK_STATE_WORKING"}}})
        created = await api.post(path + "/tasks", json=remote_tasks.command(seed, session_id))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        worker = asyncio.create_task(remote_tasks.execute(seed, task_id, app_user_sessionmaker))
        try:
            await asyncio.wait_for(received.wait(), 10)
            result = await asyncio.wait_for(api.delete(f"/agents/{seed['agent']}"), 10)
            assert result.status_code == 409 and result.json()["detail"]["code"] == "agent_retirement_pending", result.text
            assert directory.exists()
            release.set()
            await asyncio.wait_for(worker, 20)
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, UUID(task_id))
                operations = task.metadata_json["remote_a2a"]["operations"]
                assert task.status == "killed" and operations[0]["observation"]["task"]["id"] == "late-retirement"
                assert [o["method"] for o in operations] == ["SendMessage", "CancelTask"]
            assert (await api.delete(f"/agents/{seed['agent']}")).status_code == 204
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.fixture
async def local_retirement_api(app_user_sessionmaker, owner_sessionmaker, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app import database
    from app.api import agents, local_agent_channel
    from app.core.security import create_access_token
    from app.database import get_db
    from app.models.user import User
    from tests.integration.test_m3a_local_employee_control_postgres import local_work as local_fixture
    import sys

    # Reuse the established real capability/leased-message fixture without
    # replacing identity, RLS, queue, dispatch or retirement behavior.
    seed = await local_fixture.__wrapped__(owner_sessionmaker)
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed["user_id"])
        user.role = "org_admin"
        await db.commit()
        token = create_access_token(str(user.id), user.role, str(user.tenant_id))
    app = FastAPI()
    app.include_router(agents.router)
    app.include_router(local_agent_channel.router)

    async def scoped_db():
        async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
            yield db

    app.dependency_overrides[get_db] = scoped_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local",
        headers={"Authorization": "Bearer " + token}) as client:
        yield seed, client


@pytest.mark.parametrize("delivered", [False, True])
async def test_local_retirement_preserves_unknown_host_execution_and_original_request_disposition(
    local_retirement_api, app_user_sessionmaker, monkeypatch, tmp_path, delivered,
):
    from app.models.local_agent_channel import LocalAgentChannelMessage
    from app.models.local_bridge import LocalAgentBridgeConnection
    from app.services import agent_manager, local_agent_channel_service as channel

    seed, api = local_retirement_api
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(seed["agent_id"])
    directory.mkdir()
    if delivered:
        async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
            actual = await channel.poll_pending_channel_messages(db, context=seed["selected"])
            assert [row["id"] for row in actual] == [seed["message_id"]]
    result = await api.delete(f"/agents/{seed['agent_id']}")
    assert result.status_code == (409 if delivered else 204), result.text
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed["message_id"]))
        bridge = await db.get(LocalAgentBridgeConnection, seed["selected"].connection_id)
        other = await db.get(LocalAgentBridgeConnection, seed["other"].connection_id)
        assert message.status == ("needs_reconciliation" if delivered else "cancelled")
        assert bridge.status == "revoked" and other.status == "active"
        assert message.delivery_attempt_count == int(delivered)
        request_hash = message.request_hash
    if delivered:
        assert directory.exists() and result.json()["detail"]["external_execution"] == "unknown"
        pending = await api.get("/agents/retirement-pending")
        assert pending.status_code == 200 and pending.json()[0]["local_work_count"] == 1, pending.text
        existing = await api.get(f"/agents/{seed['agent_id']}/local-agent/sessions/{seed['work_id']}")
        assert existing.status_code == 200, existing.text
        requests = await api.get(f"/local-agents/sessions/{seed['work_id']}/requests")
        assert requests.status_code == 200 and requests.json()["requests"][0]["can_abandon"], requests.text
        assert (await api.post(f"/agents/{seed['agent_id']}/local-agent/sessions/{seed['work_id']}/messages",
            json={"content": "Must not dispatch new retirement work"})).status_code == 410
        ended = await api.post(f"/local-agents/sessions/{seed['work_id']}/messages/{seed['message_id']}/abandon", json={
            "expected_user_id": str(seed["user_id"]), "expected_tenant_id": str(seed["tenant_id"]),
            "request_hash": request_hash, "idempotency_key": "end-retiring-local-tracking"})
        assert ended.status_code == 200 and ended.json()["execution_state"] == "unknown", ended.text
        assert (await api.delete(f"/agents/{seed['agent_id']}")).status_code == 204
    assert not directory.exists()


@pytest.mark.parametrize("manager_kind", ["org_admin", "platform_admin"])
@pytest.mark.parametrize("lost_reply", [False, True])
async def test_source_retirement_cancels_only_its_delegated_remote_work_without_revoking_the_recipient(
    remote_work, app_user_sessionmaker, owner_sessionmaker, monkeypatch, tmp_path, lost_reply, manager_kind,
):
    from app.core.security import create_access_token
    from app.models.user import User
    from app.models.tenant import Tenant
    from app.api import chat_sessions
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.models.remote_a2a import RemoteA2AConnection
    from app.services import agent_manager

    seed, api, session_id = remote_work
    api._transport.app.include_router(chat_sessions.router)
    target_id = uuid4()
    async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
        db.add(Agent(id=target_id, tenant_id=seed["tenant"], creator_id=seed["owner"], owner_user_id=seed["owner"],
            sponsor_user_id=seed["owner"], name="Independent remote recipient", agent_type="remote_a2a", status="running"))
        await db.commit()
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(seed["agent"])
    directory.mkdir()
    manager_id = uuid4()
    home_id = uuid4() if manager_kind == "platform_admin" else seed["tenant"]
    async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
        if home_id != seed["tenant"]:
            db.add(Tenant(id=home_id, name="Management home", slug="retirement-home-" + home_id.hex))
            await db.flush()
        db.add(User(id=manager_id, tenant_id=home_id, role=manager_kind, is_active=True,
            username="retirement-manager-" + manager_id.hex, email=f"{manager_id}@retirement.test",
            display_name="Retirement manager", password_hash="synthetic"))
        await db.commit()
    manager_headers = {"Authorization": "Bearer " + create_access_token(str(manager_id), manager_kind, str(home_id)),
        "X-Tenant-Id": str(seed["tenant"])}
    with withdrawal.remote_server() as remote:
        path = await remote_tasks.configure({**seed, "agent": target_id}, api, remote)
        remote.reply = lambda request: ({"id": "source-retirement-task", "status": {"state": "TASK_STATE_CANCELED"}}
            if request["method"] == "CancelTask" else {"task": {"id": "source-retirement-task", "status": {"state": "TASK_STATE_WORKING"}}})
        created = await api.post(path + "/tasks", json=remote_tasks.command(seed, session_id))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        await remote_tasks.execute(seed, task_id, app_user_sessionmaker)
        rpc = RemoteA2AEndpoint.rpc

        async def lose(endpoint, method, *args, **kwargs):
            result = await rpc(endpoint, method, *args, **kwargs)
            if method == "CancelTask":
                raise RemoteA2AError("remote_a2a_response_lost", execution_status="unknown")
            return result

        if lost_reply:
            monkeypatch.setattr(RemoteA2AEndpoint, "rpc", lose)
        response = await api.delete(f"/agents/{seed['agent']}", headers=manager_headers)
        assert response.status_code == (409 if lost_reply else 204), response.text
        assert [r[3]["method"] for r in remote.requests if r[0] == "POST"] == ["SendMessage", "CancelTask"]
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            recipient = await db.get(Agent, target_id)
            connection = await db.get(RemoteA2AConnection, target_id)
            task = await db.get(RuntimeTask, UUID(task_id))
            assert recipient.deleted_at is recipient.deactivated_at is None
            assert connection.state == "discovered" and connection.version == 1 and connection.credential_configured
            assert task.parent_agent_id == seed["agent"] and task.child_agent_id == target_id
            assert task.root_user_id == seed["owner"]
            audit = await db.scalar(select(AuditLog).where(AuditLog.agent_id == seed["agent"],
                AuditLog.action == "agent:retirement_work_withdrawal"))
            assert audit.user_id == manager_id and task.metadata_json["remote_a2a"]["operations"][-1]["actor_user_id"] == str(manager_id)
            assert task.status == ("needs_reconciliation" if lost_reply else "killed")
        assert (await api.get(path + "/tasks/" + task_id, headers=manager_headers)).status_code == 404
        if lost_reply:
            delivered = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id="retiring-source-return")
            assert delivered["delivered"] >= 1, delivered
            transcript = await api.get(f"/agents/{seed['agent']}/sessions/{session_id}/transcript")
            assert transcript.status_code == 200, transcript.text
            original_work = await api.get(f"/agents/{seed['agent']}/remote-a2a/tasks", params={
                "source_session_id": str(session_id), "source_work": "true"})
            assert original_work.status_code == 200, original_work.text
            assert [(item["id"], item["agent_id"]) for item in original_work.json()["items"]] == [(task_id, str(target_id))]
            receipt = await api.get(path + "/tasks/" + original_work.json()["items"][0]["id"])
            assert receipt.status_code == 200 and receipt.json()["remote_task_id"] == "source-retirement-task"
            assert "abandon" in receipt.json()["recovery_actions"]
            assert directory.exists()
            assert (await api.delete(f"/agents/{seed['agent']}")).status_code == 409
            assert len([r for r in remote.requests if r[0] == "POST"]) == 2
            ended = await api.post(path + f"/tasks/{task_id}/operations", json={**withdrawal.request_frame(seed, 1),
                "operation_id": str(uuid4()), "action": "abandon", "params": {}})
            assert ended.status_code == 200, ended.text
            assert (await api.delete(f"/agents/{seed['agent']}")).status_code == 204


@pytest.mark.parametrize("reconfigured", [False, True])
async def test_source_retirement_http_gap_retains_identity_and_cannot_cancel_a_new_recipient_configuration(
    remote_work, app_user_sessionmaker, owner_sessionmaker, monkeypatch, tmp_path, reconfigured,
):
    from tests.integration.test_m3n_remote_a2a_connections_postgres import configuration, request_frame
    from app.services import agent_manager

    seed, api, session_id = remote_work
    target_id = uuid4()
    async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
        db.add(Agent(id=target_id, tenant_id=seed["tenant"], creator_id=seed["owner"], owner_user_id=seed["owner"],
            sponsor_user_id=seed["owner"], name="Recipient with independent configuration", agent_type="remote_a2a", status="running"))
        await db.commit()
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(seed["agent"])
    directory.mkdir()
    received, release = asyncio.Event(), asyncio.Event()
    rpc = RemoteA2AEndpoint.rpc

    async def hold(endpoint, method, *args, **kwargs):
        result = await rpc(endpoint, method, *args, **kwargs)
        if method == "SendMessage":
            received.set()
            await release.wait()
        return result

    monkeypatch.setattr(RemoteA2AEndpoint, "rpc", hold)
    with withdrawal.remote_server() as remote:
        target_seed = {**seed, "agent": target_id}
        path = await remote_tasks.configure(target_seed, api, remote)
        remote.reply = lambda request: ({"id": "late-source-identity", "status": {"state": "TASK_STATE_CANCELED"}}
            if request["method"] == "CancelTask" else {"task": {"id": "late-source-identity", "status": {"state": "TASK_STATE_WORKING"}}})
        queued = await api.post(path + "/tasks", json=remote_tasks.command(seed, session_id))
        assert queued.status_code == 201, queued.text
        task_id = queued.json()["id"]
        worker = asyncio.create_task(remote_tasks.execute(seed, task_id, app_user_sessionmaker))
        try:
            await asyncio.wait_for(received.wait(), 10)
            response = await asyncio.wait_for(api.delete(f"/agents/{seed['agent']}"), 10)
            assert response.status_code == 409 and directory.exists(), response.text
            if reconfigured:
                changed = await api.put(path, json={**configuration(target_seed, remote), "expected_version": 1,
                    "bearer_token": "new-recipient-synthetic-credential"})
                assert changed.status_code == 200 and changed.json()["version"] == 2, changed.text
                assert (await api.post(path + "/discover", json=request_frame(target_seed, 2))).status_code == 200
            release.set()
            await asyncio.wait_for(worker, 20)
            view = await api.get(path + "/tasks/" + task_id)
            assert view.status_code == 200 and view.json()["status"] == ("needs_reconciliation" if reconfigured else "killed"), view.text
            assert [r[3]["method"] for r in remote.requests if r[0] == "POST"] == (["SendMessage"] if reconfigured else ["SendMessage", "CancelTask"])
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, UUID(task_id))
                state = task.metadata_json["remote_a2a"]
                assert state["operations"][0]["observation"]["task"]["id"] == "late-source-identity"
                assert state["connection_withdrawal"]["status"] == ("unavailable" if reconfigured else "cancel_reported")
            if reconfigured:
                ended = await api.post(path + f"/tasks/{task_id}/operations", json={**request_frame(seed, 1),
                    "operation_id": str(uuid4()), "action": "abandon", "params": {}})
                assert ended.status_code == 200, ended.text
            assert (await api.delete(f"/agents/{seed['agent']}")).status_code == 204
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)


async def test_retiring_shared_history_keeps_current_membership_without_restoring_execution(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.core.permissions import check_agent_access
    from app.models.channel_config import ChannelConfig
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService
    from app.services.shared_group_history import authorize_group_history_reader
    from fastapi import HTTPException
    from datetime import datetime, timezone
    from tests.services.test_channel_delivery_outbox import _seed_delivery

    seed = await _seed_delivery(owner_sessionmaker, tmp_path, external=True, channel="feishu")
    members = {"retirement-reader"}

    async def current_members(_self, *_args, **_kwargs):
        return set(members)

    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", current_members, raising=False)
    async with tenant_scoped_session(seed["tenant_id"], session_factory=owner_sessionmaker) as db:
        user = await db.get(User, seed["user_id"])
        user.role = "member"
        config = await db.get(ChannelConfig, seed["config_id"])
        await bind_authenticated_self_channel_principal(db, tenant_id=seed["tenant_id"], config=config,
            provider_subject_id="retirement-reader", user_id=user.id, actor_user_id=user.id)
        group = await db.get(ChatSession, seed["session_id"])
        group.user_id = group.external_principal_id = None
        group.audience_kind = group.visibility_scope = "group"
        group.group_installation_ref, group.group_subject_id = str(config.id), "oc_retirement"
        agent = await db.get(Agent, seed["agent_id"])
        agent.deactivated_at = datetime.now(timezone.utc)
        agent.deactivation_reason = "retirement_pending"
        await db.commit()
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed["user_id"])
        group = await db.get(ChatSession, seed["session_id"])
        assert await authorize_group_history_reader(db, session=group, user=user) == "shared_group_member"
        with pytest.raises(HTTPException) as blocked:
            await check_agent_access(db, user, seed["agent_id"])
        assert blocked.value.status_code == 410
        members.clear()
        with pytest.raises(HTTPException) as revoked:
            await authorize_group_history_reader(db, session=group, user=user)
        assert revoked.value.status_code == 403
