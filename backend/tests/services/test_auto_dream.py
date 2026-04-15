"""Tests for auto-dream memory consolidation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.auto_dream import (
    MIN_SESSIONS_SINCE_DREAM,
    _apply_dream_decisions,
    _consolidate_t3_files,
    _last_dream_time,
    _parse_dream_decision,
    _read_preservation_flags,
    _sessions_since_dream,
    _simple_dedup,
    _upsert_soul_section,
    record_session_end,
    should_dream,
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


# ── PR-10: LLM consolidation + decision application ──


class TestParseDreamDecision:
    def test_parses_raw_json(self) -> None:
        raw = '{"reasoning": "merged 2 duplicates", "soul_promotions": []}'
        result = _parse_dream_decision(raw)
        assert result is not None
        assert result["reasoning"] == "merged 2 duplicates"

    def test_strips_code_fences(self) -> None:
        raw = '```json\n{"reasoning": "ok"}\n```'
        result = _parse_dream_decision(raw)
        assert result is not None
        assert result["reasoning"] == "ok"

    def test_strips_bare_triple_backtick_fence(self) -> None:
        raw = '```\n{"reasoning": "ok"}\n```'
        result = _parse_dream_decision(raw)
        assert result is not None

    def test_extracts_embedded_json(self) -> None:
        raw = 'Here is the result:\n{"reasoning": "nested"}\nThat\'s all.'
        result = _parse_dream_decision(raw)
        assert result is not None
        assert result["reasoning"] == "nested"

    def test_returns_none_on_non_object(self) -> None:
        assert _parse_dream_decision("[1,2,3]") is None  # array, not object

    def test_returns_none_on_invalid_json(self) -> None:
        assert _parse_dream_decision("not json at all") is None

    def test_returns_none_on_empty(self) -> None:
        assert _parse_dream_decision("") is None


class TestUpsertSoulSection:
    def test_creates_new_section(self, tmp_path: Path) -> None:
        soul_path = tmp_path / "soul.md"
        soul_path.write_text("# Soul\n\n## Identity\n- Name: Alice\n", encoding="utf-8")
        added = _upsert_soul_section(soul_path, "Core Strategies", ["break down into subtasks"])
        assert added == 1
        content = soul_path.read_text(encoding="utf-8")
        assert "## Core Strategies" in content
        assert "- break down into subtasks" in content

    def test_appends_to_existing_section(self, tmp_path: Path) -> None:
        soul_path = tmp_path / "soul.md"
        soul_path.write_text(
            "# Soul\n\n## Learned Behaviors\n- always ask first\n", encoding="utf-8"
        )
        added = _upsert_soul_section(soul_path, "Learned Behaviors", ["verify after each step"])
        assert added == 1
        content = soul_path.read_text(encoding="utf-8")
        assert "- always ask first" in content
        assert "- verify after each step" in content

    def test_dedup_skips_existing_entry(self, tmp_path: Path) -> None:
        soul_path = tmp_path / "soul.md"
        soul_path.write_text(
            "# Soul\n\n## Learned Behaviors\n- Always Ask First\n", encoding="utf-8"
        )
        added = _upsert_soul_section(soul_path, "Learned Behaviors", ["always ask first"])
        # Case-insensitive dedup: nothing new.
        assert added == 0


class TestApplyDreamDecisions:
    def _scaffold(self, tmp_path: Path) -> uuid.UUID:
        agent_id = uuid.uuid4()
        agent_dir = tmp_path / str(agent_id)
        (agent_dir / "memory").mkdir(parents=True)
        (agent_dir / "soul.md").write_text(
            "# Soul\n\n## Identity\n- Name: Test\n", encoding="utf-8"
        )
        return agent_id

    def test_writes_soul_promotions_into_targeted_sections(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        decision = {
            "soul_promotions": [
                {
                    "content": "always prefer concise output",
                    "source_file": "feedback.md",
                    "section": "Learned Behaviors",
                    "reason": "repeated 4x",
                },
                {
                    "content": "break problems into 3 phases",
                    "source_file": "strategies.md",
                    "section": "Core Strategies",
                    "reason": "applied across tasks",
                },
            ]
        }
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions(agent_id, decision)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "## Learned Behaviors" in soul
        assert "- always prefer concise output" in soul
        assert "## Core Strategies" in soul
        assert "- break problems into 3 phases" in soul
        assert report["soul_added"] == 2

    def test_t3_merges_drop_duplicate_lines(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        feedback_path = tmp_path / str(agent_id) / "memory" / "feedback.md"
        feedback_path.write_text(
            "# Feedback\n\n"
            "- [2026-04-10] user prefers concise output\n"
            "- [2026-04-12] prefers concise output please\n"
            "- [2026-04-14] wants concise answers\n",
            encoding="utf-8",
        )
        decision = {
            "t3_merges": [
                {
                    "file": "feedback.md",
                    "keep": "[2026-04-10] user prefers concise output",
                    "drop": [
                        "prefers concise output please",
                        "wants concise answers",
                    ],
                    "reason": "all say the same thing",
                }
            ]
        }
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions(agent_id, decision)

        new_content = feedback_path.read_text(encoding="utf-8")
        assert "user prefers concise output" in new_content
        assert "prefers concise output please" not in new_content
        assert "wants concise answers" not in new_content
        assert report["t3_merges_applied"] == 1

    def test_contradictions_kept_new_drops_old(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        feedback_path = tmp_path / str(agent_id) / "memory" / "feedback.md"
        feedback_path.write_text(
            "# Feedback\n\n"
            "- [2026-04-10] use Japanese for responses\n"
            "- [2026-04-14] please respond in Chinese always\n",
            encoding="utf-8",
        )
        decision = {
            "t3_contradictions": [
                {
                    "file": "feedback.md",
                    "new": "respond in Chinese always",
                    "old": "use Japanese for responses",
                    "resolution": "kept_new",
                    "reason": "user switched language",
                }
            ]
        }
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions(agent_id, decision)

        new_content = feedback_path.read_text(encoding="utf-8")
        assert "respond in Chinese always" in new_content
        assert "use Japanese" not in new_content
        assert report["contradictions_resolved"] == 1

    def test_preservation_flags_persisted_to_sidecar(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        decision = {
            "preservation_flags": [
                {
                    "file": "feedback.md",
                    "content": "Never skip verification — founding principle",
                    "reason": "foundational principle from day one",
                }
            ]
        }
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions(agent_id, decision)
            flags = _read_preservation_flags(agent_id)

        assert report["preservation_flags_added"] == 1
        assert len(flags) == 1
        assert flags[0]["file"] == "feedback.md"
        assert "Never skip verification" in flags[0]["content"]

    def test_preservation_flags_dedup_on_reapply(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        decision = {
            "preservation_flags": [
                {"file": "feedback.md", "content": "principle A", "reason": "x"},
            ]
        }
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            _apply_dream_decisions(agent_id, decision)
            report2 = _apply_dream_decisions(agent_id, decision)
            flags = _read_preservation_flags(agent_id)

        assert report2["preservation_flags_added"] == 0  # already present
        assert len(flags) == 1


class TestConsolidateRespectsPreservation:
    def test_protected_entries_survive_cap(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()
        mem_dir = tmp_path / str(agent_id) / "memory"
        mem_dir.mkdir(parents=True)

        protected_line = "- [2024-01-01] foundational principle: never ship without tests"
        # 60 lines > _T3_MAX_ENTRIES_PER_FILE (50) so cap fires.
        lines = [protected_line]
        for i in range(60):
            lines.append(f"- [2026-04-{(i % 28) + 1:02d}] routine observation {i}")
        (mem_dir / "feedback.md").write_text(
            "# Feedback\n\n" + "\n".join(lines) + "\n", encoding="utf-8"
        )

        # Write preservation sidecar flagging the foundational line.
        import json

        (mem_dir / ".preservation.json").write_text(
            json.dumps({"protected": [{"file": "feedback.md", "content": "foundational principle"}]}),
            encoding="utf-8",
        )

        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            _consolidate_t3_files(agent_id)

        final = (mem_dir / "feedback.md").read_text(encoding="utf-8")
        assert "foundational principle: never ship without tests" in final

    def test_no_preservation_sidecar_means_default_cap(self, tmp_path: Path) -> None:
        """Generate lines with ratio < 0.7 pairwise so dedup leaves > 50 survivors."""
        import random

        agent_id = uuid.uuid4()
        mem_dir = tmp_path / str(agent_id) / "memory"
        mem_dir.mkdir(parents=True)

        oldest = "- [2024-01-01] canary-entry foundational principle about verification"
        # Random 24-char hex per line keeps pairwise similarity well under 0.7.
        rnd = random.Random(42)
        lines = [oldest]
        for i in range(60):
            unique = rnd.randbytes(12).hex()
            lines.append(f"- [2026-04-{(i % 28) + 1:02d}] {unique}")
        (mem_dir / "feedback.md").write_text(
            "# Feedback\n\n" + "\n".join(lines) + "\n", encoding="utf-8"
        )

        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            _consolidate_t3_files(agent_id)

        final = (mem_dir / "feedback.md").read_text(encoding="utf-8")
        # Without a preservation flag the oldest entry gets capped out.
        assert "canary-entry foundational principle" not in final


@pytest.mark.asyncio
class TestRunDreamIntegration:
    async def test_run_dream_falls_back_when_llm_unavailable(self, tmp_path: Path) -> None:
        """If LLM consolidator returns None, run_dream still completes via pure-Python path."""
        from app.services.auto_dream import run_dream

        agent_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        mem_dir = tmp_path / str(agent_id) / "memory"
        mem_dir.mkdir(parents=True)
        (tmp_path / str(agent_id) / "soul.md").write_text("# Soul\n\n## Identity\n", encoding="utf-8")
        (mem_dir / "feedback.md").write_text(
            "# Feedback\n\n- [2026-04-10] prefer concise\n", encoding="utf-8"
        )

        with patch("app.services.auto_dream.get_settings") as mock_settings, \
             patch("app.services.auto_dream._dream_llm_consolidate", return_value=None) as mock_llm, \
             patch("app.services.auto_dream._read_all_t3", return_value={"feedback.md": "# Feedback\n\n- [2026-04-10] prefer concise\n"}):
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            result = await run_dream(agent_id, tenant_id)

        mock_llm.assert_called_once()
        assert "consolidated" in result
        # Fell back successfully; strategy marker reflects md_only path.

    async def test_run_dream_applies_llm_decision_when_available(self, tmp_path: Path) -> None:
        """If LLM returns a decision, run_dream applies it before pure-Python cleanup."""
        from app.services.auto_dream import run_dream

        agent_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        mem_dir = tmp_path / str(agent_id) / "memory"
        mem_dir.mkdir(parents=True)
        (tmp_path / str(agent_id) / "soul.md").write_text(
            "# Soul\n\n## Identity\n", encoding="utf-8"
        )
        (mem_dir / "feedback.md").write_text(
            "# Feedback\n\n- [2026-04-10] user always prefers concise output\n",
            encoding="utf-8",
        )

        fake_decision = {
            "reasoning": "clear repeated preference, promoting",
            "soul_promotions": [
                {
                    "content": "always prefer concise output",
                    "source_file": "feedback.md",
                    "section": "Learned Behaviors",
                    "reason": "repeated across sessions",
                }
            ],
        }

        async def fake_llm(*_args, **_kwargs):
            return fake_decision

        with patch("app.services.auto_dream.get_settings") as mock_settings, \
             patch("app.services.auto_dream._dream_llm_consolidate", side_effect=fake_llm):
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            await run_dream(agent_id, tenant_id)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "## Learned Behaviors" in soul
        assert "- always prefer concise output" in soul
