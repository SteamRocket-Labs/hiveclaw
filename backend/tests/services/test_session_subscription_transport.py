from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.services.session_subscription import (
    SessionSubscriptionError,
    build_session_ready,
    load_session_catchup_window,
    parse_session_subscribe,
    resolve_subscription_cursor,
)
from app.services.web_chat_broker import SessionLiveBufferOverflow, WebChatBroker


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class _StalledSocket(_Socket):
    def __init__(self) -> None:
        super().__init__()
        self.closed_codes: list[int] = []

    async def send_json(self, payload: dict) -> None:
        await asyncio.Event().wait()

    async def close(self, *, code: int = 1000) -> None:
        self.closed_codes.append(code)


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


def test_live_tail_subscription_uses_server_watermark_without_replaying_unbounded_history() -> None:
    session_id = uuid4()
    request = parse_session_subscribe(
        {
            "type": "session.subscribe",
            "session_id": str(session_id),
            "after_sequence": 0,
            "cursor_mode": "live_tail",
            "schema_version": 2,
            "connection_attempt_id": "attempt-tail",
        },
        expected_session_id=session_id,
    )

    assert request.cursor_mode == "live_tail"
    assert resolve_subscription_cursor(request, last_committed_sequence=3200) == 3200

    resume = parse_session_subscribe(
        {
            "type": "session.subscribe",
            "session_id": str(session_id),
            "after_sequence": 41,
            "schema_version": 2,
            "connection_attempt_id": "attempt-resume",
        },
        expected_session_id=session_id,
    )
    assert resume.cursor_mode == "resume"
    assert resolve_subscription_cursor(resume, last_committed_sequence=3200) == 41


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
async def test_ranked_legacy_catchup_window_exposes_safe_delivery_watermark(monkeypatch) -> None:
    from app.services import session_subscription
    from app.services.session_delivery_cursor import resolve_session_delivery_cursor

    session_id = uuid4()
    storage_first = 1_777_000_000_000_000_000
    cursor = resolve_session_delivery_cursor(
        event_count=3,
        storage_first_sequence=storage_first,
        storage_last_sequence=storage_first + 90_000_000_000,
        first_event_schema_version=1,
        first_event_metadata={"source": "backfill_recent_chat_logs"},
    )

    async def fake_load_cursor(_db, *, session_id: object):
        return cursor

    monkeypatch.setattr(session_subscription, "load_session_delivery_cursor", fake_load_cursor)
    window = await load_session_catchup_window(object(), session_id=session_id, after_sequence=2)

    assert window.last_committed_sequence == 3
    assert window.last_committed_storage_sequence == storage_first + 90_000_000_000
    assert window.cursor is cursor

    ready = build_session_ready(
        session_id=session_id,
        connection_attempt_id="attempt-ranked",
        accepted_after_sequence=2,
        last_committed_sequence=3,
        active_run=None,
        sequence_projection=cursor.mode,
    )
    assert ready["accepted_after_sequence"] == 2
    assert ready["last_committed_sequence"] == 3
    assert ready["sequence_projection"] == cursor.mode


