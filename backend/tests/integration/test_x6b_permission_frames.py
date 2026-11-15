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


def _assert_member_frame(frame):
    forbidden = {
        "pending_tool_frame", "round_state", "capability", "permission_mode",
        "decision_reason", "permission_profile", "eventCapability",
    }
    # Check every carrier, including JSON embedded in legacy content. The
    # separate prose assertion also rejects the actual internal field values.
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
    from app.models.audit import ChatMessage
    from app.models.plaza import PlazaPost
    from app.models.runtime_task import RuntimeTask
    from app.models.security_audit import ResourcePermission
    from app.models.session_v2 import SessionEventOutbox
    from app.services.chat_message_parts import serialize_chat_message
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
        operator_serialized = serialize_session_event(canonical)
        # The outbox keeps the full payload with its original direct-user
        # visibility; an operator REST projection changes that visibility.
        operator_canonical = copy.deepcopy((await db.scalar(select(SessionEventOutbox).where(
            SessionEventOutbox.event_id == canonical.id))).envelope_json)
        original_canonical = copy.deepcopy(operator_canonical)
        user_canonical = serialize_session_event(canonical, audience="user")
        # The live producer publishes operator bytes; assert this branch before
        # the raw legacy callback checks so its own regression cannot hide.
        await web_chat_runtime.broadcast_web_chat_event(n.agent, n.session, operator_canonical)
        assert delivered[-1]["event_id"] == str(canonical.id)
        assert delivered[-1]["item_kind"] == "tool_permission"
        _assert_member_frame(delivered[-1])

        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.foreign,
            resource_type="agent", resource_id=n.agent, actions=["operator.inspect"],
            conditions={"operator_inspection": {"schema": "hive.agent.operator_inspection.v1"}}))
        await db.commit()

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
            messages = await client.get(f"{path}/messages")
            assert messages.status_code == 200, messages.text
            assert any(part.get("permission_request") for message in messages.json() for part in message.get("parts", []))
            owner_operator = await client.get(f"{path}/messages", params={"operator_view": "true"})
            assert owner_operator.status_code == 200, owner_operator.text
            operator_seed = SimpleNamespace(**{**vars(seed), "user_id": n.foreign})
            async with _mounted_client(n.app_user_sessionmaker, operator_seed) as operator:
                operator_messages = await operator.get(f"{path}/messages", params={
                    "operator_view": "true", "operator_reason": "Inspect the original pending permission",
                })
                assert operator_messages.status_code == 200, operator_messages.text
            stored_messages = list(await db.scalars(select(ChatMessage).where(
                ChatMessage.conversation_id == str(n.session)).order_by(ChatMessage.created_at)))
            assert operator_messages.json() == [serialize_chat_message(message) for message in stored_messages]
            operator_json = json.dumps(operator_messages.json())
            for key in ("pending_tool_frame", "round_state", "capability", "permission_mode",
                        "decision_reason", "permission_profile", "eventCapability"):
                assert key in operator_json
            user = await db.get(User, n.user)
            views = await read_session_run_permissions(db, run=run, session=session, user=user)
            assert len(views) == 1
            for frame in [owner_operator.json(), *delivered, canonical_reload, legacy_reload, v1.json(), messages.json(), views,
                          _user_live_projection(operator_legacy), _user_live_projection(operator_canonical)]:
                _assert_member_frame(frame)
                encoded = json.dumps(frame)
                for private in (raw_receipt["permission_request"]["capability"],
                                raw_receipt["permission_request"]["decision_reason"],
                                "do not create a backend approval request"):
                    assert private not in encoded
            confirmation = {"plaza_create_post": "需要你的确认：Plaza create post",
                            "delete_file": "需要你的确认：Delete file"}[tool_name]
            for message in [*messages.json(), *owner_operator.json()]:
                for part in message.get("parts", []):
                    if part.get("permission_request"):
                        assert message["content"] == part["text"] == confirmation
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
            assert serialize_session_event(canonical) == operator_serialized
            assert operator_canonical == original_canonical
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


