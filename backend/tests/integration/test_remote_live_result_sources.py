"""Live remote results use the same current sources as saved result readers."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.remote_a2a_artifacts import load_packet
from app.services.remote_a2a_transport import RemoteA2AEndpoint
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
from tests.integration.test_m3o_remote_a2a_tasks_postgres import configure, machine_api, remote_work  # noqa: F401
from tests.integration.test_m3q_remote_runtime_postgres import selection
from tests.integration.test_m3t_remote_chat_postgres import execute_chat, prepare_employee
from tests.services.test_remote_a2a_transport import remote_server

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
BODY = "REMOTE_RESULT_FROM_PINNED_ORIGINAL_CONTEXT"


@pytest.mark.parametrize("source_change", ["none", "before_refresh", "during_refresh"])
async def test_live_remote_refresh_revalidates_saved_packet_sources(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, source_change,  # noqa: F811
):
    from app.api import chat_sessions
    from app.models.user import User
    from app.services import web_chat_runtime

    seed, api, session_id = remote_work
    api._transport.app.include_router(chat_sessions.router)
    await prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path, personality=BODY)
    source = tmp_path / str(seed["agent"]) / "soul.md"
    original = source.read_bytes()
    events = []

    async def capture(agent_id, work_id, event):
        assert agent_id == seed["agent"] and work_id == session_id
        events.append(event)

    monkeypatch.setattr(web_chat_runtime, "broadcast_web_chat_event", capture)
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        assert (await api.post(path + "/runtime", json=selection(seed))).status_code == 200
        remote.reply = lambda _request: {"task": {"id": "live-source-task", "contextId": "live-source-context",
            "status": {"state": "TASK_STATE_WORKING"}}}
        response = await api.post(f"/agents/{seed['agent']}/sessions/{session_id}/runs", json={
            "content": "Use the complete authorized context to finish the original work.",
            "input_id": str(uuid4()), "idempotency_key": "remote-live-source"})
        assert response.status_code == 201, response.text
        run_id = UUID(response.json()["run_id"])
        await execute_chat(seed, str(run_id), app_user_sessionmaker)
        posts = [row[3] for row in remote.requests if row[0] == "POST"]
        assert len(posts) == 1 and posts[0]["method"] == "SendMessage"
        assert BODY in json.dumps(posts[0]["params"])
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            assert (await db.get(RuntimeTask, run_id)).status == "suspended"
        events.clear()
        remote.reply = lambda _request: {"id": "live-source-task", "contextId": "live-source-context",
            "status": {"state": "TASK_STATE_COMPLETED", "message": {"messageId": "live-source-result",
                "role": "ROLE_AGENT", "parts": [{"text": BODY}]}}}
        if source_change == "before_refresh":
            source.write_text("Changed after the original remote input was captured.")
        elif source_change == "during_refresh":
            rpc = RemoteA2AEndpoint.rpc

            async def change_after_reply(endpoint, method, *args, **kwargs):
                result = await rpc(endpoint, method, *args, **kwargs)
                if method == "GetTask":
                    source.write_text("Changed while the remote observation was in flight.")
                return result

            monkeypatch.setattr(RemoteA2AEndpoint, "rpc", change_after_reply)
        queued = await api.post(path + f"/tasks/{run_id}/operations", json={
            "expected_user_id": str(seed["owner"]), "expected_tenant_id": str(seed["tenant"]),
            "expected_version": 1, "operation_id": str(uuid4()), "action": "refresh", "params": {}})
        assert queued.status_code == 200, queued.text
        await execute_chat(seed, str(run_id), app_user_sessionmaker)
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, run_id)
            state = task.metadata_json["remote_a2a"]
            assert task.status == "completed" and state["operations"][-1]["phase"] == "received", state
            assert BODY in json.dumps(state["operations"][-1]["observation"])
            outbox = await db.scalar(select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.source_run_id == f"{run_id}:{state['operations'][-1]['id']}"))
            dependency = {"source_kind": "remote_a2a_result", "source_id": str(outbox.result_object_id),
                "source_version": outbox.result_sha256}
            reader = await db.get(User, seed["owner"])
            if source_change == "none":
                await load_packet(db, seed["tenant"], dependency, user=reader)
            else:
                with pytest.raises(ContentInputReceiptError) as denied:
                    await load_packet(db, seed["tenant"], dependency, user=reader)
                assert denied.value.status_code == 403
                assert denied.value.code == "remote_a2a_instruction_source_unavailable"
        observed = {"source_change": source_change, "events": events, "dependency": dependency}
        (tmp_path / "live-result-source.json").write_text(json.dumps(observed, indent=2, default=str))
        if source_change == "none":
            assert any(event.get("type") == "done" and event.get("content") == BODY for event in events), observed
        else:
            assert BODY not in json.dumps(events), observed
            assert any(event.get("type") == "run_updated" and event.get("status") == "completed"
                and event.get("content_status") == "source_unavailable"
                and event.get("content_recovery_action") == "reconcile_instruction_sources"
                for event in events), observed
            source.write_bytes(original)
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                reader = await db.get(User, seed["owner"])
                _task, _result, _session, packet = await load_packet(db, seed["tenant"], dependency, user=reader)
                assert BODY in json.dumps(packet)
        returned = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(
            worker_id="remote-live-original-return")
        assert returned["claimed"] == returned["delivered"] == 2, returned
        observed["original_work_return"] = returned
        (tmp_path / "live-result-source.json").write_text(json.dumps(observed, indent=2, default=str))
        posts = [row[3] for row in remote.requests if row[0] == "POST"]
        assert [post["method"] for post in posts] == ["SendMessage", "GetTask"]
