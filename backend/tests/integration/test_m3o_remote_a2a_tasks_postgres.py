"""Outgoing remote work uses real HTTP and the original RLS-backed journal."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import database
from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.capability_policy import CapabilityPolicy
from app.models.runtime_result import RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.services import runtime_task_worker
from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks
from tests.integration.test_m3n_remote_a2a_connections_postgres import (
    configuration,
    machine_api as _machine_api,
    request_frame,
)
from tests.services.test_remote_a2a_transport import remote_server
from tests.integration import conftest as _database_fixtures

# Recoverable remote work deliberately leaves outbox items for later delivery.
# Give these journeys an independent migrated database, like identity cutover.
app_user_engine = _database_fixtures.app_user_engine
app_user_sessionmaker = _database_fixtures.app_user_sessionmaker
migrated_pg_url = _database_fixtures.migrated_pg_url
owner_engine = _database_fixtures.owner_engine
owner_sessionmaker = _database_fixtures.owner_sessionmaker
pg_container = _database_fixtures.pg_container

machine_api = _machine_api
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def remote_work(machine_api, app_user_sessionmaker, monkeypatch):
    seed, api = machine_api
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        session = ChatSession(id=uuid4(), agent_id=seed["agent"], tenant_id=seed["tenant"], user_id=seed["owner"])
        db.add(session)
        db.add(
            CapabilityPolicy(
                tenant_id=seed["tenant"], capability="agent.message.send", allowed=True, requires_approval=False
            )
        )
        await db.commit()
    yield seed, api, session.id


async def configure(seed, api, remote):
    path = f"/agents/{seed['agent']}/remote-a2a"
    assert (await api.put(path, json=configuration(seed, remote))).status_code == 200
    found = await api.post(path + "/discover", json=request_frame(seed, 1))
    assert found.status_code == 200 and found.json()["state"] == "discovered"
    return path


def message(text="Produce the report.", *, task_id=None, context_id=None):
    value = {"messageId": str(uuid4()), "role": "ROLE_USER", "parts": [{"text": text}]}
    if task_id:
        value["taskId"] = task_id
    if context_id:
        value["contextId"] = context_id
    return {"message": value}


def command(seed, session_id):
    return {
        **request_frame(seed, 1),
        "request_id": str(uuid4()),
        "source_session_id": str(session_id),
        "params": message(),
        "uncontrolled_effects_acknowledged": True,
        "unknown_cost_acknowledged": True,
    }


async def execute(seed, task_id, app_user_sessionmaker):
    worker = "remote-reference-" + uuid4().hex
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        claimed = await RuntimeTaskClaimService(db=db, worker_id=worker, task_types=("remote_a2a",)).claim_available()
        task = next(row for row in claimed if row.id == UUID(task_id))
    assert runtime_task_worker._dispatch_claimed_task(task)
    _kind, process = runtime_task_worker._DISPATCHED_TASKS[task.id.hex]
    await asyncio.wait_for(process, 30)


async def test_remote_task_preserves_full_result_and_original_work_return_once(remote_work, app_user_sessionmaker):
    seed, api, session_id = remote_work
    text = "Complete remote result\n" * 4000
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        remote.reply = lambda request: {
            "task": {
                "id": "remote-task",
                "contextId": "remote-context",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"artifactId": "report", "parts": [{"text": text}]}],
            }
        }
        body = command(seed, session_id)
        queued = await api.post(path + "/tasks", json=body)
        assert queued.status_code == 201, queued.text
        task_id = queued.json()["id"]
        assert queued.json()["status"] == "pending" and len(remote.requests) == 1
        await execute(seed, task_id, app_user_sessionmaker)
        result = await api.get(path + "/tasks/" + task_id)
        assert result.status_code == 200, result.text
        payload = result.json()
        assert payload["status"] == "completed" and payload["cost_metering"] == "unknown"
        assert payload["observation"]["task"]["artifacts"][0]["parts"][0]["text"] == text
        replay = await api.post(path + "/tasks", json=body)
        assert replay.status_code == 201 and replay.json()["id"] == task_id and len(remote.requests) == 2
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            rows = list(
                await db.scalars(select(RuntimeResultObject).where(RuntimeResultObject.source_kind == "remote_a2a"))
            )
            assert len(rows) == 1
            returned = json.loads(json.loads(rows[0].payload_bytes)["summary"])["remote_a2a"]
            assert returned["source_session_id"] == str(session_id)
            assert returned["observation"] == payload["observation"]
        outbox = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        delivery = await outbox.drain_once(worker_id="remote-original-work-return")
        assert delivery["claimed"] == delivery["delivered"] == 1, delivery
        assert (await outbox.drain_once(worker_id="remote-original-work-return"))["claimed"] == 0
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            event = await db.scalar(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
            assert event is not None
            assert event.metadata_json["result_manifest"]["items"][0]["source_kind"] == "remote_a2a"
            assert event.metadata_json["result_refs"][0].startswith("runtime-result://")


async def test_expired_dispatch_is_unknown_and_never_sends_again(remote_work, app_user_sessionmaker):
    seed, api, session_id = remote_work
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        body = command(seed, session_id)
        queued = await api.post(path + "/tasks", json=body)
        assert queued.status_code == 201, queued.text
        task_id = queued.json()["id"]
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(task_id))
            metadata = deepcopy(task.metadata_json)
            metadata["remote_a2a"]["operations"][-1]["phase"] = "dispatching"
            task.metadata_json = metadata
            task.status, task.claimed_by, task.claim_version = "running", "dead-remote-worker", 1
            task.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
            await db.commit()
            await db.refresh(task)
            assert task.metadata_json["remote_a2a"]["operations"][-1]["phase"] == "dispatching"
        await reconcile_orphaned_runtime_tasks()
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(task_id))
            assert task.status == "running"
        await execute(seed, task_id, app_user_sessionmaker)
        result = await api.get(path + "/tasks/" + task_id)
        assert result.json()["status"] == "needs_reconciliation"
        assert result.json()["error_code"] == "remote_a2a_dispatch_unconfirmed" and len(remote.requests) == 1
        assert (await api.post(path + "/tasks", json=body)).json()["id"] == task_id
        assert len(remote.requests) == 1


@pytest.mark.parametrize("changed", ["connection", "employee", "policy"])
async def test_late_remote_reply_does_not_revive_disabled_authority(
    remote_work, app_user_sessionmaker, monkeypatch, changed
):
    seed, api, session_id = remote_work
    received, release = asyncio.Event(), asyncio.Event()
    original = RemoteA2AEndpoint.rpc

    async def delayed(endpoint, *args, **kwargs):
        value = await original(endpoint, *args, **kwargs)
        received.set()
        await release.wait()
        return value

    monkeypatch.setattr(RemoteA2AEndpoint, "rpc", delayed)
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        remote.reply = lambda request: {"task": {"id": "remote-task", "status": {"state": "TASK_STATE_COMPLETED"}}}
        queued = await api.post(path + "/tasks", json=command(seed, session_id))
        assert queued.status_code == 201, queued.text
        task_id = queued.json()["id"]
        pending = asyncio.create_task(execute(seed, task_id, app_user_sessionmaker))
        try:
            await asyncio.wait_for(received.wait(), 10)
            if changed == "connection":
                assert (await api.post(path + "/disable", json=request_frame(seed, 1))).status_code == 200
            else:
                from app.models.agent import Agent

                async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                    if changed == "employee":
                        employee = await db.get(Agent, seed["agent"])
                        employee.deactivated_at = datetime.now(timezone.utc)
                    else:
                        policy = await db.scalar(
                            select(CapabilityPolicy).where(
                                CapabilityPolicy.tenant_id == seed["tenant"],
                                CapabilityPolicy.capability == "agent.message.send",
                                CapabilityPolicy.agent_id.is_(None),
                            )
                        )
                        policy.allowed = False
                    await db.commit()
            release.set()
            await asyncio.wait_for(pending, 10)
            result = (await api.get(path + "/tasks/" + task_id)).json()
            assert result["status"] == "needs_reconciliation"
            assert result["error_code"] == "remote_a2a_authority_changed"
            assert result["observation"]["task"]["id"] == "remote-task" and len(remote.requests) == 2
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_remote_question_reply_stays_on_same_task_and_original_work(remote_work, app_user_sessionmaker):
    seed, api, session_id = remote_work
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        remote.reply = lambda request: {
            "task": {
                "id": "remote-task",
                "contextId": "remote-context",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {
                        "messageId": "question",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "Which reporting period?"}],
                    },
                },
            }
        }
        created = await api.post(path + "/tasks", json=command(seed, session_id))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        await execute(seed, task_id, app_user_sessionmaker)
        waiting = (await api.get(path + "/tasks/" + task_id)).json()
        assert waiting["status"] == "suspended" and waiting["remote_state"] == "TASK_STATE_INPUT_REQUIRED"
        body = {
            **request_frame(seed, 1),
            "operation_id": str(uuid4()),
            "action": "send_message",
            "params": message(
                "2026 Q3, including the complete attachment.", task_id="remote-task", context_id="remote-context"
            ),
        }
        remote.reply = lambda request: {
            "task": {
                "id": "remote-task",
                "contextId": "remote-context",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"artifactId": "report", "parts": [{"text": "Full final report"}]}],
            }
        }
        queued = await api.post(path + f"/tasks/{task_id}/operations", json=body)
        assert queued.status_code == 200, queued.text
        await execute(seed, task_id, app_user_sessionmaker)
        completed = (await api.get(path + "/tasks/" + task_id)).json()
        assert completed["id"] == task_id and completed["source_session_id"] == str(session_id)
        assert completed["status"] == "completed" and len(completed["operations"]) == 2
        posted = [r[3] for r in remote.requests if r[0] == "POST"]
        assert posted[-1]["params"] == body["params"] and len(posted) == 2
        listed = await api.get(path + "/tasks", params={"source_session_id": str(session_id)})
        assert listed.status_code == 200 and len(listed.json()["items"]) == 1
        assert "operations" not in listed.json()["items"][0] and "observation" not in listed.json()["items"][0]
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            rows = list(
                await db.scalars(select(RuntimeResultObject).where(RuntimeResultObject.source_kind == "remote_a2a"))
            )
            returns = [json.loads(json.loads(r.payload_bytes)["summary"])["remote_a2a"] for r in rows]
            assert len(returns) == 2 and all(r["source_session_id"] == str(session_id) for r in returns)
            assert any("Which reporting period?" in json.dumps(r["observation"]) for r in returns)


async def test_cancel_unknown_is_not_confirmed_and_readonly_reconciliation_does_not_resend_cancel(
    remote_work, app_user_sessionmaker, monkeypatch
):
    seed, api, session_id = remote_work
    original = RemoteA2AEndpoint.rpc

    async def lost_cancel(endpoint, method, *args, **kwargs):
        reply = await original(endpoint, method, *args, **kwargs)
        if method == "CancelTask":
            raise RemoteA2AError("remote_a2a_transport_unavailable", execution_status="unknown")
        return reply

    monkeypatch.setattr(RemoteA2AEndpoint, "rpc", lost_cancel)
    with remote_server() as remote:
        path = await configure(seed, api, remote)

        def respond(request):
            task = {
                "id": "remote-task",
                "contextId": "remote-context",
                "status": {
                    "state": "TASK_STATE_WORKING" if request["method"] == "SendMessage" else "TASK_STATE_CANCELED"
                },
            }
            return {"task": task} if request["method"] == "SendMessage" else task

        remote.reply = respond
        created = await api.post(path + "/tasks", json=command(seed, session_id))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        await execute(seed, task_id, app_user_sessionmaker)
        operation_path = path + f"/tasks/{task_id}/operations"
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            policy = await db.scalar(
                select(CapabilityPolicy).where(
                    CapabilityPolicy.tenant_id == seed["tenant"],
                    CapabilityPolicy.agent_id.is_(None),
                    CapabilityPolicy.capability == "agent.message.send",
                )
            )
            policy.allowed = False
            await db.commit()
        cancel = {**request_frame(seed, 1), "operation_id": str(uuid4()), "action": "cancel"}
        assert (await api.post(operation_path, json=cancel)).status_code == 200
        await execute(seed, task_id, app_user_sessionmaker)
        unknown = (await api.get(path + "/tasks/" + task_id)).json()
        assert unknown["status"] == "needs_reconciliation" and unknown["remote_state"] == "TASK_STATE_WORKING"
        assert unknown["cost_metering"] == "unknown" and "send_message" not in unknown["recovery_actions"]
        assert (await api.post(operation_path, json=cancel)).status_code == 200
        refresh = {**request_frame(seed, 1), "operation_id": str(uuid4()), "action": "refresh"}
        assert (await api.post(operation_path, json=refresh)).status_code == 200
        await execute(seed, task_id, app_user_sessionmaker)
        reconciled = (await api.get(path + "/tasks/" + task_id)).json()
        assert reconciled["status"] == "killed" and reconciled["remote_state"] == "TASK_STATE_CANCELED"
        methods = [r[3]["method"] for r in remote.requests if r[0] == "POST"]
        assert methods == ["SendMessage", "CancelTask", "GetTask"]


async def test_remote_admission_requires_exact_owner_request_and_does_not_claim_controls(
    remote_work, app_user_sessionmaker
):
    seed, api, session_id = remote_work
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        body = command(seed, session_id)
        for field in ("unknown_cost_acknowledged", "uncontrolled_effects_acknowledged"):
            denied = await api.post(path + "/tasks", json={**body, field: False})
            assert (
                denied.status_code == 409
                and denied.json()["detail"]["code"] == "remote_a2a_owner_authorization_required"
            )
        denied = await api.post(path + "/tasks", json={**body, "required_controls": ["hard_cost_limit"]})
        assert (
            denied.status_code == 409 and denied.json()["detail"]["code"] == "remote_a2a_required_control_unavailable"
        )
        assert (await api.post(path + "/tasks", json={**body, "source_session_id": str(uuid4())})).status_code == 404
        assert (await api.post(path + "/tasks", json={**body, "expected_user_id": str(uuid4())})).status_code == 409
        assert (
            await api.post(path + "/tasks", json={**body, "params": message(task_id="unbound-remote-task")})
        ).status_code == 422
        assert (
            await api.post(path + "/tasks", json={**body, "params": message("synthetic-remote-credential")})
        ).status_code == 422
        assert len(remote.requests) == 1
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            assert not list(await db.scalars(select(RuntimeTask).where(RuntimeTask.task_type == "remote_a2a")))


@pytest.mark.parametrize("allowed,approval,status", [(False, False, 403), (True, True, 409)])
async def test_owner_ack_cannot_override_company_policy(remote_work, app_user_sessionmaker, allowed, approval, status):
    seed, api, session_id = remote_work
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        policy = await db.scalar(
            select(CapabilityPolicy).where(
                CapabilityPolicy.tenant_id == seed["tenant"],
                CapabilityPolicy.agent_id.is_(None),
                CapabilityPolicy.capability == "agent.message.send",
            )
        )
        policy.allowed, policy.requires_approval = allowed, approval
        db.add(
            CapabilityPolicy(
                tenant_id=seed["tenant"],
                agent_id=seed["agent"],
                capability="agent.message.send",
                allowed=True,
                requires_approval=False,
            )
        )
        await db.commit()
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        denied = await api.post(path + "/tasks", json=command(seed, session_id))
        assert denied.status_code == status, denied.text
        assert len(remote.requests) == 1


async def test_uncontrolled_remote_cannot_waive_authoritative_policy_conditions(remote_work, app_user_sessionmaker):
    seed, api, session_id = remote_work
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        policy = await db.scalar(
            select(CapabilityPolicy).where(
                CapabilityPolicy.tenant_id == seed["tenant"],
                CapabilityPolicy.agent_id.is_(None),
                CapabilityPolicy.capability == "agent.message.send",
            )
        )
        policy.conditions = {"required_controls": ["hard_cost_limit"]}
        await db.commit()
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        denied = await api.post(path + "/tasks", json=command(seed, session_id))
        assert (
            denied.status_code == 409 and denied.json()["detail"]["code"] == "remote_a2a_required_control_unavailable"
        )
        assert len(remote.requests) == 1


async def test_original_work_ui_uses_real_owner_http_worker_and_remote_receipt(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path
):
    from fastapi import FastAPI
    from app.api import agents
    from app.database import get_db
    from app.models.agent import Agent
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge

    seed, api, session_id = remote_work
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        agent = await db.get(Agent, seed["agent"])
        agent.agent_type = "native"
        await db.commit()
    web = FastAPI()
    web.include_router(agents.router, prefix="/api")

    async def dependency():
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            yield db

    web.dependency_overrides[get_db] = dependency
    queued, finished = asyncio.Queue(), asyncio.Event()
    failures = []

    async def wake(**kwargs):
        queued.put_nowait(str(kwargs["runtime_task_id"]))

    async def consume():
        try:
            task_id = await asyncio.wait_for(queued.get(), 20)
            await execute(seed, task_id, app_user_sessionmaker)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    @web.get("/__remote-test/wait")
    async def wait_for_worker():
        await asyncio.wait_for(finished.wait(), 15)
        assert not failures, failures
        return {"worker_finished": True}

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", wake)
    with remote_server() as remote:
        await configure(seed, api, remote)
        remote.reply = lambda request: {
            "task": {
                "id": "remote-task",
                "contextId": "remote-context",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"artifactId": "report", "parts": [{"text": "NATIVE_REMOTE_FULL_RESULT\n" * 4000}]}],
            }
        }
        worker = asyncio.create_task(consume())
        try:
            await _exercise_dom_bridge(
                web,
                {"user_id": seed["owner"], "tenant_id": seed["tenant"], "agent_id": seed["agent"]},
                tmp_path,
                entry_file="e2e/remote-a2a-work-native.tsx",
                extra_env={"HIVE_M3N_TOKEN": seed["user_token"], "HIVE_M3O_SESSION_ID": str(session_id)},
            )
            await asyncio.wait_for(worker, 5)
            assert not failures
            posts = [row[3] for row in remote.requests if row[0] == "POST"]
            assert len(posts) == 1 and posts[0]["method"] == "SendMessage"
            assert posts[0]["params"]["message"]["parts"][0]["text"] == "Prepare the complete remote report."
        finally:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