async def test_stored_tool_permission_results_are_projected_on_messages_and_v1_reload(
    native_workspace, owner_sessionmaker,  # noqa: F811
):
    from app.services.chat_transcript import append_session_event

    n = native_workspace
    async with owner_sessionmaker() as db:
        invocation, effective, turn = await _prepare_write(
            db, n, tool_name="delete_file", arguments={"path": "workspace/obsolete.txt"},
        )
        receipt = str(await n.service.execute(
            "delete_file", effective, agent_id=n.agent, user_id=n.user, session_id=str(n.session),
            runtime_task_id=str(n.run), turn_id=turn, tool_call_id=invocation.provider_tool_use_id,
            emit_runtime_hooks=False,
        ))
        request = json.loads(receipt)["permission_request"]
        results = [receipt, "ordinary result\n", ' { "ok": true, "result": "normal" } ']
        events = []
        for result in results:
            events.append(await append_session_event(
                db=db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, user_id=n.user,
                actor_type="tool", event_type="tool_result", role="tool_call", t0_role="tool",
                content=json.dumps({"name": "delete_file", "args": effective, "status": "done", "result": result}),
                metadata={"tool_name": "delete_file", "status": "done"}, source="task_executor",
            ))
        await db.commit()
        seed = SimpleNamespace(tenant_id=n.tenant, user_id=n.user, agent_id=n.agent,
                               session_id=n.session, run_id=n.run)
        async with _mounted_client(n.app_user_sessionmaker, seed) as client:
            path = f"/api/agents/{n.agent}/sessions/{n.session}"
            messages = await client.get(f"{path}/messages")
            # Schema-1 stored rows reload through the compatibility envelope.
            reload = await client.get(f"{path}/transcript", params={"schema_version": 2})
        assert messages.status_code == reload.status_code == 200
        rows = [message for message in messages.json() if message.get("role") == "tool_call"]
        assert len(rows) == len(results)
        frames = [next(frame for frame in reload.json() if frame["event_id"] == str(event.event_id))
                  for event in events]
        _assert_member_frame([rows[0], frames[0]])
        for private in (request["capability"], request["decision_reason"], "do not create a backend approval request"):
            assert private not in json.dumps([rows[0], frames[0]])
        projected = rows[0]["toolResult"]
        assert projected == rows[0]["parts"][0]["result"]
        assert json.loads(projected)["permission_request"]["permission_request_id"] == request["permission_request_id"]
        assert json.loads(frames[0]["payload"]["content"])["result"] == projected
        for row, frame, result in zip(rows[1:], frames[1:], results[1:]):
            assert row["toolResult"] == row["parts"][0]["result"] == result
            assert json.loads(frame["payload"]["content"])["result"] == result
        assert events[0].chat_message.content == events[0].transcript_event.content
        assert json.loads(events[0].transcript_event.content)["result"] == receipt


