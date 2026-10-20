"""Retirement cannot archive original work while a new external send escapes."""

import asyncio
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from tests.integration import test_m3aj_employee_retirement_postgres as retirement

app_user_engine = retirement.app_user_engine
app_user_sessionmaker = retirement.app_user_sessionmaker
machine_api = retirement.machine_api
migrated_pg_url = retirement.migrated_pg_url
owner_engine = retirement.owner_engine
owner_sessionmaker = retirement.owner_sessionmaker
pg_container = retirement.pg_container
remote_work = retirement.remote_work
local_retirement_api = retirement.local_retirement_api


async def test_new_remote_work_cannot_escape_the_final_native_retirement_transaction(
    remote_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services import agent_manager, ai_assets

    seed, api, _session_id = remote_work
    source_id, session_id = uuid4(), uuid4()
    async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
        db.add(Agent(id=source_id, tenant_id=seed["tenant"], creator_id=seed["owner"],
            owner_user_id=seed["owner"], sponsor_user_id=seed["owner"], name="Native retiring source"))
        await db.flush()
        db.add(ChatSession(id=session_id, tenant_id=seed["tenant"], agent_id=source_id, user_id=seed["owner"]))
        await db.commit()
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(source_id)
    directory.mkdir()
    (directory / "original.txt").write_text("Retain until external work is settled")
    final_window, release = asyncio.Event(), asyncio.Event()
    project = ai_assets.register_projection

    async def hold_final_commit(*args, **kwargs):
        final_window.set()
        await release.wait()
        return await project(*args, **kwargs)

    monkeypatch.setattr(ai_assets, "register_projection", hold_final_commit)
    with retirement.withdrawal.remote_server() as remote:
        path = await retirement.remote_tasks.configure(seed, api, remote)
        remote.reply = lambda _request: {"task": {"id": "final-window-work", "status": {"state": "TASK_STATE_WORKING"}}}
        deleting = asyncio.create_task(api.delete(f"/agents/{source_id}"))
        admission = None
        try:
            await asyncio.wait_for(final_window.wait(), 10)
            admission = asyncio.create_task(api.post(path + "/tasks", json=retirement.remote_tasks.command(seed, session_id)))
            created = await asyncio.wait_for(admission, 10)
            if created.status_code == 201:
                await retirement.remote_tasks.execute(seed, created.json()["id"], app_user_sessionmaker)
            release.set()
            deleted = await asyncio.wait_for(deleting, 20)
            created = await asyncio.wait_for(admission, 20)
            if created.status_code == 201:
                task_id = created.json()["id"]
                async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                    task = await db.get(RuntimeTask, UUID(task_id))
                    if task.status == "pending":
                        await retirement.remote_tasks.execute(seed, task_id, app_user_sessionmaker)
                if [r for r in remote.requests if r[0] == "POST"]:
                    assert deleted.status_code == 409 and directory.is_dir(), deleted.text
                else:
                    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                        task = await db.get(RuntimeTask, UUID(task_id))
                        assert task.status == "needs_reconciliation"
                        assert task.metadata_json["remote_a2a"]["operations"][0]["phase"] == "not_started"
            else:
                assert created.status_code == 409, created.text
                assert created.json()["detail"] == {"code": "agent_remote_admission_in_progress", "retryable": True}
                assert not [r for r in remote.requests if r[0] == "POST"]
            if deleted.status_code == 204:
                assert not directory.exists()
                assert (await api.get(f"/agents/{source_id}")).status_code == 404
        finally:
            release.set()
            await asyncio.gather(deleting, *([admission] if admission else []), return_exceptions=True)


async def manager_headers(owner_sessionmaker, tenant_id):
    from app.core.security import create_access_token

    user_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=user_id, tenant_id=tenant_id, role="org_admin", is_active=True,
            username="tracking-manager-" + user_id.hex, display_name="Tracking manager",
            email=f"{user_id}@tracking.test", password_hash="synthetic"))
        await db.commit()
    return user_id, {"Authorization": "Bearer " + create_access_token(str(user_id), "org_admin", str(tenant_id))}


def tracking_command(manager_id, tenant_id, row):
    return {"request_id": str(uuid4()), "expected_user_id": str(manager_id), "expected_tenant_id": str(tenant_id),
        "expected_tracking_version": row["tracking_version"], "external_execution_remains_unknown": True}


