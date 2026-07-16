from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.session_subscription import (
    SessionSubscriptionError,
    build_session_ready,
    parse_session_subscribe,
)
from app.services.web_chat_broker import SessionLiveBufferOverflow, WebChatBroker


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def test_subscribe_contract_requires_exact_session_schema_cursor_and_attempt() -> None:
    session_id = uuid4()
    request = parse_session_subscribe(
        {
            "type": "session.subscribe",
            "session_id": str(session_id),
            "after_sequence": 41,
            "schema_version": 2,
            "connection_attempt_id": "attempt-1",
        },
        expected_session_id=session_id,
    )
    assert request.after_sequence == 41
    assert request.connection_attempt_id == "attempt-1"

    with pytest.raises(SessionSubscriptionError) as unsupported:
        parse_session_subscribe(
            {
                "type": "session.subscribe",
                "session_id": str(session_id),
                "after_sequence": 41,
                "schema_version": 1,
                "connection_attempt_id": "attempt-1",
            },
            expected_session_id=session_id,
        )
    assert unsupported.value.code == "schema_unsupported"


def test_ready_binds_watermark_and_projection_hint_without_model_dependency() -> None:
    session_id = uuid4()
    ready = build_session_ready(
        session_id=session_id,
        connection_attempt_id="attempt-2",
        accepted_after_sequence=7,
        last_committed_sequence=11,
        active_run={"run_id": "run-1", "turn_id": "turn-1", "status": "running"},
    )
    assert ready == {
        "type": "session.ready",
        "session_id": str(session_id),
        "subscription_id": ready["subscription_id"],
        "connection_attempt_id": "attempt-2",
        "accepted_after_sequence": 7,
        "last_committed_sequence": 11,
        "active_turn_id": "turn-1",
        "active_run_id": "run-1",
        "run_status": "running",
        "schema_version": 2,
    }


@pytest.mark.asyncio
async def test_broker_buffers_live_until_catchup_then_drains_in_sequence_order() -> None:
    broker = WebChatBroker()
    socket = _Socket()
    await broker.begin_session_subscription("agent-1", socket, "session-1")

    await broker.send_session_message(
        "agent-1", "session-1", {"schema": "hive.session_event", "event_id": "e13", "sequence": 13}
    )
    await broker.send_session_message(
        "agent-1", "session-1", {"schema": "hive.session_event", "event_id": "e12", "sequence": 12}
    )
    await broker.send_session_message(
        "agent-1", "session-1", {"schema": "hive.session_event", "event_id": "e11", "sequence": 11}
    )
    assert socket.sent == []

    await broker.activate_session_subscription(socket, delivered_through_sequence=11)
    assert [frame["sequence"] for frame in socket.sent] == [12, 13]

    await broker.send_session_message(
        "agent-1", "session-1", {"schema": "hive.session_event", "event_id": "e14", "sequence": 14}
    )
    assert [frame["sequence"] for frame in socket.sent] == [12, 13, 14]


@pytest.mark.asyncio
async def test_live_buffer_overflow_is_typed_and_recovers_from_durable_cursor_instead_of_silent_drop() -> None:
    broker = WebChatBroker(live_buffer_limit=2)
    socket = _Socket()
    await broker.begin_session_subscription("agent-1", socket, "session-1")
    for sequence in (1, 2, 3):
        await broker.send_session_message(
            "agent-1",
            "session-1",
            {"schema": "hive.session_event", "event_id": f"e{sequence}", "sequence": sequence},
        )

    with pytest.raises(SessionLiveBufferOverflow):
        await broker.activate_session_subscription(socket, delivered_through_sequence=0)
    assert socket.sent == []


@pytest.mark.asyncio
async def test_broker_preserves_same_sequence_conflicts_for_client_consistency_detection() -> None:
    broker = WebChatBroker()
    socket = _Socket()
    await broker.begin_session_subscription("agent-1", socket, "session-1")
    await broker.send_session_message(
        "agent-1", "session-1", {"schema": "hive.session_event", "event_id": "event-a", "sequence": 2}
    )
    await broker.send_session_message(
        "agent-1", "session-1", {"schema": "hive.session_event", "event_id": "event-b", "sequence": 2}
    )

    await broker.activate_session_subscription(socket, delivered_through_sequence=1)
    assert [frame["event_id"] for frame in socket.sent] == ["event-a", "event-b"]


def test_live_endpoint_registers_buffer_before_watermark_and_activates_after_catchup() -> None:
    import inspect

    from app.api.websocket import websocket_chat

    source = inspect.getsource(websocket_chat)
    begin = source.index("await manager.begin_session_subscription")
    watermark = source.index("catchup = await load_session_catchup_window")
    assert begin < watermark
    assert watermark < source.index("send_json(ready)")
    catchup_loop = source.index("async for event in iter_session_catchup_events")
    assert source.index("send_json(ready)") < catchup_loop
    assert catchup_loop < source.index("activate_session_subscription")
    assert "select(LLMModel)" not in source