async def test_mode_denial_projects_permission_events_and_native_results_but_not_legacy_strings(
    native_workspace, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    from sqlalchemy import select

    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.chat_session import ChatSession
    from app.models.plaza import PlazaPost
    from app.models.runtime_task import RuntimeTask
    from app.models.security_audit import ResourcePermission
    from app.models.session_v2 import SessionEventOutbox
    from app.models.user import User
    from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1
    from app.services import session_tool_runtime, web_chat_runtime, web_chat_stream_bus as bus
    from app.services.chat_message_parts import serialize_chat_message
    from app.services.chat_transcript import append_session_event
    from app.services.execution_receipts import canonical_payload_hash
    from app.services.session_event_contract import serialize_session_event
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_permission_views import read_session_run_permissions

    n = native_workspace
    delivered, produced = [], []

    async def capture(_agent, _session, payload):
        delivered.append(payload)

    async def on_event(event):
        if event.get("status") == "permission_denied":
            produced.append(copy.deepcopy(event))
            event = {**event, "runtime_task_id": str(n.run)}
            await web_chat_runtime.broadcast_web_chat_event(n.agent, n.session, event)
            committed = await web_chat_runtime._persist_runtime_event(
                agent_id=n.agent, user_id=n.user, session_id=str(n.session), data=event,
            )
            await web_chat_runtime.broadcast_web_chat_event(n.agent, n.session, committed)

    monkeypatch.setattr(web_chat_runtime.web_chat_broker, "send_session_message", capture)
    async with owner_sessionmaker() as db:
        invocation, arguments, turn = await _prepare_write(
            db, n, tool_name="plaza_create_post", arguments={"content": "Synthetic mode-denied update"},
        )
        trace = {}
        result = await n.service.execute(
            "plaza_create_post", arguments, agent_id=n.agent, user_id=n.user, session_id=str(n.session),
            runtime_task_id=str(n.run), turn_id=turn, tool_call_id=invocation.provider_tool_use_id,
            permission_profile=PermissionProfileV1(mode=PermissionMode.DONT_ASK),
            event_callback=on_event, emit_runtime_hooks=False, trace_metadata_sink=trace,
        )
        assert len(produced) == 1 and "permission_request" not in produced[0]
        assert result.status == "permission_denied"
        assert str(result) == produced[0]["message"]
        legacy_id = uuid.UUID(delivered[-1]["event_id"])
        legacy_row = await db.get(ChatTranscriptEvent, legacy_id)
        operator_permission = serialize_session_event(legacy_row)
        operator_before = copy.deepcopy(operator_permission)
        events = await session_tool_runtime.complete_tool_invocation(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, invocation_id=invocation.id,
            provider_result_content=str(result), execution_evidence=_execution_evidence(trace),
            effective_arguments=trace["effective_arguments"], model_visible_content=str(result),
        )
        native = next(event for event in events if event.item_kind == "tool_result")
        operator_result = serialize_session_event(native)
        canonical = copy.deepcopy((await db.scalar(select(SessionEventOutbox).where(
            SessionEventOutbox.event_id == native.id))).envelope_json)
        original_canonical = copy.deepcopy(canonical)
        await web_chat_runtime.broadcast_web_chat_event(n.agent, n.session, canonical)
        member_result = serialize_session_event(native, audience="user")
        assert member_result["payload"]["outcome"] == "denied"
        assert member_result["payload"]["content"] == member_result["payload"]["result"] == (
            member_result["payload"]["model_visible_content"]) == "当前没有权限完成这次操作。"
        assert "member_summary" not in member_result["payload"]
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.foreign,
            resource_type="agent", resource_id=n.agent, actions=["operator.inspect"],
            conditions={"operator_inspection": {"schema": "hive.agent.operator_inspection.v1"}}))
        session = await db.get(ChatSession, n.session)
        session.runtime_task_id = n.run
        await db.commit()
        run = await db.get(RuntimeTask, n.run)
        views = await read_session_run_permissions(db, run=run, session=session, user=await db.get(User, n.user))
        assert views == []
        seed = SimpleNamespace(tenant_id=n.tenant, user_id=n.user, agent_id=n.agent,
                               session_id=n.session, run_id=n.run)
        async with _mounted_client(n.app_user_sessionmaker, seed) as client:
            path = f"/api/agents/{n.agent}/sessions/{n.session}"
            reload = await client.get(f"{path}/transcript", params={"schema_version": 2})
            messages = await client.get(f"{path}/messages")
            owner_operator = await client.get(f"{path}/messages", params={"operator_view": "true"})
            assert reload.status_code == messages.status_code == owner_operator.status_code == 200
            operator_seed = SimpleNamespace(**{**vars(seed), "user_id": n.foreign})
            async with _mounted_client(n.app_user_sessionmaker, operator_seed) as operator:
                params = {"operator_view": "true", "operator_reason": "Inspect the original mode denial"}
                operator_reload = await operator.get(f"{path}/transcript", params={**params, "schema_version": 2})
                operator_messages = await operator.get(f"{path}/messages", params=params)
                assert operator_reload.status_code == operator_messages.status_code == 200
            stored = list(await db.scalars(select(ChatMessage).where(
                ChatMessage.conversation_id == str(n.session)).order_by(ChatMessage.created_at)))
            assert operator_messages.json() == [serialize_chat_message(message) for message in stored]
            assert str(result) in json.dumps(operator_messages.json(), ensure_ascii=False)
            for key in ("capability", "permission_mode", "authorization_decision_entry", "model_visible_message"):
                assert key in json.dumps(operator_reload.json())
                assert key in json.dumps(operator_permission)
            assert operator_result["payload"]["content"] == operator_result["payload"]["model_visible_content"] == str(result)

            # Already user-marked frames from an older publisher must still
            # pass the same projection at the forwarding boundary.
            class Pubsub:
                async def subscribe(self, *_args):
                    pass

                async def listen(self):
                    for frame in (operator_permission, canonical):
                        yield {"type": "message", "data": json.dumps({
                            "schema": "hive.session_event.delivery", "projection_audience": "user",
                            "agent_id": str(n.agent), "session_id": str(n.session),
                            "event_id": frame["event_id"], "sequence": frame["sequence"],
                            "envelope": frame, "envelope_sha256": canonical_payload_hash(frame),
                        })}

            class Redis:
                def pubsub(self):
                    return Pubsub()

            async def redis():
                return Redis()

            monkeypatch.setattr(bus, "get_redis", redis)
            count = len(delivered)
            await bus._listen_web_chat_stream_once()
            assert len(delivered) == count + 2
            frames = [*delivered, member_result, serialize_session_event(legacy_row, audience="user"),
                      bus._user_live_projection(operator_permission), bus._user_live_projection(canonical),
                      reload.json(), messages.json(), owner_operator.json()]
            for frame in frames:
                _assert_member_frame(frame)
                encoded = json.dumps(frame, ensure_ascii=False)
                for private in ("authorization_decision_entry", "model_visible_message", str(result),
                                "ask the user to change the session permission mode"):
                    assert private not in encoded
                assert "当前没有权限完成这次操作。" in encoded
            assert serialize_session_event(legacy_row) == operator_before
            assert serialize_session_event(native) == operator_result
            assert canonical == original_canonical
            assert json.loads(legacy_row.content)["message"] == str(result)
            assert produced[0]["message"] == str(result)
            assert await db.scalar(select(PlazaPost).where(PlazaPost.author_id == n.agent)) is None

            legacy = await append_session_event(
                db=db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, user_id=n.user,
                actor_type="tool", event_type="tool_result", role="tool_call", t0_role="tool",
                content=json.dumps({"name": "plaza_create_post", "args": arguments, "status": "done", "result": str(result)}),
                metadata={"tool_name": "plaza_create_post", "status": "done"}, source="task_executor",
            )
            await db.commit()
            response = await client.get(f"{path}/messages")
            assert response.status_code == 200
            row = next(row for row in response.json() if row["id"] == str(legacy.chat_message.id))
            # X6c controller-approved known limit: legacy string rows lost the
            # typed denial fact. Preserve them; never classify their prose.
            assert row["toolResult"] == row["parts"][0]["result"] == str(result)
            assert json.loads(serialize_session_event(legacy.transcript_event, audience="user")["payload"]["content"])["result"] == str(result)