@pytest.mark.parametrize("foreign_home", [False, True])
async def test_inactive_requester_retirement_records_the_actual_manager_without_transferring_private_work(
    remote_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, foreign_home,
):
    from app.models.tenant import Tenant
    from app.services import agent_manager
    from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError

    seed, api, session_id = remote_work
    manager_id, headers = await manager_headers(owner_sessionmaker, seed["tenant"])
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(seed["agent"])
    directory.mkdir()
    with retirement.withdrawal.remote_server() as remote:
        path = await retirement.remote_tasks.configure(seed, api, remote)
        remote.reply = lambda request: ({"id": "orphaned-request", "status": {"state": "TASK_STATE_CANCELED"}}
            if request["method"] == "CancelTask" else {"task": {"id": "orphaned-request", "status": {"state": "TASK_STATE_WORKING"}}})
        command = retirement.remote_tasks.command(seed, session_id)
        command["params"]["message"]["parts"] = [{"text": "PRIVATE_ORIGINAL_REQUEST"}]
        created = await api.post(path + "/tasks", json=command)
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        await retirement.remote_tasks.execute(seed, task_id, app_user_sessionmaker)
        rpc = RemoteA2AEndpoint.rpc

        async def lose_cancel(endpoint, method, *args, **kwargs):
            result = await rpc(endpoint, method, *args, **kwargs)
            if method == "CancelTask":
                raise RemoteA2AError("remote_a2a_response_lost", execution_status="unknown")
            return result

        monkeypatch.setattr(RemoteA2AEndpoint, "rpc", lose_cancel)
        assert (await api.delete(f"/agents/{seed['agent']}", headers=headers)).status_code == 409
        inventory = (await api.get("/agents/retirement-pending", headers=headers)).json()[0]
        assert inventory["inactive_requester_work_count"] == 0 and inventory["remote_work_count"] == 1
        action_path = f"/agents/{seed['agent']}/retirement-tracking"
        assert (await api.post(action_path, headers=headers,
            json=tracking_command(manager_id, seed["tenant"], inventory))).status_code == 409
        async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
            user = await db.get(User, seed["owner"])
            if foreign_home:
                home_id = uuid4()
                db.add(Tenant(id=home_id, name="Inactive requester home", slug="inactive-home-" + home_id.hex))
                await db.flush()
                user.tenant_id, user.role = home_id, "platform_admin"
            user.is_active = False
            await db.commit()
        fresh = await api.get("/agents/retirement-pending", headers=headers)
        assert fresh.status_code == 200, fresh.text
        row = fresh.json()[0]
        assert row["inactive_requester_work_count"] == 1 and directory.is_dir()
        assert "PRIVATE_ORIGINAL_REQUEST" not in fresh.text and task_id not in fresh.text
        assert (await api.get(path + "/tasks/" + task_id, headers=headers)).status_code == 404
        body = tracking_command(manager_id, seed["tenant"], row)
        async with owner_sessionmaker() as db:
            requester = await db.get(User, seed["owner"])
            requester.is_active = True
            await db.commit()
        reactivated = await api.post(action_path, headers=headers, json=body)
        assert reactivated.status_code == 409 and reactivated.json()["detail"]["code"] == "agent_retirement_tracking_changed"
        async with owner_sessionmaker() as db:
            requester = await db.get(User, seed["owner"])
            requester.is_active = False
            await db.commit()
        assert (await api.post(action_path, headers=headers, json={**body,
            "expected_tracking_version": inventory["tracking_version"]})).status_code == 409
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(task_id))
            before = deepcopy(task.metadata_json), task.status, task.claim_version
        ended = await api.post(action_path, headers=headers, json=body)
        assert ended.status_code == 200 and ended.json()["external_execution"] == "unknown", ended.text
        assert (await api.post(action_path, headers=headers, json=body)).json() == ended.json()
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(task_id))
            assert (task.metadata_json, task.status, task.claim_version) == before
            assert task.root_user_id == seed["owner"] and task.parent_session_id == str(session_id)
            audit = await db.scalar(select(AuditLog).where(AuditLog.id == UUID(ended.json()["receipt_id"])))
            assert audit.user_id == manager_id and audit.details["reason"] == "original_requester_inactive"
        settled = (await api.get("/agents/retirement-pending", headers=headers)).json()[0]
        assert settled["remote_work_count"] == settled["inactive_requester_work_count"] == 0
        deleted = await api.delete(f"/agents/{seed['agent']}", headers=headers)
        assert deleted.status_code == 204, deleted.text
        assert not directory.exists()
        assert [r[3]["method"] for r in remote.requests if r[0] == "POST"] == ["SendMessage", "CancelTask"]


async def test_local_inactive_requester_disposition_keeps_the_original_unknown_delivery(
    local_retirement_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.local_agent_channel import LocalAgentChannelMessage
    from app.services import agent_manager, local_agent_channel_service as channel

    seed, api = local_retirement_api
    manager_id, headers = await manager_headers(owner_sessionmaker, seed["tenant_id"])
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(seed["agent_id"])
    directory.mkdir()
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        actual = await channel.poll_pending_channel_messages(db, context=seed["selected"])
        assert [row["id"] for row in actual] == [seed["message_id"]]
    assert (await api.delete(f"/agents/{seed['agent_id']}", headers=headers)).status_code == 409
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed["user_id"])
        user.is_active = False
        await db.commit()
    row = (await api.get("/agents/retirement-pending", headers=headers)).json()[0]
    assert row["inactive_requester_work_count"] == row["local_work_count"] == 1
    result = await api.post(f"/agents/{seed['agent_id']}/retirement-tracking", headers=headers,
        json=tracking_command(manager_id, seed["tenant_id"], row))
    assert result.status_code == 200 and result.json()["external_execution"] == "unknown", result.text
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed["message_id"]))
        assert message.status == "needs_reconciliation" and message.delivery_attempt_count == 1
        assert message.sender_user_id == seed["user_id"]
    assert (await api.delete(f"/agents/{seed['agent_id']}", headers=headers)).status_code == 204
    assert not directory.exists()
