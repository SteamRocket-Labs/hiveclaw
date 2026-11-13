"""X3 r2 — pins for the round-1 production fixes on the native V2 path.

The current web runtime writes tool results as native V2 rows whose payload
carries the raw result in content and (since X3) the typed tool_name beside
it, and the in-process broadcast must send the member projection for every
tool_result. These tests pin: the write-side tool_name and its exclusion from
content_hash, the broadcast projection, and the real model-input builder over
a settled native row.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.services.test_r6a_ordinary_tool_results import _settled_result

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

from uuid import uuid4

MARKER = "不要向用户展示"
MEMORY_BODY = (
    "## 读取到的记忆\n"
    f"管理引用（仅用于读取、更正或忘记，{MARKER}）：id=explicit_284ff1e71ca75b9f92800d2c6fc40ab9\n"
    "session-tool-invocation:4f10ca36-8bed-5bc0-88f2-ff509041ab08 scope=sessions"
)


def _native_envelope(name: str, result: str) -> dict:
    sid = str(uuid4())
    return {
        "schema": "hive.session_event",
        "schema_version": 2,
        "event_id": str(uuid4()),
        "sequence": 41,
        "tenant_id": str(uuid4()),
        "scope": {"level": "round", "session_id": sid, "thread_id": sid, "turn_id": str(uuid4()),
                  "run_id": "run-x3", "round_id": str(uuid4())},
        "item_id": str(uuid4()),
        "item_kind": "tool_result",
        "kind": "tool_result.completed",
        "lifecycle": "completed",
        "payload_schema": "hive.session.payload.tool_result.completed.v2",
        "actor": {"type": "tool"},
        "visibility": {"audience": "direct_user"},
        "payload": {
            "invocation_id": str(uuid4()),
            "tool_name": name,
            "outcome": "success",
            "retryable": False,
            "content": result,
            "content_hash": "h" * 8,
            "parts": [],
        },
        "occurred_at": "2026-11-13T10:00:00Z",
        "persisted_at": "2026-11-13T10:00:00Z",
        "run_id": "run-x3",
    }


@pytest.mark.asyncio
async def test_broadcast_sends_the_member_projection_for_native_tool_results(monkeypatch) -> None:
    from app.services import web_chat_runtime

    sent: list[dict] = []

    async def capture(_agent_id, _session_id, payload):
        sent.append(payload)

    monkeypatch.setattr(web_chat_runtime.web_chat_broker, "send_session_message", capture)
    envelope = _native_envelope("load_memory", MEMORY_BODY)

    await web_chat_runtime.broadcast_web_chat_event(
        uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        envelope,
    )

    assert len(sent) == 1
    delivered = sent[0]
    assert delivered["payload"]["member_summary"] == {"kind": "memory_read", "state": "done"}
    assert MARKER not in json.dumps(delivered, ensure_ascii=False)
    assert MEMORY_BODY not in json.dumps(delivered, ensure_ascii=False)


@pytest.mark.asyncio
async def test_broadcast_keeps_committed_bytes_for_non_tool_results(monkeypatch) -> None:
    from app.services import web_chat_runtime

    sent: list[dict] = []

    async def capture(_agent_id, _session_id, payload):
        sent.append(payload)

    monkeypatch.setattr(web_chat_runtime.web_chat_broker, "send_session_message", capture)
    envelope = _native_envelope("load_memory", MEMORY_BODY)
    envelope["item_kind"] = "assistant_text"
    envelope["kind"] = "assistant_text.delta"
    envelope["lifecycle"] = "delta"
    envelope["payload_schema"] = "hive.session.payload.assistant_text.delta.v2"
    envelope["payload"] = {"phase": "unknown", "content": "literal bytes"}

    await web_chat_runtime.broadcast_web_chat_event(
        uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        envelope,
    )

    # Every envelope that is not a tool_result keeps its committed bytes.
    assert sent == [envelope]


@pytest.mark.asyncio
async def test_complete_tool_invocation_writes_tool_name_outside_the_hash(owner_sessionmaker) -> None:
    # Ruling 7: the result payload carries the typed tool_name (mirrored from
    # the paired invocation) and content_hash still covers only the result
    # bytes — the name reaches neither the model input nor any source hash.
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_tool_runtime import _sha256

    content = 'print("memory receipt")\n' * 40
    tenant, _user, _agent, _session, invocation_id = await _settled_result(owner_sessionmaker, "read_file", content)
    async with owner_sessionmaker() as db:
        event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.invocation_id == invocation_id,
            ChatTranscriptEvent.event_type == "tool_result.completed",
        ))
        payload = event.metadata_json["v2_payload"]
        assert payload["tool_name"] == "read_file"
        assert payload["content_hash"] == _sha256(content)


@pytest.mark.asyncio
async def test_model_input_builder_carries_raw_bytes_despite_user_projection(owner_sessionmaker) -> None:
    # Ruling 7: pin the real model-input path. After a user serialization of
    # the settled native row, session replay still feeds the model the exact
    # durable bytes — the view-time projection never touches model input.
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_event_contract import serialize_session_event
    from app.services.session_semantic_history import load_session_semantic_history

    tenant, user, agent, session, invocation_id = await _settled_result(owner_sessionmaker, "load_memory", MEMORY_BODY)
    async with owner_sessionmaker() as db:
        event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.invocation_id == invocation_id,
            ChatTranscriptEvent.event_type == "tool_result.completed",
        ))
        row = SimpleNamespace(
            id=event.id, sequence=event.sequence, tenant_id=event.tenant_id, schema_version=2,
            scope_json=event.scope_json, item_id=event.item_id or str(event.id), item_kind=event.item_kind,
            event_type=event.event_type, lifecycle=event.lifecycle, payload_schema=event.payload_schema,
            actor_type=event.actor_type, visibility_scope=event.visibility_scope, run_id=event.run_id,
            message_id=event.message_id, invocation_id=event.invocation_id,
            provider_tool_use_id=getattr(event, "provider_tool_use_id", None),
            content_hash=getattr(event, "content_hash", None), created_at=str(event.created_at),
            metadata_json=event.metadata_json,
        )
        user_view = serialize_session_event(row, audience="user")["payload"]
        assert user_view["member_summary"] is not None
        assert user_view["content"] == ""
        history = await load_session_semantic_history(db, tenant_id=tenant, agent_id=agent, session_id=session)
        tool_messages = [message for message in history.messages if message.role == "tool"]
        assert tool_messages, "fixture_did_not_produce_tool_replay"
        # The replayed bytes equal the durable row content exactly: the model
        # still receives the marker/id bytes the member view removed.
        durable_content = event.metadata_json["v2_payload"]["content"]
        assert durable_content == MEMORY_BODY
        assert all(str(message.content) == durable_content for message in tool_messages)
