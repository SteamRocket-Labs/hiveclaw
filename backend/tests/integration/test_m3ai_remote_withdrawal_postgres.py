"""Connection withdrawal preserves exact remote execution and original-work recovery."""

import json
import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.audit import AuditLog
from app.models.remote_a2a import RemoteA2AConnection
from app.models.runtime_task import RuntimeTask
from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError
from tests.integration import test_m3o_remote_a2a_tasks_postgres as original
from tests.integration.test_m3n_remote_a2a_connections_postgres import request_frame
from tests.services.test_remote_a2a_transport import remote_server

app_user_engine = original.app_user_engine
app_user_sessionmaker = original.app_user_sessionmaker
machine_api = original.machine_api
migrated_pg_url = original.migrated_pg_url
owner_engine = original.owner_engine
owner_sessionmaker = original.owner_sessionmaker
pg_container = original.pg_container
remote_work = original.remote_work
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("flow", ["queued", "working", "lost_reply", "invalid_reply"])
async def test_disable_disposes_exact_remote_work_without_claiming_unknown_execution_stopped(
    remote_work, app_user_sessionmaker, monkeypatch, flow,
):
    seed, api, session_id = remote_work
    with remote_server() as remote:
        path = await original.configure(seed, api, remote)
        def reply(request):
            task = {"id": "withdrawn-remote-task", "contextId": "withdrawn-context",
                "status": {"state": "TASK_STATE_CANCELED" if request["method"] == "CancelTask" else "TASK_STATE_WORKING"}}
            # A2A CancelTask returns Task directly, unlike SendMessageResponse.
            return task if request["method"] == "CancelTask" and flow != "invalid_reply" else {"task": task}

        remote.reply = reply
        body = original.command(seed, session_id)
        body["params"]["message"]["parts"] = [{"text": "PRIVATE_SOURCE_REQUEST_BODY"}]
        queued = await api.post(path + "/tasks", json=body)
        assert queued.status_code == 201, queued.text
        task_id = queued.json()["id"]
        if flow != "queued":
            await original.execute(seed, task_id, app_user_sessionmaker)
        if flow == "lost_reply":
            rpc = RemoteA2AEndpoint.rpc

            async def lose_cancel(endpoint, method, *args, **kwargs):
                result = await rpc(endpoint, method, *args, **kwargs)
                if method == "CancelTask":
                    raise RemoteA2AError("remote_a2a_response_lost", execution_status="unknown")
                return result

            monkeypatch.setattr(RemoteA2AEndpoint, "rpc", lose_cancel)
        disabled = await api.post(path + "/disable", json=request_frame(seed, 1))
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["state"] == "disabled" and not disabled.json()["credential_configured"]
        assert "PRIVATE_SOURCE_REQUEST_BODY" not in disabled.text and "synthetic-remote-credential" not in disabled.text
        current = await api.get(path + "/tasks/" + task_id)
        assert current.status_code == 200, current.text
        view = current.json()
        assert view["status"] == ("needs_reconciliation" if flow in {"lost_reply", "invalid_reply"} else "killed"), view
        posts = [item[3] for item in remote.requests if item[0] == "POST"]
        assert [item["method"] for item in posts] == ([] if flow == "queued" else ["SendMessage", "CancelTask"])
        if flow != "queued":
            assert posts[-1]["params"]["id"] == "withdrawn-remote-task"
            assert remote.requests[-1][2]["Authorization"] == "Bearer synthetic-remote-credential"
        if flow in {"lost_reply", "invalid_reply"}:
            assert disabled.json()["withdrawal"]["unknown_count"] == 1
            assert "abandon" in view["recovery_actions"] and "send_message" not in view["recovery_actions"]
            assert "cancel" not in view["recovery_actions"]
        else:
            key = "not_started_count" if flow == "queued" else "cancel_reported_count"
            assert disabled.json()["withdrawal"][key] == 1
        replay = await api.post(path + "/disable", json=request_frame(seed, 1))
        assert replay.status_code == 200 and replay.json()["withdrawal"] == disabled.json()["withdrawal"], replay.text
        assert len([item for item in remote.requests if item[0] == "POST"]) == len(posts)
        if flow in {"lost_reply", "invalid_reply"}:
            abandoned = await api.post(path + f"/tasks/{task_id}/operations", json={**request_frame(seed, 1),
                "operation_id": str(uuid4()), "action": "abandon", "params": {}})
            assert abandoned.status_code == 200 and abandoned.json()["local_abandoned"], abandoned.text
            assert len([item for item in remote.requests if item[0] == "POST"]) == len(posts)
        rejected = await api.post(path + "/tasks", json={**body, "request_id": str(uuid4())})
        assert rejected.status_code == 409, rejected.text
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(task_id))
            connection = await db.get(RemoteA2AConnection, seed["agent"])
            await db.refresh(connection, ["credential_config"])
            assert task.root_user_id == seed["owner"] and task.parent_session_id == str(session_id)
            assert connection.credential_config == {}
            audit = list(await db.scalars(select(AuditLog).where(AuditLog.agent_id == seed["agent"], AuditLog.action == "remote_a2a:disabled")))
            assert len(audit) == 1 and audit[0].user_id == seed["owner"]
            assert "synthetic-remote-credential" not in json.dumps(task.metadata_json)


