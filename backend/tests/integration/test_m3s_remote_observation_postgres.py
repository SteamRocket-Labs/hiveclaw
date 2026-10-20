"""Bounded remote observation survives workers and preserves explicit controls."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
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


@pytest.mark.parametrize("flow", ["completed", "limit", "deadline", "unknown", "pause", "cancel", "identity", "input_required", "repeat", "pause_inflight", "policy"])
async def test_owner_observation_is_bounded_durable_and_never_replays_send(
    remote_work, app_user_sessionmaker, monkeypatch, flow
):
    seed, api, session_id = remote_work
    with remote_server() as remote:
        path = await original.configure(seed, api, remote)
        def respond(request):
            state = "TASK_STATE_WORKING"
            if request["method"] == "GetTask":
                state = "TASK_STATE_COMPLETED" if flow == "completed" or (flow == "repeat" and sum(r[0] == "POST" and r[3]["method"] == "GetTask" for r in remote.requests) >= 2) else "TASK_STATE_INPUT_REQUIRED" if flow == "input_required" else state
            if request["method"] == "CancelTask":
                state = "TASK_STATE_CANCELED"
            packet = {"id": "wrong-task" if flow == "identity" and request["method"] == "GetTask" else "observed-task",
                "contextId": "observed-context", "status": {"state": state}}
            return {"task": packet} if request["method"] == "SendMessage" else packet
        remote.reply = respond
        created = await api.post(path + "/tasks", json=original.command(seed, session_id))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        await original.execute(seed, task_id, app_user_sessionmaker)
        operations_path = path + f"/tasks/{task_id}/operations"
        enable = {**request_frame(seed, 1), "operation_id": str(uuid4()), "action": "observe",
            "params": {"max_observations": 2 if flow in {"repeat", "pause_inflight"} else 1, "interval_seconds": 3600, "duration_seconds": 7200}}
        enabled = await api.post(operations_path, json=enable)
        assert enabled.status_code == 200, enabled.text
        saved = enabled.json()
        assert saved["status"] == "resumable" and saved["automatic_observation"]["status"] == "active"
        assert saved["automatic_observation"]["started"] == 0 and saved["automatic_observation"]["next_at"]
        assert (await api.post(operations_path, json=enable)).json()["operations"] == saved["operations"]
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            assert not await RuntimeTaskClaimService(db=db, worker_id="not-due", task_types=("remote_a2a",)).claim_available()
        if flow in {"pause", "cancel"}:
            action = "pause_observation" if flow == "pause" else "cancel"
            stop = {**request_frame(seed, 1), "operation_id": str(uuid4()), "action": action}
            stopped = await api.post(operations_path, json=stop)
            assert stopped.status_code == 200, stopped.text
            if flow == "pause":
                assert stopped.json()["automatic_observation"]["status"] == "paused"
                assert stopped.json()["status"] == "suspended"
                assert (await api.post(operations_path, json=stop)).status_code == 200
                async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                    assert not await RuntimeTaskClaimService(db=db, worker_id="paused", task_types=("remote_a2a",)).claim_available()
                assert [r[3]["method"] for r in remote.requests if r[0] == "POST"] == ["SendMessage"]
                return
            await original.execute(seed, task_id, app_user_sessionmaker)
            result = (await api.get(path + "/tasks/" + task_id)).json()
            assert result["status"] == "killed"
            assert [r[3]["method"] for r in remote.requests if r[0] == "POST"] == ["SendMessage", "CancelTask"]
            return
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(task_id))
            task.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            if flow == "deadline":
                metadata = deepcopy(task.metadata_json)
                metadata["remote_a2a"]["observation_control"]["deadline"] = task.scheduled_at.isoformat()
                task.metadata_json = metadata
            await db.commit()
        if flow == "policy":
            from sqlalchemy import select
            from app.models.capability_policy import CapabilityPolicy
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == seed["tenant"],
                    CapabilityPolicy.capability == "agent.message.send", CapabilityPolicy.agent_id.is_(None)))
                policy.allowed = False
                await db.commit()
        if flow == "unknown":
            rpc = RemoteA2AEndpoint.rpc
            async def lost_reply(endpoint, *args, **kwargs):
                await rpc(endpoint, *args, **kwargs)
                raise RemoteA2AError("remote_a2a_response_lost", execution_status="unknown")
            monkeypatch.setattr(RemoteA2AEndpoint, "rpc", lost_reply)
        if flow == "pause_inflight":
            rpc = RemoteA2AEndpoint.rpc
            async def pause_inflight(endpoint, *args, **kwargs):
                paused = await api.post(operations_path, json={**request_frame(seed, 1),
                    "operation_id": str(uuid4()), "action": "pause_observation"})
                assert paused.status_code == 200 and paused.json()["status"] == "running", paused.text
                return await rpc(endpoint, *args, **kwargs)
            monkeypatch.setattr(RemoteA2AEndpoint, "rpc", pause_inflight)
        await original.execute(seed, task_id, app_user_sessionmaker)
        if flow == "repeat":
            interim = (await api.get(path + "/tasks/" + task_id)).json()
            assert interim["status"] == "resumable" and interim["automatic_observation"]["started"] == 1
            assert len(interim["operations"]) == 3 and interim["operations"][-2]["observation"]["id"] == "observed-task"
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                (await db.get(RuntimeTask, UUID(task_id))).scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()
            await original.execute(seed, task_id, app_user_sessionmaker)
        result = (await api.get(path + "/tasks/" + task_id)).json()
        control = result["automatic_observation"]
        assert control["status"] == "paused" and control["next_at"] is None
        if flow in {"completed", "repeat"}:
            assert result["status"] == "completed" and control["reason"] == "remote_terminal"
        elif flow in {"unknown", "policy"}:
            assert result["status"] == "needs_reconciliation" and control["reason"] == "operation_unconfirmed"
        elif flow == "identity":
            assert result["status"] == "needs_reconciliation" and result["remote_task_id"] == "observed-task"
            assert result["error_code"] == "remote_a2a_task_identity_mismatch"
        else:
            assert result["status"] == "suspended"
            assert control["reason"] == {"limit": "observation_limit", "deadline": "deadline", "input_required": "remote_input_required", "pause_inflight": "owner_paused"}[flow]
        assert control["started"] == (0 if flow in {"deadline", "policy"} else 2 if flow == "repeat" else 1)
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            assert not await RuntimeTaskClaimService(db=db, worker_id="exhausted", task_types=("remote_a2a",)).claim_available()
        assert [r[3]["method"] for r in remote.requests if r[0] == "POST"] == ["SendMessage"] + ([] if flow in {"deadline", "policy"} else ["GetTask", "GetTask"] if flow == "repeat" else ["GetTask"])