@pytest.mark.asyncio
async def test_unrecoverable_delivery_cursor_is_typed_and_not_retried(monkeypatch) -> None:
    from app.services import session_subscription
    from app.services.session_delivery_cursor import SessionDeliveryCursorError

    async def fail_cursor_load(_db, *, session_id: object):
        raise SessionDeliveryCursorError("session_delivery_cursor_unrecoverable")

    monkeypatch.setattr(session_subscription, "load_session_delivery_cursor", fail_cursor_load)

    with pytest.raises(SessionSubscriptionError) as raised:
        await load_session_catchup_window(object(), session_id=uuid4(), after_sequence=0)

    assert raised.value.code == "session_delivery_cursor_unrecoverable"
    assert raised.value.retryable is False


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
async def test_broker_keeps_ranked_legacy_catchup_and_live_delivery_on_one_safe_cursor() -> None:
    broker = WebChatBroker()
    socket = _Socket()
    storage_watermark = 1_777_000_000_090_000_000
    await broker.begin_session_subscription("agent-1", socket, "session-1")

    await broker.send_session_message(
        "agent-1",
        "session-1",
        {
            "schema": "hive.session_event",
            "schema_version": 2,
            "event_id": "event-4",
            "sequence": storage_watermark + 2,
        },
    )
    await broker.send_session_message(
        "agent-1",
        "session-1",
        {
            "schema": "hive.session_event",
            "schema_version": 2,
            "event_id": "event-3",
            "sequence": storage_watermark + 1,
        },
    )

    await broker.activate_session_subscription(
        socket,
        delivered_through_sequence=storage_watermark,
        delivered_through_delivery_sequence=2,
    )
    assert [frame["sequence"] for frame in socket.sent] == [3, 4]
    assert [frame["storage_sequence"] for frame in socket.sent] == [
        str(storage_watermark + 1),
        str(storage_watermark + 2),
    ]

    await broker.send_session_message(
        "agent-1",
        "session-1",
        {
            "schema": "hive.session_event",
            "schema_version": 2,
            "event_id": "event-5",
            "sequence": storage_watermark + 3,
        },
    )
    assert [frame["sequence"] for frame in socket.sent] == [3, 4, 5]
    assert socket.sent[-1]["storage_sequence"] == str(storage_watermark + 3)


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


@pytest.mark.asyncio
async def test_broker_bounds_stalled_socket_without_blocking_healthy_delivery() -> None:
    broker = WebChatBroker(send_timeout_seconds=0.02)
    healthy = _Socket()
    stalled = _StalledSocket()
    await broker.connect("agent-1", healthy, "session-1")
    await broker.connect("agent-1", stalled, "session-1")

    payload = {"type": "phase", "phase": "done", "run_id": "run-1"}
    await asyncio.wait_for(
        broker.send_session_message("agent-1", "session-1", payload),
        timeout=0.2,
    )

    assert healthy.sent == [payload]
    assert stalled.sent == []
    assert stalled.closed_codes == [1011]
    assert broker.active_connections["agent-1"] == [(healthy, "session-1")]


@pytest.mark.asyncio
async def test_terminal_phase_stall_cannot_outlive_claimed_runtime_work(monkeypatch) -> None:
    from app.runtime.runtime_phase import RunPhaseEmitter, RuntimePhase
    from app.services import runtime_task_fence as fence_service

    broker = WebChatBroker(send_timeout_seconds=0.02)
    stalled = _StalledSocket()
    await broker.connect("agent-1", stalled, "session-1")
    renewals: list[float] = []

    async def fake_renew(*, lease_seconds: float):
        renewals.append(lease_seconds)
        return datetime.now(timezone.utc)

    monkeypatch.setattr(fence_service, "renew_current_runtime_task_lease", fake_renew)
    emitter = RunPhaseEmitter(
        lambda event: broker.send_session_message("agent-1", "session-1", event),
        run_id="run-1",
    )

    async def terminal_cleanup() -> str:
        assert await emitter.transition(RuntimePhase.DONE) is True
        return "settled"

    result = await asyncio.wait_for(
        fence_service.run_claimed_runtime_task(
            terminal_cleanup(),
            task_id=uuid4(),
            claim_version=1,
            worker_id="worker-1",
            lease_seconds=0.3,
        ),
        timeout=0.2,
    )

    assert result == "settled"
    assert renewals == []
    assert stalled.closed_codes == [1011]


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


def test_live_endpoint_skips_history_replay_only_for_typed_live_tail_bootstrap() -> None:
    import inspect

    from app.api.websocket import websocket_chat

    source = inspect.getsource(websocket_chat)
    assert "resolve_subscription_cursor" in source
    assert 'subscription.cursor_mode == "resume"' in source
    assert "accepted_after_sequence=accepted_after_sequence" in source
    assert "delivered_through_sequence=catchup.last_committed_storage_sequence" in source
    assert "delivered_through_delivery_sequence=catchup.last_committed_sequence" in source