@pytest.mark.parametrize("restore_previous", [False, True, "new_configuration"])
async def test_withdrawal_during_first_http_gap_cancels_only_after_the_exact_late_identity(
    remote_work, app_user_sessionmaker, monkeypatch, restore_previous,
):
    seed, api, session_id = remote_work
    received, release = asyncio.Event(), asyncio.Event()
    active_hash = None
    rpc = RemoteA2AEndpoint.rpc

    async def hold_first_reply(endpoint, method, *args, **kwargs):
        result = await rpc(endpoint, method, *args, **kwargs)
        if method == "SendMessage":
            received.set()
            await release.wait()
        return result

    monkeypatch.setattr(RemoteA2AEndpoint, "rpc", hold_first_reply)
    with remote_server() as remote:
        path = await original.configure(seed, api, remote)
        remote.reply = lambda request: ({"id": "late-identity", "status": {"state": "TASK_STATE_CANCELED"}}
            if request["method"] == "CancelTask" else {"task": {"id": "late-identity", "status": {"state": "TASK_STATE_WORKING"}}})
        queued = await api.post(path + "/tasks", json=original.command(seed, session_id))
        assert queued.status_code == 201, queued.text
        task_id = queued.json()["id"]
        worker = asyncio.create_task(original.execute(seed, task_id, app_user_sessionmaker))
        try:
            await asyncio.wait_for(received.wait(), 10)
            disabled = await asyncio.wait_for(api.post(path + "/disable", json=request_frame(seed, 1)), 10)
            assert disabled.status_code == 200 and disabled.json()["withdrawal"]["unknown_count"] == 1, disabled.text
            if restore_previous == "new_configuration":
                from tests.integration.test_m3n_remote_a2a_connections_postgres import configuration

                changed = await api.put(path, json={**configuration(seed, remote), "expected_version": 2,
                    "bearer_token": "replacement-synthetic-credential"})
                assert changed.status_code == 200 and changed.json()["version"] == 3, changed.text
                from tests.integration.test_m3q_remote_runtime_postgres import selection
                from app.models.agent import Agent
                from app.services.remote_a2a_runtime import require_runtime_authorization

                assert (await api.post(path + "/discover", json=request_frame(seed, 3))).status_code == 200
                selected = await api.post(path + "/runtime", json=selection(seed, expected_version=3))
                assert selected.status_code == 200, selected.text
                async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                    _connection, _authorization, active_hash = await require_runtime_authorization(db, agent=await db.get(Agent, seed["agent"]))
            elif restore_previous:
                from tests.integration.test_m3q_remote_runtime_postgres import selection

                restored = await api.post(path + "/runtime", json=selection(seed, expected_version=2, selected_runtime="previous"))
                assert restored.status_code == 200, restored.text
            assert [item[3]["method"] for item in remote.requests if item[0] == "POST"] == ["SendMessage"]
            release.set()
            await asyncio.wait_for(worker, 15)
            actual = await api.get(path + "/tasks/" + task_id)
            expected = "needs_reconciliation" if restore_previous == "new_configuration" else "killed"
            assert actual.status_code == 200 and actual.json()["status"] == expected, actual.text
            assert [item[3]["method"] for item in remote.requests if item[0] == "POST"] == (
                ["SendMessage"] if restore_previous == "new_configuration" else ["SendMessage", "CancelTask"])
            profile = (await api.get(path)).json()
            assert profile["withdrawal"]["unknown_count"] == (1 if restore_previous == "new_configuration" else 0)
            assert profile["withdrawal"]["cancel_reported_count"] == (0 if restore_previous == "new_configuration" else 1)
            if restore_previous == "new_configuration":
                assert not profile["withdrawal"]["current_connection_withdrawn"] and profile["state"] == "discovered"
                assert actual.json()["connection_withdrawal"]["status"] == "unavailable"
                assert profile["withdrawal"]["unavailable_count"] == 1 and profile["withdrawal"]["waiting_remote_identity_count"] == 0
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, UUID(task_id))
                operations = task.metadata_json["remote_a2a"]["operations"]
                assert operations[0]["observation"]["task"]["id"] == "late-identity"
                if restore_previous != "new_configuration":
                    assert len(operations) == 2 and operations[1]["params"] == {"id": "late-identity"}
                else:
                    assert len(operations) == 1
                    await require_runtime_authorization(db, agent=await db.get(Agent, seed["agent"]), expected_hash=active_hash)
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)


