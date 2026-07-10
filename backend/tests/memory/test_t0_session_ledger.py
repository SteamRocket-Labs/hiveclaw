from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.memory.t0.ledger import (
    EVENT_RECORD_SCHEMA_VERSION,
    EVENTS_FILENAME,
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
    assert first.jsonl_path == expected_root / "segments" / first.segment_id / EVENTS_FILENAME
    assert second.jsonl_path == first.jsonl_path
    assert "logs" not in first.path.parts
    assert first.sequence == 1
    assert second.sequence == 2

    records = [json.loads(line) for line in first.jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert [record["schema_version"] for record in records] == [
        EVENT_RECORD_SCHEMA_VERSION,
        EVENT_RECORD_SCHEMA_VERSION,
    ]
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[0]["content"] == "请按 Claude Code 的 transcript 方式保存这一轮"
    assert records[0]["projection"]["path"] == f"segments/{first.segment_id}/source.md"
    assert records[0]["mechanical_truth"]["format"] == "jsonl"
    assert records[0]["event_hash"]
    assert records[1]["prev_event_hash"] == records[0]["event_hash"]

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
    assert [event.record_schema_version for event in events] == [
        EVENT_RECORD_SCHEMA_VERSION,
        EVENT_RECORD_SCHEMA_VERSION,
    ]
    assert [event.truth_path for event in events] == [first.jsonl_path, first.jsonl_path]
    assert [event.path for event in events] == [first.path, first.path]
    assert events[0].event_hash == records[0]["event_hash"]
    assert events[1].prev_event_hash == records[0]["event_hash"]


def test_t0_event_promotes_runtime_metadata_to_mechanical_record_and_projection(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()

    result = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="runtime metadata discipline",
        runtime_task_id="runtime-1",
        source="web_chat",
        metadata={"turn_id": "turn-1", "intent_id": "intent-1", "request_id": "request-1"},
        data_root=tmp_path,
    )

    record = json.loads(result.jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["sequence"] == 1
    assert record["source"] == "web_chat"
    assert record["runtime_task_id"] == "runtime-1"
    assert record["turn_id"] == "turn-1"
    assert record["intent_id"] == "intent-1"

    projection = result.path.read_text(encoding="utf-8")
    assert 'turn_id="turn-1"' in projection
    assert 'intent_id="intent-1"' in projection
    assert 'runtime_task_id="runtime-1"' in projection
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events[0].turn_id == "turn-1"
    assert events[0].intent_id == "intent-1"
    assert events[0].runtime_task_id == "runtime-1"
    assert events[0].source == "web_chat"
    assert events[0].sequence == 1


def test_jsonl_append_uses_o_append_and_single_write(monkeypatch, tmp_path: Path) -> None:
    import app.memory.t0.ledger as ledger

    opened: list[tuple[str, int, int]] = []
    writes: list[bytes] = []
    real_open = os.open

    def fake_open(path, flags, mode=0o777):
        opened.append((str(path), flags, mode))
        return real_open(tmp_path / "actual.jsonl", flags, mode)

    real_write = os.write

    def fake_write(fd, data):
        writes.append(data)
        return real_write(fd, data)

    monkeypatch.setattr(ledger.os, "open", fake_open)
    monkeypatch.setattr(ledger.os, "write", fake_write)

    offset, length = ledger._append_event_record(tmp_path / "events.jsonl", {"sequence": 1, "content": "hello"})

    assert opened
    assert opened[0][1] & os.O_APPEND
    assert opened[0][1] & os.O_CREAT
    assert len(writes) == 1
    assert writes[0].endswith(b"\n")
    assert length == len(writes[0])
    assert offset == 0


def test_replay_falls_back_to_markdown_projection_for_legacy_segments(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()

    result = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="legacy projection still replays",
        data_root=tmp_path,
    )
    result.jsonl_path.unlink()

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)

    assert [(event.sequence, event.event_type, event.content) for event in events] == [
        (1, "user_message", "legacy projection still replays")
    ]
    assert events[0].record_schema_version == "t0.markdown-projection.v1"
    assert events[0].truth_path == result.path


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


def test_tail_replay_returns_latest_window_ascending(tmp_path: Path) -> None:
    from app.memory.t0.ledger import replay_t0_session_events_tail

    agent_id = uuid4()
    session_id = uuid4()
    for i in range(1, 16):
        append_t0_session_event(
            agent_id=agent_id,
            session_id=session_id,
            event_type="user_message",
            role="user",
            content=f"m{i}",
            data_root=tmp_path,
        )
    seal_t0_session_segment(agent_id=agent_id, session_id=session_id, reason="session_idle", data_root=tmp_path)
    for i in range(17, 31):
        append_t0_session_event(
            agent_id=agent_id,
            session_id=session_id,
            event_type="user_message",
            role="user",
            content=f"m{i}",
            data_root=tmp_path,
        )

    events = replay_t0_session_events_tail(agent_id=agent_id, session_id=session_id, limit=10, data_root=tmp_path)

    assert [event.sequence for event in events] == list(range(21, 31))


def test_tail_replay_returns_everything_when_limit_exceeds_total(tmp_path: Path) -> None:
    from app.memory.t0.ledger import replay_t0_session_events, replay_t0_session_events_tail

    agent_id = uuid4()
    session_id = uuid4()
    for i in range(1, 6):
        append_t0_session_event(
            agent_id=agent_id,
            session_id=session_id,
            event_type="user_message",
            role="user",
            content=f"m{i}",
            data_root=tmp_path,
        )

    tail = replay_t0_session_events_tail(agent_id=agent_id, session_id=session_id, limit=100, data_root=tmp_path)
    full = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)

    assert [event.sequence for event in tail] == [event.sequence for event in full]


def test_tail_replay_skips_older_segments_when_tail_is_enough(monkeypatch, tmp_path: Path) -> None:
    import app.memory.t0.ledger as ledger_module
    from app.memory.t0.ledger import replay_t0_session_events_tail

    agent_id = uuid4()
    session_id = uuid4()
    for segment in range(3):
        for i in range(10):
            append_t0_session_event(
                agent_id=agent_id,
                session_id=session_id,
                event_type="user_message",
                role="user",
                content=f"seg{segment}-m{i}",
                data_root=tmp_path,
            )
        if segment < 2:
            seal_t0_session_segment(agent_id=agent_id, session_id=session_id, reason="session_idle", data_root=tmp_path)

    parse_calls: list[str] = []
    real_parse = ledger_module._parse_events_from_jsonl

    def counting_parse(*, path, segment_id, source_path):
        parse_calls.append(segment_id)
        return real_parse(path=path, segment_id=segment_id, source_path=source_path)

    monkeypatch.setattr(ledger_module, "_parse_events_from_jsonl", counting_parse)

    events = replay_t0_session_events_tail(agent_id=agent_id, session_id=session_id, limit=5, data_root=tmp_path)

    assert len(events) == 5
    assert len(parse_calls) == 1, f"expected only the newest segment to be parsed, parsed: {parse_calls}"


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
