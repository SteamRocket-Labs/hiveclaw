"""Tests for auto-dream memory consolidation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.auto_dream import (
    MIN_SESSIONS_SINCE_DREAM,
    _simple_dedup,
    record_session_end,
    should_dream,
    _last_dream_time,
    _sessions_since_dream,
)


def _reset_state():
    _last_dream_time.clear()
    _sessions_since_dream.clear()


class TestDreamGates:
    """Auto-dream trigger condition evaluation."""

    def test_not_ready_with_zero_sessions(self) -> None:
        _reset_state()
        agent_id = uuid.uuid4()
        assert should_dream(agent_id) is False

    def test_ready_after_enough_sessions(self) -> None:
        _reset_state()
        agent_id = uuid.uuid4()
        for _ in range(MIN_SESSIONS_SINCE_DREAM):
            record_session_end(agent_id)
        assert should_dream(agent_id) is True

    def test_not_ready_if_recently_dreamed(self) -> None:
        _reset_state()
        agent_id = uuid.uuid4()
        # Record enough sessions
        for _ in range(MIN_SESSIONS_SINCE_DREAM + 1):
            record_session_end(agent_id)
        # But mark as recently dreamed
        _last_dream_time[agent_id.hex] = datetime.now(timezone.utc)
        assert should_dream(agent_id) is False

    def test_session_count_increments(self) -> None:
        _reset_state()
        agent_id = uuid.uuid4()
        record_session_end(agent_id)
        record_session_end(agent_id)
        assert _sessions_since_dream[agent_id.hex] == 2

    def test_independent_per_agent(self) -> None:
        _reset_state()
        a1 = uuid.uuid4()
        a2 = uuid.uuid4()
        for _ in range(MIN_SESSIONS_SINCE_DREAM):
            record_session_end(a1)
        assert should_dream(a1) is True
        assert should_dream(a2) is False

    def test_gate_persists_across_in_memory_reset(self, monkeypatch, tmp_path) -> None:
        import app.services.auto_dream as auto_dream

        _reset_state()
        monkeypatch.setattr(
            auto_dream,
            "get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
            raising=False,
        )

        agent_id = uuid.uuid4()
        for _ in range(MIN_SESSIONS_SINCE_DREAM):
            record_session_end(agent_id)

        auto_dream._last_dream_time.clear()
        auto_dream._sessions_since_dream.clear()

        assert should_dream(agent_id) is True


class TestSimpleDedup:
    """Fallback deduplication logic."""

    def test_removes_exact_duplicates(self) -> None:
        facts = [
            {"content": "fact A"},
            {"content": "fact A"},
            {"content": "fact B"},
        ]
        result = _simple_dedup(facts)
        assert len(result) == 2

    def test_case_insensitive(self) -> None:
        facts = [
            {"content": "User prefers Python"},
            {"content": "user prefers python"},
        ]
        result = _simple_dedup(facts)
        assert len(result) == 1

    def test_preserves_order(self) -> None:
        facts = [
            {"content": "first"},
            {"content": "second"},
            {"content": "first"},
        ]
        result = _simple_dedup(facts)
        assert result[0]["content"] == "first"
        assert result[1]["content"] == "second"

    def test_empty_content_skipped(self) -> None:
        facts = [
            {"content": ""},
            {"content": "valid"},
            {"content": "  "},
        ]
        result = _simple_dedup(facts)
        assert len(result) == 1
        assert result[0]["content"] == "valid"