async def test_manager_withdrawal_does_not_impersonate_or_disclose_the_original_requester(remote_work, app_user_sessionmaker):
    from app.core.security import create_access_token
    from app.models.user import User

    seed, api, session_id = remote_work
    manager_id = uuid4()
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        manager = User(id=manager_id, tenant_id=seed["tenant"], username=f"manager-{manager_id.hex}",
            email=f"{manager_id}@withdrawal.test", display_name="Connection manager", password_hash="synthetic", role="org_admin", is_active=True)
        db.add(manager)
        await db.commit()
    token = create_access_token(str(manager_id), "org_admin", str(seed["tenant"]))
    headers = {"Authorization": "Bearer " + token}
    with remote_server() as remote:
        path = await original.configure(seed, api, remote)
        remote.reply = lambda request: ({"id": "managed-task", "status": {"state": "TASK_STATE_CANCELED"}}
            if request["method"] == "CancelTask" else {"task": {"id": "managed-task", "status": {"state": "TASK_STATE_WORKING"}}})
        body = original.command(seed, session_id)
        body["params"]["message"]["parts"] = [{"text": "PRIVATE_REQUESTER_DETAILS"}]
        created = await api.post(path + "/tasks", json=body)
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        await original.execute(seed, task_id, app_user_sessionmaker)
        assert (await api.get(path + "/tasks/" + task_id, headers=headers)).status_code == 404
        disabled = await api.post(path + "/disable", headers=headers,
            json={**request_frame(seed, 1), "expected_user_id": str(manager_id)})
        assert disabled.status_code == 200 and disabled.json()["withdrawal"]["cancel_reported_count"] == 1, disabled.text
        assert "PRIVATE_REQUESTER_DETAILS" not in disabled.text
        assert (await api.get(path + "/tasks/" + task_id, headers=headers)).status_code == 404
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(task_id))
            assert task.root_user_id == seed["owner"] and task.metadata_json["remote_a2a"]["operations"][-1]["actor_user_id"] == str(manager_id)
            audit = await db.scalar(select(AuditLog).where(AuditLog.agent_id == seed["agent"], AuditLog.action == "remote_a2a:disabled"))
            assert audit.user_id == manager_id


