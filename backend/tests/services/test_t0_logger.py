"""Tests for T0 Raw Behavior Logger."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.t0_logger import (
    _category_of,
    _format_chat_log,
    _format_delegation_log,
    _format_dream_log,
    _format_heartbeat_log,
    _format_trigger_log,
    _generate_filename,
    _truncate,
    _yaml_frontmatter,
    audit_t0_logs,
    backfill_missing_chat_transcript_t0,
    backfill_recent_chat_logs,
    cleanup_old_logs,
    migrate_t0_layout,
    write_t0_log,
)


# ── Helpers ──


@pytest.fixture
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def tmp_agent_dir(tmp_path: Path, agent_id: uuid.UUID) -> Path:
    """Create a temporary agent data dir with logs/."""
    agent_dir = tmp_path / str(agent_id)
    (agent_dir / "logs").mkdir(parents=True)
    return tmp_path


# ── _yaml_frontmatter ──


class TestYamlFrontmatter:
    def test_basic_fields(self) -> None:
        result = _yaml_frontmatter({"type": "chat", "turns": 5})
        assert result.startswith("---")
        assert result.endswith("---")
        assert "type: chat" in result
        assert "turns: 5" in result

    def test_list_field(self) -> None:
        result = _yaml_frontmatter({"tools": ["search", "write"]})
        assert "tools: [search, write]" in result

    def test_empty_list(self) -> None:
        result = _yaml_frontmatter({"tools": []})
        assert "tools: []" in result

    def test_datetime_field(self) -> None:
        dt = datetime(2026, 4, 5, 14, 30, tzinfo=timezone.utc)
        result = _yaml_frontmatter({"started": dt})
        assert "started: 2026-04-05T14:30:00+00:00" in result

    def test_bool_field(self) -> None:
        result = _yaml_frontmatter({"active": True, "stale": False})
        assert "active: true" in result
        assert "stale: false" in result


# ── _generate_filename ──


class TestGenerateFilename:
    def test_format(self) -> None:
        name = _generate_filename("chat", "a1b2")
        assert name.startswith("chat-")
        assert name.endswith("-a1b2.md")
        # HHmm part should be 4 digits
        parts = name.split("-")
        assert len(parts[1]) == 4
        assert parts[1].isdigit()

    def test_auto_short_id(self) -> None:
        name = _generate_filename("trigger")
        assert name.startswith("trigger-")
        assert name.endswith(".md")
        # Should have auto-generated short_id (4 hex chars)
        parts = name.rsplit("-", 1)
        short_id = parts[1].replace(".md", "")
        assert len(short_id) == 4


# ── _truncate ──


class TestTruncate:
    def test_short_text(self) -> None:
        assert _truncate("hello", 100) == "hello"

    def test_exact_length(self) -> None:
        assert _truncate("12345", 5) == "12345"

    def test_truncation(self) -> None:
        result = _truncate("hello world", 5)
        assert result == "hello…"
        assert len(result) == 6  # 5 + ellipsis


# ── Format functions ──


class TestFormatChatLog:
    def test_basic_chat(self) -> None:
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = _format_chat_log(messages, {"source": "web", "session_id": "abc-123", "user_name": "Rocky"})
        assert "type: chat" in result
        assert "source: web" in result
        assert "user: Rocky" in result
        assert "turns: 1" in result
        assert "## Turn 1" in result
        assert "**User**: Hello" in result
        assert "**Agent**: Hi there!" in result

    def test_multi_part_content(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "image question"}, {"type": "image_url"}]},
        ]
        result = _format_chat_log(messages, {})
        assert "image question" in result

    def test_tool_calls(self) -> None:
        messages = [
            {"role": "user", "content": "search for X"},
            {
                "role": "assistant",
                "content": "Searching...",
                "tool_calls": [{"function": {"name": "web_search", "arguments": '{"q":"X"}'}}],
            },
        ]
        result = _format_chat_log(messages, {})
        assert "tools: [web_search]" in result
        assert "`web_search(" in result

    def test_empty_messages(self) -> None:
        result = _format_chat_log([], {})
        assert "type: chat" in result
        assert "turns: 0" in result


class TestFormatTriggerLog:
    def test_basic_trigger(self) -> None:
        result = _format_trigger_log(
            [{"role": "assistant", "content": "Done"}],
            {
                "trigger_name": "daily-standup",
                "trigger_type": "cron",
                "status": "success",
                "duration_ms": 5000,
                "instruction": "Check tasks",
                "result": "3 tasks updated",
            },
        )
        assert "type: trigger" in result
        assert "trigger_name: daily-standup" in result
        assert "status: success" in result
        assert "## Instruction" in result
        assert "Check tasks" in result
        assert "## Result" in result
        assert "3 tasks updated" in result


class TestFormatDelegationLog:
    def test_basic_delegation(self) -> None:
        result = _format_delegation_log(
            [{"role": "assistant", "content": "Researched"}],
            {
                "from_agent": "PM",
                "to_agent": "Researcher",
                "task": "Find competitors",
                "status": "success",
                "result": "Found 5",
            },
        )
        assert "type: delegation" in result
        assert "from: PM" in result
        assert "to: Researcher" in result
        assert "Find competitors" in result


class TestFormatHeartbeatLog:
    def test_basic_heartbeat(self) -> None:
        result = _format_heartbeat_log(
            [],
            {
                "tick": 3,
                "new_t2": 2,
                "distilled": 1,
                "score": 7,
                "new_t2_entries": ["feedback: user likes X"],
                "action": "none",
            },
        )
        assert "type: heartbeat" in result
        assert "tick: 3" in result
        assert "new_t2: 2" in result
        assert "feedback: user likes X" in result


class TestFormatDreamLog:
    def test_basic_dream(self) -> None:
        result = _format_dream_log(
            [],
            {
                "t3_processed": 5,
                "deduped": 3,
                "promoted_to_soul": 1,
                "dedup_summary": "Merged 3 feedback entries",
                "soul_candidate": {
                    "target": "soul.md",
                    "source_refs": ["t3:memory/t3/worker.md#quality"],
                    "review": {"recommendation": "promote"},
                },
                "cleanup_summary": "T2 truncated to 10",
            },
        )
        assert "type: dream" in result
        assert "t3_processed: 5" in result
        assert "Merged 3 feedback entries" in result
        assert "## Soul Candidate" in result
        assert "t3:memory/t3/worker.md#quality" in result


# ── write_t0_log ──


class TestWriteT0Log:
    def test_writes_chat_file(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            path = write_t0_log(
                agent_id,
                behavior_type="chat",
                messages=[{"role": "user", "content": "Hi"}],
                metadata={"source": "web", "session_id": "test-sess"},
            )
        assert path is not None
        assert path.exists()
        assert path.name.startswith("chat-")
        content = path.read_text()
        assert "type: chat" in content

    def test_unknown_type_returns_none(self, agent_id: uuid.UUID) -> None:
        result = write_t0_log(agent_id, behavior_type="unknown_type")
        assert result is None

    def test_writes_to_date_directory(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            path = write_t0_log(agent_id, behavior_type="trigger", metadata={"trigger_name": "test"})
        assert path is not None
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today in str(path)

    def test_all_five_types(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            for btype in ["chat", "trigger", "delegation", "heartbeat", "dream"]:
                path = write_t0_log(agent_id, behavior_type=btype)
                assert path is not None, f"Failed for type: {btype}"
                assert path.exists()


# ── cleanup_old_logs ──


class TestCleanupOldLogs:
    def test_removes_old_directories(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        logs_dir = tmp_agent_dir / str(agent_id) / "logs"
        # Create old directory (40 days ago)
        old_date = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
        (logs_dir / old_date).mkdir()
        (logs_dir / old_date / "chat-1200-abcd.md").write_text("old log")
        # Create recent directory
        recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (logs_dir / recent_date).mkdir()
        (logs_dir / recent_date / "chat-1200-efgh.md").write_text("new log")

        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            removed = cleanup_old_logs(agent_id, retention_days=30)

        assert removed == 1
        assert not (logs_dir / old_date).exists()
        assert (logs_dir / recent_date).exists()

    def test_no_logs_dir(self, tmp_agent_dir: Path) -> None:
        fake_id = uuid.uuid4()
        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            removed = cleanup_old_logs(fake_id, retention_days=30)
        assert removed == 0

    def test_keeps_recent(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        logs_dir = tmp_agent_dir / str(agent_id) / "logs"
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (logs_dir / recent).mkdir()

        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            removed = cleanup_old_logs(agent_id, retention_days=30)
        assert removed == 0
        assert (logs_dir / recent).exists()


class TestAuditT0Logs:
    def test_detects_empty_date_directories(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        logs_dir = tmp_agent_dir / str(agent_id) / "logs"
        empty_day = logs_dir / "2026-04-01"
        full_day = logs_dir / "2026-04-02"
        empty_day.mkdir()
        full_day.mkdir()
        (full_day / "chat-0800-abcd.md").write_text("log", encoding="utf-8")

        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            report = audit_t0_logs(agent_id, recent_days=30)

        assert report["date_dirs"] == 2
        assert report["empty_date_dirs"] == 1
        assert report["total_files"] == 1
        assert report["healthy"] is True

    def test_counts_recent_files(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        logs_dir = tmp_agent_dir / str(agent_id) / "logs"
        recent_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stale_day = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
        (logs_dir / recent_day).mkdir()
        (logs_dir / recent_day / "chat-0800-abcd.md").write_text("recent", encoding="utf-8")
        (logs_dir / stale_day).mkdir()
        (logs_dir / stale_day / "chat-0800-efgh.md").write_text("old", encoding="utf-8")

        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            report = audit_t0_logs(agent_id, recent_days=30)

        assert report["total_files"] == 2
        assert report["recent_files"] == 1
        assert report["healthy"] is True


class _BackfillResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _BackfillSession:
    def __init__(self, session_rows, message_rows):
        self._session_rows = session_rows
        self._message_rows = message_rows
        self._calls = 0
        self.added = []
        self.flushed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        self._calls += 1
        stmt_text = str(_stmt)
        if "chat_sessions" in stmt_text:
            return _BackfillResult(self._session_rows)
        if "chat_transcript_events" in stmt_text:
            return _BackfillResult([])
        return _BackfillResult(self._message_rows)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed += 1


class _BackfillEnumerationResult:
    def __init__(self, rows=None, scalar_value=None):
        self._rows = list(rows or [])
        self._scalar_value = scalar_value

    def all(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar_value


class _BackfillEnumerationSession:
    def __init__(self, rows):
        self._rows = list(rows)
        self.executed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        self.executed += 1
        stmt_text = str(_stmt)
        if "chat_transcript_events" in stmt_text:
            return _BackfillEnumerationResult(scalar_value=0)
        return _BackfillEnumerationResult(rows=self._rows)


@pytest.mark.asyncio
async def test_backfill_recent_chat_logs_creates_t0_files(
    agent_id: uuid.UUID, tmp_agent_dir: Path, monkeypatch
) -> None:
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.chat_transcript_event import ChatTranscriptEvent

    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session = type(
        "SessionRow",
        (),
        {
            "id": session_id,
            "source_channel": "web",
            "created_at": datetime(2026, 4, 9, 8, 0, tzinfo=timezone.utc),
            "last_message_at": datetime(2026, 4, 9, 8, 5, tzinfo=timezone.utc),
            "summary": "讨论 md-first 架构",
            "user_id": user_id,
        },
    )()
    messages = [
        type(
            "MessageRow",
            (),
            {
                "role": "user",
                "content": "请你把记忆系统收成 md-first",
                "created_at": datetime(2026, 4, 9, 8, 1, tzinfo=timezone.utc),
                "conversation_id": "web",
                "id": uuid.uuid4(),
            },
        )(),
        type(
            "MessageRow",
            (),
            {
                "role": "assistant",
                "content": "我会先从 session recall 和 T0 审计开始。",
                "created_at": datetime(2026, 4, 9, 8, 2, tzinfo=timezone.utc),
                "conversation_id": "web",
                "id": uuid.uuid4(),
            },
        )(),
    ]
    fake_db = _BackfillSession([session], messages)

    monkeypatch.setattr(
        "app.services.t0_logger.tenant_scoped_session", lambda *a, **k: fake_db
    )
    with patch("app.services.t0_logger.get_settings") as mock_settings:
        mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
        report = await backfill_recent_chat_logs(agent_id, recent_days=30, limit_sessions=10)

    logs_root = tmp_agent_dir / str(agent_id) / "logs"
    files = list((tmp_agent_dir / str(agent_id) / "memory" / "t0").rglob("source.md"))

    assert report["sessions_scanned"] == 1
    assert report["written"] == 1
    assert len(files) == 1
    assert not list(logs_root.rglob("chat-*.md"))
    transcript_events = [item for item in fake_db.added if isinstance(item, ChatTranscriptEvent)]
    assert [(event.event_type, event.role if hasattr(event, "role") else event.actor_type) for event in transcript_events] == [
        ("user_message", "user"),
        ("assistant_message", "assistant"),
    ]
    assert transcript_events[0].message_id == messages[0].id
    assert transcript_events[0].created_at == messages[0].created_at
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_agent_dir)
    assert [(event.event_type, event.role, event.content) for event in events] == [
        ("user_message", "user", "请你把记忆系统收成 md-first"),
        ("assistant_message", "assistant", "我会先从 session recall 和 T0 审计开始。"),
        ("segment_boundary", "system", "backfill_recent_chat_logs"),
    ]


@pytest.mark.asyncio
async def test_backfill_missing_chat_transcript_t0_groups_by_tenant_and_session(
    agent_id: uuid.UUID, tmp_agent_dir: Path, monkeypatch
) -> None:
    from app.services import t0_logger as module

    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    fake_db = _BackfillEnumerationSession([(session_id, agent_id, tenant_id)])
    calls = []

    class _Bypass:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_backfill_recent_chat_logs(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return {
            "sessions_scanned": len(kwargs["session_ids"]),
            "written": len(kwargs["session_ids"]),
            "skipped_existing": 0,
            "skipped_empty": 0,
            "transcript_events_written": 2,
            "t0_events_written": 2,
        }

    monkeypatch.setattr(module, "async_session", lambda: fake_db)
    monkeypatch.setattr(module, "enter_rls_bypass", lambda *a, **k: _Bypass())
    monkeypatch.setattr(module, "replay_t0_session_events", lambda **kwargs: [])
    monkeypatch.setattr(module, "backfill_recent_chat_logs", fake_backfill_recent_chat_logs)
    with patch("app.services.t0_logger.get_settings") as mock_settings:
        mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
        report = await backfill_missing_chat_transcript_t0(recent_days=3650, max_sessions=50, batch_size=2)

    assert fake_db.executed >= 2
    assert report["candidate_sessions_scanned"] == 1
    assert report["sessions_needing_backfill"] == 1
    assert report["groups_processed"] == 1
    assert report["written"] == 1
    assert calls == [
        {
            "args": (agent_id,),
            "kwargs": {
                "recent_days": 3650,
                "limit_sessions": 1,
                "tenant_id": tenant_id,
                "session_ids": [session_id],
            },
        }
    ]


# ── PR-1: behavior/ vs system/ split ──


class TestCategoryMapping:
    def test_behavior_types_map_to_behavior(self) -> None:
        assert _category_of("chat") == "behavior"
        assert _category_of("trigger") == "behavior"
        assert _category_of("delegation") == "behavior"

    def test_system_types_map_to_system(self) -> None:
        assert _category_of("heartbeat") == "system"
        assert _category_of("dream") == "system"

    def test_unknown_type_falls_back_to_system(self) -> None:
        assert _category_of("totally_new_kind") == "system"


class TestWriteT0LogSubdirs:
    def _patch_settings(self, tmp_agent_dir: Path):
        return patch(
            "app.services.t0_logger.get_settings",
            return_value=type("S", (), {"AGENT_DATA_DIR": str(tmp_agent_dir)})(),
        )

    def test_chat_lands_in_behavior_subdir(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with self._patch_settings(tmp_agent_dir):
            path = write_t0_log(
                agent_id,
                behavior_type="chat",
                messages=[{"role": "user", "content": "hi"}],
                metadata={"session_id": "s1"},
            )
        assert path is not None
        assert path.parent.name == "behavior"
        assert path.parent.parent.parent.name == "logs"

    def test_trigger_lands_in_behavior_subdir(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with self._patch_settings(tmp_agent_dir):
            path = write_t0_log(agent_id, behavior_type="trigger", metadata={"trigger_name": "t"})
        assert path is not None
        assert path.parent.name == "behavior"

    def test_heartbeat_lands_in_system_subdir(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with self._patch_settings(tmp_agent_dir):
            path = write_t0_log(agent_id, behavior_type="heartbeat", metadata={"tick": 1})
        assert path is not None
        assert path.parent.name == "system"

    def test_dream_lands_in_system_subdir(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with self._patch_settings(tmp_agent_dir):
            path = write_t0_log(agent_id, behavior_type="dream", metadata={"t3_processed": 0})
        assert path is not None
        assert path.parent.name == "system"


class TestAuditSplitCounts:
    def test_audit_counts_behavior_and_system_separately(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        logs_dir = tmp_agent_dir / str(agent_id) / "logs"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (logs_dir / today / "behavior").mkdir(parents=True)
        (logs_dir / today / "system").mkdir(parents=True)
        (logs_dir / today / "behavior" / "chat-1200-abcd.md").write_text("x", encoding="utf-8")
        (logs_dir / today / "behavior" / "trigger-1300-efgh.md").write_text("x", encoding="utf-8")
        (logs_dir / today / "system" / "heartbeat-1400-ijkl.md").write_text("x", encoding="utf-8")

        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            report = audit_t0_logs(agent_id)

        assert report["behavior_files"] == 2
        assert report["system_files"] == 1
        assert report["legacy_flat_files"] == 0
        assert report["total_files"] == 3
        assert report["healthy"] is True

    def test_audit_counts_legacy_flat_files(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        logs_dir = tmp_agent_dir / str(agent_id) / "logs"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (logs_dir / today).mkdir(parents=True)
        (logs_dir / today / "chat-1200-abcd.md").write_text("legacy", encoding="utf-8")

        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            report = audit_t0_logs(agent_id)

        assert report["legacy_flat_files"] == 1
        assert report["behavior_files"] == 0
        assert report["total_files"] == 1


class TestMigrateT0Layout:
    def test_moves_behavior_and_system_files(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        logs_dir = tmp_agent_dir / str(agent_id) / "logs" / "2026-04-10"
        logs_dir.mkdir(parents=True)
        (logs_dir / "chat-0900-aaaa.md").write_text("c", encoding="utf-8")
        (logs_dir / "trigger-1000-bbbb.md").write_text("t", encoding="utf-8")
        (logs_dir / "heartbeat-1100-cccc.md").write_text("h", encoding="utf-8")
        (logs_dir / "dream-1200-dddd.md").write_text("d", encoding="utf-8")

        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            report = migrate_t0_layout(agent_id)

        assert report["moved"] == 4
        assert report["skipped"] == 0
        assert (logs_dir / "behavior" / "chat-0900-aaaa.md").exists()
        assert (logs_dir / "behavior" / "trigger-1000-bbbb.md").exists()
        assert (logs_dir / "system" / "heartbeat-1100-cccc.md").exists()
        assert (logs_dir / "system" / "dream-1200-dddd.md").exists()
        assert not (logs_dir / "chat-0900-aaaa.md").exists()
        marker = tmp_agent_dir / str(agent_id) / ".t0_layout_v2"
        assert marker.exists()

    def test_idempotent_via_marker(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        logs_dir = tmp_agent_dir / str(agent_id) / "logs" / "2026-04-10"
        logs_dir.mkdir(parents=True)
        marker = tmp_agent_dir / str(agent_id) / ".t0_layout_v2"
        marker.write_text("migrated_at: 2026-04-10T00:00:00+00:00\n", encoding="utf-8")
        (logs_dir / "chat-0900-aaaa.md").write_text("should-not-move", encoding="utf-8")

        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            report = migrate_t0_layout(agent_id)

        assert report["moved"] == 0
        assert report["marker_existed"] == 1
        assert (logs_dir / "chat-0900-aaaa.md").exists()
        assert not (logs_dir / "behavior").exists()

    def test_skips_unknown_prefixes(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        logs_dir = tmp_agent_dir / str(agent_id) / "logs" / "2026-04-10"
        logs_dir.mkdir(parents=True)
        (logs_dir / "weird-0900-aaaa.md").write_text("?", encoding="utf-8")
        (logs_dir / "chat-1000-bbbb.md").write_text("c", encoding="utf-8")

        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            report = migrate_t0_layout(agent_id)

        assert report["moved"] == 1
        assert report["skipped"] == 1
        assert (logs_dir / "behavior" / "chat-1000-bbbb.md").exists()
        assert (logs_dir / "weird-0900-aaaa.md").exists()  # untouched

    def test_handles_missing_logs_dir(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        with patch("app.services.t0_logger.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
            report = migrate_t0_layout(agent_id)

        assert report["moved"] == 0
        assert report["skipped"] == 0
        assert (tmp_agent_dir / str(agent_id) / ".t0_layout_v2").exists()


# ── PR-2: behavior raw rendering (tool_results, errors, no truncation) ──


class TestChatLogIncludesToolResults:
    def test_tool_call_followed_by_inline_result(self) -> None:
        messages = [
            {"role": "user", "content": "search docs"},
            {
                "role": "assistant",
                "content": "looking up...",
                "tool_calls": [{"id": "call_1", "function": {"name": "web_search", "arguments": '{"q":"hive"}'}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "found 3 results: A, B, C"},
        ]
        result = _format_chat_log(messages, {"session_id": "s1"})
        assert "`web_search(" in result
        assert "→ result: found 3 results: A, B, C" in result

    def test_long_args_not_truncated_at_200_chars(self) -> None:
        long_query = "x" * 800
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "search", "arguments": f'{{"q":"{long_query}"}}'}}],
            },
        ]
        result = _format_chat_log(messages, {})
        assert "x" * 800 in result  # full 800-char arg present

    def test_multipart_tool_result_content(self) -> None:
        messages = [
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "fetch", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [{"type": "text", "text": "page body here"}],
            },
        ]
        result = _format_chat_log(messages, {})
        assert "→ result: page body here" in result

    def test_missing_tool_call_id_falls_back_to_order(self) -> None:
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "search", "arguments": "{}"}}],  # no id
            },
            {"role": "tool", "content": "fallback result"},  # no tool_call_id either
        ]
        result = _format_chat_log(messages, {})
        assert "→ result: fallback result" in result

    def test_no_crash_when_tool_result_missing(self) -> None:
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "search", "arguments": "{}"}}],
            },
            # no tool message at all
        ]
        result = _format_chat_log(messages, {})
        assert "`search(" in result
        assert "→ result:" not in result


class TestErrorsSection:
    def test_extracts_error_tool_results(self) -> None:
        messages = [
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "fetch", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "[Error] timeout after 30s"},
        ]
        result = _format_chat_log(messages, {})
        assert "## Errors" in result
        assert "[Error] timeout after 30s" in result
        assert "(call c1)" in result

    def test_extracts_assistant_error_field(self) -> None:
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "error": "rate_limit_429"},
        ]
        result = _format_chat_log(messages, {})
        assert "## Errors" in result
        assert "rate_limit_429" in result

    def test_no_errors_section_when_clean(self) -> None:
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = _format_chat_log(messages, {})
        assert "## Errors" not in result


class TestTriggerDelegationToolChain:
    def test_trigger_includes_tool_calls_and_results(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "running",
                "tool_calls": [{"id": "c1", "function": {"name": "fetch_news", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "5 articles"},
        ]
        result = _format_trigger_log(
            messages,
            {"trigger_name": "daily", "instruction": "Fetch news", "result": "done"},
        )
        assert "## Execution" in result
        assert "`fetch_news(" in result
        assert "→ result: 5 articles" in result

    def test_delegation_includes_tool_chain(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "delegating",
                "tool_calls": [{"id": "c2", "function": {"name": "summarize", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c2", "content": "summary text"},
        ]
        result = _format_delegation_log(
            messages,
            {"from_agent": "PM", "to_agent": "Researcher", "task": "summarize", "result": "ok"},
        )
        assert "→ result: summary text" in result

    def test_trigger_renders_errors_section(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "fetch", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "[Error] backend down"},
        ]
        result = _format_trigger_log(messages, {"trigger_name": "x", "instruction": "y", "result": "fail"})
        assert "## Errors" in result
        assert "backend down" in result


class TestRenderHelperEdgeCases:
    def test_empty_messages_returns_marker(self) -> None:
        from app.services.t0_logger import _render_messages_with_tools

        assert _render_messages_with_tools([]) == "(no messages)"

    def test_only_system_messages_returns_no_content(self) -> None:
        from app.services.t0_logger import _render_messages_with_tools

        # system messages aren't rendered → result should be "(no content)"
        out = _render_messages_with_tools([{"role": "system", "content": "you are helpful"}])
        assert out == "(no content)"


# ── PR-3: system log decision audit trail ──


class TestHeartbeatT3NormalizationSection:
    """PR-9: heartbeat log surfaces the T3 format normalizer report."""

    def test_renders_section_when_lines_were_fixed(self) -> None:
        result = _format_heartbeat_log(
            [],
            {
                "tick": 1,
                "t3_normalization": {
                    "fixed": 3,
                    "warnings": [],
                    "files_touched": ["feedback.md", "strategies.md"],
                },
            },
        )
        assert "## T3 Normalization" in result
        assert "Fixed: 3" in result
        assert "Warnings: 0" in result
        assert "feedback.md, strategies.md" in result

    def test_renders_warnings_when_unfixable_lines_present(self) -> None:
        result = _format_heartbeat_log(
            [],
            {
                "tick": 2,
                "t3_normalization": {
                    "fixed": 0,
                    "warnings": ["user.md: Hello world.", "user.md: stray prose"],
                    "files_touched": [],
                },
            },
        )
        assert "## T3 Normalization" in result
        assert "Warnings: 2" in result
        assert "### Warnings" in result
        assert "Hello world." in result
        assert "stray prose" in result

    def test_omits_section_when_nothing_to_report(self) -> None:
        result = _format_heartbeat_log(
            [],
            {
                "tick": 3,
                "t3_normalization": {"fixed": 0, "warnings": [], "files_touched": []},
            },
        )
        assert "## T3 Normalization" not in result

    def test_omits_section_when_key_missing(self) -> None:
        # Older callers don't pass this field at all — must still render.
        result = _format_heartbeat_log([], {"tick": 4})
        assert "## T3 Normalization" not in result


class TestHeartbeatLogReasoning:
    def test_includes_decision_reasoning_when_provided(self) -> None:
        result = _format_heartbeat_log(
            [],
            {"tick": 1, "score": 5, "reasoning": "noop because workspace/notes.md unchanged in 30 min"},
        )
        assert "## Decision Reasoning" in result
        assert "noop because workspace/notes.md unchanged" in result

    def test_omits_reasoning_section_when_empty(self) -> None:
        result = _format_heartbeat_log([], {"tick": 1})
        assert "## Decision Reasoning" not in result
        assert "## T3 Intake Candidates" in result  # other sections still emitted

    def test_includes_t2_inputs_section(self) -> None:
        result = _format_heartbeat_log(
            [],
            {"tick": 1, "t2_inputs": ["entry A", "entry B"]},
        )
        assert "## T3 Source Inputs Considered" in result
        assert "- entry A" in result
        assert "- entry B" in result

    def test_includes_tool_calls_when_messages_present(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "checking workspace",
                "tool_calls": [{"id": "h1", "function": {"name": "list_files", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "h1", "content": "10 files"},
        ]
        result = _format_heartbeat_log(messages, {"tick": 1})
        assert "## Tool Calls" in result
        assert "`list_files(" in result
        assert "→ result: 10 files" in result

    def test_omits_tool_calls_section_when_no_messages(self) -> None:
        result = _format_heartbeat_log([], {"tick": 1})
        assert "## Tool Calls" not in result


class TestDreamLogDecisions:
    def test_renders_structured_dedup_decisions(self) -> None:
        result = _format_dream_log(
            [],
            {
                "dedup_decisions": [
                    {"file": "feedback.md", "kept": "rep entry", "dropped_count": 4, "reason": "fuzzy match"}
                ],
            },
        )
        assert "## Dedup Decisions" in result
        assert "feedback.md" in result
        assert "dropped 4" in result

    def test_renders_promotion_decisions(self) -> None:
        result = _format_dream_log(
            [],
            {
                "promotion_decisions": [
                    {
                        "soul_excerpt": "respond in Chinese",
                        "source_t3_file": "feedback.md",
                        "repetition_count": 5,
                        "reason": "repeated 5+ times",
                    }
                ],
            },
        )
        assert "## Legacy Soul Promotion Audit" in result
        assert "respond in Chinese" in result
        assert "repeated 5x" in result
        assert "feedback.md" in result

    def test_falls_back_to_legacy_summary_when_decisions_missing(self) -> None:
        result = _format_dream_log(
            [],
            {
                "dedup_summary": "Merged 3 feedback entries",
                "soul_promotions": ["Core value: quality"],
            },
        )
        # Legacy keys still rendered when new structured fields are absent.
        assert "Merged 3 feedback entries" in result
        assert "Core value: quality" in result
        assert "## Dedup Decisions" not in result
        assert "## Legacy Soul Promotion Audit" in result

    def test_renders_dream_reasoning_when_provided(self) -> None:
        result = _format_dream_log(
            [],
            {"dream_reasoning": "consolidated 3 redundant feedback entries"},
        )
        assert "## Reasoning" in result
        assert "consolidated 3 redundant" in result


# ── PR-5: artifact spillover for large tool_results ──


class TestArtifactSpillover:
    def _patch_settings(self, tmp_agent_dir: Path):
        return patch(
            "app.services.t0_logger.get_settings",
            return_value=type("S", (), {"AGENT_DATA_DIR": str(tmp_agent_dir)})(),
        )

    def test_short_result_inlined_no_artifact(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "search", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "tiny result"},
        ]
        with self._patch_settings(tmp_agent_dir):
            path = write_t0_log(agent_id, behavior_type="chat", messages=messages, metadata={"session_id": "s1"})
        assert path is not None
        body = path.read_text(encoding="utf-8")
        assert "tiny result" in body
        assert "[artifact:" not in body
        assert not (path.parent.parent / "artifacts").exists()

    def test_large_result_spilled_to_artifact(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        big = "X" * 10000  # > _ARTIFACT_THRESHOLD_CHARS (8000)
        messages = [
            {"role": "user", "content": "fetch big"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_big", "function": {"name": "fetch_doc", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_big", "content": big},
        ]
        with self._patch_settings(tmp_agent_dir):
            path = write_t0_log(agent_id, behavior_type="chat", messages=messages, metadata={"session_id": "sess-big"})
        assert path is not None
        body = path.read_text(encoding="utf-8")

        # MD now contains a reference + preview, not the full payload.
        assert "[artifact: artifacts/" in body
        assert "preview:" in body
        assert big not in body  # full body must NOT be inlined

        # Artifact JSON exists with full content.
        artifacts_dir = path.parent.parent / "artifacts"
        assert artifacts_dir.is_dir()
        artifact_files = list(artifacts_dir.glob("*.json"))
        assert len(artifact_files) == 1
        import json as _json

        payload = _json.loads(artifact_files[0].read_text(encoding="utf-8"))
        assert payload["tool_name"] == "fetch_doc"
        assert payload["tool_call_id"] == "call_big"
        assert payload["session_id"] == "sess-big"
        assert payload["result"] == big
        assert payload["size_chars"] == 10000

    def test_threshold_boundary_no_spill(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        from app.services.t0_logger import _ARTIFACT_THRESHOLD_CHARS

        at_limit = "Y" * _ARTIFACT_THRESHOLD_CHARS  # exactly == threshold, should NOT spill
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "fetch", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": at_limit},
        ]
        with self._patch_settings(tmp_agent_dir):
            path = write_t0_log(agent_id, behavior_type="chat", messages=messages, metadata={"session_id": "s2"})
        assert path is not None
        assert not (path.parent.parent / "artifacts").exists()

    def test_artifact_filename_is_safe(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        big = "Z" * 9000
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "weird/id with spaces", "function": {"name": "x:y/z", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "weird/id with spaces", "content": big},
        ]
        with self._patch_settings(tmp_agent_dir):
            path = write_t0_log(agent_id, behavior_type="chat", messages=messages, metadata={"session_id": "s3"})
        assert path is not None
        artifacts_dir = path.parent.parent / "artifacts"
        files = list(artifacts_dir.glob("*.json"))
        assert len(files) == 1
        # No path-traversal characters in filename.
        assert "/" not in files[0].name
        assert " " not in files[0].name
        assert ":" not in files[0].name


class TestReplayResolvesArtifact:
    def test_replay_loads_artifact_into_message_content(self, tmp_path: Path, agent_id: uuid.UUID) -> None:
        from app.services.extract_agent import replay_messages_from_t0

        # Manually craft a chat MD that references an artifact.
        big = "Q" * 5000
        date_dir = tmp_path / str(agent_id) / "logs" / "2026-04-15"
        behavior_dir = date_dir / "behavior"
        artifacts_dir = date_dir / "artifacts"
        behavior_dir.mkdir(parents=True)
        artifacts_dir.mkdir(parents=True)

        artifact_path = artifacts_dir / "abc123-fetch_doc.json"
        import json as _json

        artifact_path.write_text(
            _json.dumps({"tool_call_id": "abc123", "tool_name": "fetch_doc", "result": big}),
            encoding="utf-8",
        )

        md = (
            "---\n"
            "type: chat\n"
            "session_id: sess-art\n"
            "source: web\n"
            "user: T\n"
            "started: 2026-04-15T08:00:00+00:00\n"
            "turns: 1\n"
            "tools: [fetch_doc]\n"
            "---\n\n"
            "## Turn 1\n"
            "**User**: go\n"
            "**Agent**: \n"
            "**Tools**:\n"
            "- `fetch_doc({})`\n"
            "  → result: [artifact: artifacts/abc123-fetch_doc.json] preview: QQQQ...\n"
        )
        chat_path = behavior_dir / "chat-1200-abcd.md"
        chat_path.write_text(md, encoding="utf-8")

        result = replay_messages_from_t0(chat_path)
        tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == big

    def test_replay_handles_missing_artifact_gracefully(self, tmp_path: Path, agent_id: uuid.UUID) -> None:
        from app.services.extract_agent import replay_messages_from_t0

        date_dir = tmp_path / str(agent_id) / "logs" / "2026-04-15"
        behavior_dir = date_dir / "behavior"
        behavior_dir.mkdir(parents=True)

        md = (
            "---\n"
            "type: chat\n"
            "session_id: sess-miss\n"
            "source: web\n"
            "user: T\n"
            "started: 2026-04-15T08:00:00+00:00\n"
            "turns: 1\n"
            "tools: []\n"
            "---\n\n"
            "## Turn 1\n"
            "**User**: go\n"
            "**Agent**: \n"
            "**Tools**:\n"
            "- `fetch({})`\n"
            "  → result: [artifact: artifacts/nope.json] preview: hint\n"
        )
        chat_path = behavior_dir / "chat-1200-miss.md"
        chat_path.write_text(md, encoding="utf-8")

        result = replay_messages_from_t0(chat_path)
        tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        # Falls back to the raw reference text — never crashes.
        assert "[artifact:" in tool_msgs[0]["content"]
