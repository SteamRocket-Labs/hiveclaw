"""Member permission frames keep the proposal and resolve through the original API."""

import copy
import json
import uuid
from types import SimpleNamespace

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _prepare_write,
    native_workspace,  # noqa: F401
)
from tests.integration.test_session_permission_execution_once import _mounted_client

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def test_permission_projection_preserves_authored_json_and_argument_fields():
    from app.services.session_event_contract import serialize_session_event
    from tests.services.test_session_event_contract import _event

    authored = {"content": json.dumps({"permission_request": {"permission_mode": "example"}})}
    assert serialize_session_event(_event(payload=authored), audience="user")["payload"] == authored
    arguments = {"permission_request": {"permission_profile": "a proposed document field"}}
    payload = {"permission_request": {"permission_request_id": "request", "arguments": arguments}}
    assert serialize_session_event(
        _event(item_kind="tool_permission", lifecycle="waiting", payload=payload), audience="user",
    )["payload"] == payload


def _assert_member_frame(frame):
    forbidden = {
        "pending_tool_frame", "round_state", "capability", "permission_mode",
        "decision_reason", "permission_profile",
    }
    # Check fields, including JSON embedded in legacy content; ordinary prose
    # such as "capability policy" is not a serialized governance field.
    if isinstance(frame, dict):
        assert not forbidden.intersection(frame), forbidden.intersection(frame)
        for value in frame.values():
            _assert_member_frame(value)
    elif isinstance(frame, list):
        for value in frame:
            _assert_member_frame(value)
    elif isinstance(frame, str):
        try:
            decoded = json.loads(frame)
        except ValueError:
            return
        if isinstance(decoded, (dict, list)):
            _assert_member_frame(decoded)


@pytest.mark.parametrize(("tool_name", "arguments", "action"), [
    ("plaza_create_post", {"content": "Approved team update"}, "allow_once"),
    ("plaza_create_post", {"content": "Session-approved team update"}, "allow_session"),
    ("delete_file", {"path": "workspace/obsolete.txt"}, "deny"),
])
async def test_member_permission_frames_and_original_reply(
    native_workspace, owner_sessionmaker, monkeypatch, tool_name, arguments, action,  # noqa: F811
):
    from sqlalchemy import select

    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.plaza import PlazaPost
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services import runtime_task_worker, session_tool_runtime, web_chat_runtime
    from app.services.session_event_contract import serialize_session_event
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_permission_views import read_session_run_permissions
    from app.services.web_chat_stream_bus import _user_live_projection
    from app.models.user import User

    n = native_workspace
    delivered = []

    async def capture(_agent_id, _session_id, payload):
        delivered.append(payload)

    async def publish_legacy(event):
        if event.get("status") != "session_permission_required":
            return
        event = {**event, "runtime_task_id": str(n.run)}
        # Both the uncommitted callback and the committed compatibility route
        # are member surfaces; the latter bypasses the ordinary broker strip.
        await web_chat_runtime.broadcast_web_chat_event(n.agent, n.session, event)
        committed = await web_chat_runtime._persist_runtime_event(
            agent_id=n.agent, user_id=n.user, session_id=str(n.session), data=event,
        )
        await web_chat_runtime.broadcast_web_chat_event(n.agent, n.session, committed)

    async def notify(**_kwargs):
        return None

    monkeypatch.setattr(web_chat_runtime.web_chat_broker, "send_session_message", capture)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    async with owner_sessionmaker() as db:
        invocation, effective, turn = await _prepare_write(db, n, tool_name=tool_name, arguments=arguments)
        trace = {}
        result = await n.service.execute(
            tool_name, effective, agent_id=n.agent, user_id=n.user, session_id=str(n.session),
            runtime_task_id=str(n.run), turn_id=turn, tool_call_id=invocation.provider_tool_use_id,
            event_callback=publish_legacy, emit_runtime_hooks=False, trace_metadata_sink=trace,
        )
        raw_receipt = copy.deepcopy(json.loads(str(result)))
        waiting = await session_tool_runtime.complete_tool_invocation(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, invocation_id=invocation.id,
            provider_result_content=str(result), execution_evidence=_execution_evidence(trace),
            effective_arguments=trace["effective_arguments"],
        )
        canonical = next(event for event in waiting if event.item_kind == "tool_permission")
        run = await db.get(RuntimeTask, n.run)
        run.status = "suspended"
        session = await db.get(ChatSession, n.session)
        session.runtime_task_id = n.run
        await db.commit()
        legacy = delivered[-1]
        operator_legacy = serialize_session_event(await db.get(ChatTranscriptEvent, uuid.UUID(legacy["event_id"])))
        operator_canonical = serialize_session_event(canonical)
        user_canonical = serialize_session_event(canonical, audience="user")
        await web_chat_runtime.broadcast_web_chat_event(n.agent, n.session, user_canonical)

        seed = SimpleNamespace(tenant_id=n.tenant, user_id=n.user, agent_id=n.agent,
                               session_id=n.session, run_id=n.run)
        async with _mounted_client(n.app_user_sessionmaker, seed) as client:
            path = f"/api/agents/{n.agent}/sessions/{n.session}"
            reload = await client.get(f"{path}/transcript", params={"schema_version": 2})
            assert reload.status_code == 200, reload.text
            reload_frames = reload.json()
            canonical_reload = next(frame for frame in reload_frames if frame.get("item_id") == str(canonical.item_id))
            legacy_reload = next(frame for frame in reload_frames if frame.get("event_id") == legacy["event_id"])
            v1 = await client.get(f"{path}/transcript")
            assert v1.status_code == 200, v1.text
            user = await db.get(User, n.user)
            views = await read_session_run_permissions(db, run=run, session=session, user=user)
            assert len(views) == 1
            for frame in [*delivered, canonical_reload, legacy_reload, v1.json(), views,
                          _user_live_projection(operator_legacy), _user_live_projection(operator_canonical)]:
                _assert_member_frame(frame)
            request = user_canonical["payload"]["permission_request"]
            assert set(request) == {
                "permission_request_id", "session_id", "runtime_task_id", "requested_by_user_id",
                "tool_name", "arguments", "allow_session_allowed", "destructive", "expires_at",
                *({"risk_class", "confirmation_kind"} if tool_name == "delete_file" else set()),
            }
            assert request["arguments"] == effective
            assert request == canonical_reload["payload"]["permission_request"] == views[0]["request"]
            for key in ("pending_tool_frame", "round_state", "capability", "permission_mode",
                        "decision_reason", "permission_profile"):
                assert key in json.dumps(operator_legacy)
            assert "decision_reason" in operator_canonical["payload"]["permission_request"]
            assert serialize_session_event(canonical) == operator_canonical
            assert json.loads(str(result)) == raw_receipt
            response = await client.post(
                f"{path}/permissions/{request['permission_request_id']}/resolve", json={"action": action},
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "resolved"
            assert response.json()["run_id"] == str(n.run)

        await db.refresh(invocation)
        assert invocation.permission_state == ("denied" if action == "deny" else "approved")
        assert invocation.result_event_id is not None
        if action != "deny":
            assert invocation.effect_state == "effect_committed"
            post = await db.scalar(select(PlazaPost).where(PlazaPost.author_id == n.agent))
            assert post is not None and post.content == arguments["content"]
        await db.refresh(session)
        grants = (session.transcript_metadata_json or {}).get("session_permission_grants", [])
        assert bool(grants) is (action == "allow_session")
