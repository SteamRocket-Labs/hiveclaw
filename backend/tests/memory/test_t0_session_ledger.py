from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.memory.t0.ledger import (
    append_t0_session_event,
    import_legacy_t0_file,
    replay_t0_session_events,
    seal_t0_session_segment,
)


def test_append_user_and_assistant_messages_to_unified_session_ledger(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()

    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="请按 Claude Code 的 transcript 方式保存这一轮",
        message_id="msg-user-1",
        actor_id="user-1",
        source="web",
        data_root=tmp_path,
        created_at=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
    )
    second = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="assistant_message",
        role="assistant",
        content="已写入 append-only session ledger。",
        message_id="msg-assistant-1",
        actor_id=str(agent_id),
        source="web",
        data_root=tmp_path,
        created_at=datetime(2026, 6, 18, 10, 1, tzinfo=timezone.utc),
    )

    expected_root = tmp_path / str(agent_id) / "memory" / "t0" / "sessions" / str(session_id)
    assert first.path == second.path
    assert first.path == expected_root / "segments" / first.segment_id / "source.md"
    assert "logs" not in first.path.parts
    assert first.sequence == 1
    assert second.sequence == 2

    content = first.path.read_text(encoding="utf-8")
    assert f"agent_id: {agent_id}" in content
    assert f"session_id: {session_id}" in content
    assert "agent_id: memory" not in content
    assert '<t0_event id="' in content
    assert 'seq="1"' in content
    assert 'event_type="user_message"' in content
    assert "请按 Claude Code" in content
    assert "已写入 append-only session ledger" in content

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.sequence, event.event_type, event.role, event.content) for event in events] == [
        (1, "user_message", "user", "请按 Claude Code 的 transcript 方式保存这一轮"),
        (2, "assistant_message", "assistant", "已写入 append-only session ledger。"),
    ]


def test_seal_segment_preserves_append_only_history_and_next_turn_gets_new_segment(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()

    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="第一段",
        data_root=tmp_path,
    )

    sealed = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="session_idle",
        data_root=tmp_path,
    )
    second = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="恢复后的第二段",
        data_root=tmp_path,
    )

    assert sealed is not None
    assert sealed.segment_id == first.segment_id
    assert second.segment_id != first.segment_id
    assert first.path.exists()
    assert second.path.exists()

    first_content = first.path.read_text(encoding="utf-8")
    second_content = second.path.read_text(encoding="utf-8")
    assert "第一段" in first_content
    assert "恢复后的第二段" not in first_content
    assert "恢复后的第二段" in second_content
    assert 'event_type="segment_boundary"' in first_content

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.sequence, event.event_type, event.content) for event in events] == [
        (1, "user_message", "第一段"),
        (2, "segment_boundary", "session_idle"),
        (3, "user_message", "恢复后的第二段"),
    ]


def test_pl4_credential_is_masked_before_it_reaches_t0_source(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()
    secret = "sk-" + "A" * 24

    result = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content=f"请轮换 api_key={secret}",
        data_root=tmp_path,
    )

    content = result.path.read_text(encoding="utf-8")
    assert secret not in content
    assert "&lt;Credential_" in content
    event = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)[0]
    assert secret not in event.content
    assert "<Credential_" in event.content
    assert event.sensitivity == "PL4_credential"


def test_legacy_t0_file_import_is_idempotent_and_quarantined_under_session_ledger(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()
    legacy_path = tmp_path / str(agent_id) / "logs" / "2026-06-18" / "behavior" / "chat-1200-abcd.md"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        "---\ntype: chat\nsession_id: old-session\n---\n\n**User**: 旧日志\n**Agent**: 旧回复\n",
        encoding="utf-8",
    )

    first = import_legacy_t0_file(
        agent_id=agent_id,
        session_id=session_id,
        legacy_path=legacy_path,
        data_root=tmp_path,
    )
    second = import_legacy_t0_file(
        agent_id=agent_id,
        session_id=session_id,
        legacy_path=legacy_path,
        data_root=tmp_path,
    )

    assert first.segment_id == second.segment_id
    assert first.imported is True
    assert second.imported is False
    assert (
        first.path
        == tmp_path
        / str(agent_id)
        / "memory"
        / "t0"
        / "sessions"
        / str(session_id)
        / "segments"
        / first.segment_id
        / "source.md"
    )
    assert first.path.exists()
    assert legacy_path.exists(), "legacy import must never delete or rewrite the source file"

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert len([event for event in events if event.event_type == "legacy_import"]) == 1
    assert events[0].metadata["legacy_path"].endswith("logs/2026-06-18/behavior/chat-1200-abcd.md")