@pytest.mark.parametrize("revoked", [False, True])
async def test_shared_goal_uses_the_actual_sdk_receiver_and_live_team_authority(remote_work, app_user_sessionmaker, monkeypatch, tmp_path, revoked):
    from app.api import business_teams, chat_sessions, tasks
    from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
    from app.models.task import Task
    from app.models.session_v2 import SessionModelResult
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from tests.integration import test_m3t_remote_chat_postgres as chat
    from tests.integration.test_m3q_remote_runtime_postgres import selection
    from tests.integration.test_m3u_remote_sdk_context_postgres import sdk_receiver

    seed, api, original_session = remote_work
    for router in (business_teams.router, chat_sessions.router, tasks.router):
        api._transport.app.include_router(router)
    await chat.prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path)
    team_id, task_id = uuid4(), uuid4()
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        db.add(BusinessTeam(id=team_id, tenant_id=seed["tenant"], name="Remote delivery team", created_by_user_id=seed["owner"], status="active"))
        await db.flush()
        db.add(BusinessTeamMembership(tenant_id=seed["tenant"], business_team_id=team_id, user_id=seed["owner"], role="lead", status="active"))
        task = Task(id=task_id, tenant_id=seed["tenant"], agent_id=seed["agent"], created_by=seed["owner"],
            title="Reviewed SDK goal", description="SHARED_TEAM_GOAL_FOR_REMOTE_DELIVERY", root_session_id=original_session,
            authority_state="owned", request_id="shared-sdk-work", request_hash="controlled")
        db.add(task)
        await db.commit()
        await db.refresh(task)
    base = f"/agents/{seed['agent']}"
    shared = await api.post(base + f"/tasks/{task_id}/team-sharing", json={"request_id": str(uuid4()),
        "business_team_id": str(team_id), "responsible_user_id": str(seed["owner"]),
        "expected_updated_at": task.updated_at.isoformat(), "reason": "Review this team goal for my own authorized work"})
    assert shared.status_code == 200, shared.text
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        (await db.get(Task, task_id)).description = "PRIVATE_SHARED_SDK_TASK_CANARY"
        await db.commit()
    async with sdk_receiver(tmp_path / "shared-sdk", True) as receiver:
        path = await original.configure(seed, api, receiver)
        assert (await api.post(path + "/runtime", json=selection(seed))).status_code == 200
        created = await api.post(base + "/sessions/runs", json={"content": "Prepare my draft from the reviewed shared goal.",
            "input_id": str(uuid4()), "parts": [shared.json()["task"]["work_context"]]})
        assert created.status_code == 201, created.text
        work = created.json()
        if revoked:
            current = (await api.get(base + f"/tasks/{task_id}")).json()["task"]
            closed = await api.post(base + f"/tasks/{task_id}/team-sharing", json={"request_id": str(uuid4()),
                "business_team_id": None, "responsible_user_id": None, "expected_updated_at": current["updated_at"], "reason": "Withdraw this shared source"})
            assert closed.status_code == 200, closed.text
        await chat.execute_chat(seed, work["run"]["run_id"], app_user_sessionmaker)
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            run = await db.get(RuntimeTask, UUID(work["run"]["run_id"]))
            assert not list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run.id)))
            assert run.root_user_id == seed["owner"] and run.parent_session_id == work["session"]["id"]
            assert UUID(run.parent_session_id) != original_session
            assert run.status == ("needs_reconciliation" if revoked else "completed"), run.metadata_json
        if revoked:
            assert not list(receiver.directory.glob("consumed-*.json"))
            assert run.metadata_json["remote_a2a"]["source_error"]["source_reason_code"].startswith("team_task_")
        else:
            consumed = json.loads((receiver.directory / "consumed-0.json").read_text())
            assert consumed["pid"] == receiver.pid
            assert "SHARED_TEAM_GOAL_FOR_REMOTE_DELIVERY" in consumed["sdk_input"]
            assert "PRIVATE_SHARED_SDK_TASK_CANARY" not in consumed["sdk_input"]
            assert "team_task_input" in {source["source_kind"] for source in consumed["context"]["dependencies"]}
            delivered = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id="shared-sdk-return")
            assert delivered["delivered"] >= 1, delivered
            events = (await api.get(base + f"/sessions/{work['session']['id']}/transcript")).json()
            report = (receiver.directory / "report-0.txt").read_text()
            assert any(event.get("content") == report for event in events)
            await chat.settle_boundary(seed, app_user_sessionmaker, tmp_path, work["run"]["run_id"])
