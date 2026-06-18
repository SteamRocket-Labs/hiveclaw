from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.memory.t0.ledger import replay_t0_session_events
from app.runtime.hooks import HookContext, HookEvent
from app.runtime.hooks_setup import (
    _t0_delegation_end,
    _t0_dream_end,
    _t0_heartbeat_tick_end,
    _t0_trigger_end,
)


def _patch_t0_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake = lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path))  # noqa: E731
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", fake)
    monkeypatch.setattr("app.services.t0_logger.get_settings", fake)


@pytest.mark.asyncio
async def test_trigger_end_writes_t0_session_ledger_not_legacy_logs(monkeypatch, tmp_path) -> None:
    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()

    await _t0_trigger_end(
        HookContext(
            event=HookEvent.TRIGGER_END,
            agent_id=str(agent_id),
            session_id="trigger-session-1",
            source="trigger",
            messages=[{"role": "assistant", "content": "trigger completed"}],
            metadata={"trigger_id": "daily-brief", "status": "success"},
        )
    )

    events = replay_t0_session_events(agent_id=agent_id, session_id="trigger-session-1", data_root=tmp_path)
    assert [(event.event_type, event.role, event.content) for event in events] == [
        ("trigger_run", "system", "trigger completed"),
        ("segment_boundary", "system", "trigger_end"),
    ]
    assert events[0].metadata["trigger_id"] == "daily-brief"
    assert not list((tmp_path / str(agent_id) / "logs").glob("**/*.md"))


@pytest.mark.asyncio
async def test_delegation_end_writes_t0_session_ledger_not_legacy_logs(monkeypatch, tmp_path) -> None:
    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()

    await _t0_delegation_end(
        HookContext(
            event=HookEvent.DELEGATION_END,
            agent_id=str(agent_id),
            session_id="delegation-session-1",
            source="delegation",
            messages=[{"role": "assistant", "content": "handoff result"}],
            metadata={"delegation_id": "handoff-1", "to_agent": "researcher"},
        )
    )

    events = replay_t0_session_events(agent_id=agent_id, session_id="delegation-session-1", data_root=tmp_path)
    assert [(event.event_type, event.role, event.content) for event in events] == [
        ("delegation_run", "system", "handoff result"),
        ("segment_boundary", "system", "delegation_end"),
    ]
    assert events[0].metadata["delegation_id"] == "handoff-1"
    assert not list((tmp_path / str(agent_id) / "logs").glob("**/*.md"))


@pytest.mark.asyncio
async def test_heartbeat_and_dream_write_t0_session_ledger_not_legacy_logs(monkeypatch, tmp_path) -> None:
    _patch_t0_root(monkeypatch, tmp_path)
    agent_id = uuid4()
    reset_calls = []

    def fake_reset_heartbeat_session(reset_agent_id):
        reset_calls.append(reset_agent_id)

    monkeypatch.setattr("app.services.heartbeat._reset_heartbeat_session", fake_reset_heartbeat_session)

    await _t0_heartbeat_tick_end(
        HookContext(
            event=HookEvent.HEARTBEAT_TICK_END,
            agent_id=str(agent_id),
            session_id="heartbeat-session-1",
            source="heartbeat",
            messages=[],
            metadata={"tick": 3, "action": "none"},
        )
    )
    await _t0_dream_end(
        HookContext(
            event=HookEvent.DREAM_END,
            agent_id=str(agent_id),
            session_id="dream-session-1",
            source="dream",
            messages=[],
            metadata={"t3_processed": 5, "promoted_to_soul": 1},
        )
    )

    heartbeat_events = replay_t0_session_events(agent_id=agent_id, session_id="heartbeat-session-1", data_root=tmp_path)
    dream_events = replay_t0_session_events(agent_id=agent_id, session_id="dream-session-1", data_root=tmp_path)
    assert [(event.event_type, event.role, event.content) for event in heartbeat_events] == [
        ("heartbeat_tick", "system", "heartbeat_tick_end"),
        ("segment_boundary", "system", "heartbeat_tick_end"),
    ]
    assert [(event.event_type, event.role, event.content) for event in dream_events] == [
        ("dream_run", "system", "dream_end"),
        ("segment_boundary", "system", "dream_end"),
    ]
    assert reset_calls == [agent_id]
    assert not list((tmp_path / str(agent_id) / "logs").glob("**/*.md"))
