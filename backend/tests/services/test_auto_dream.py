"""Tests for auto-dream memory consolidation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.auto_dream import (
    _AUTO_DREAM_SYSTEM_PROMPT,
    _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE,
    MIN_SESSIONS_SINCE_DREAM,
    MIN_HEARTBEAT_TICKS_SINCE_DREAM,
    _apply_dream_decisions,
    _build_dream_consolidation_user_prompt,
    _consolidate_t3_files,
    _last_dream_time,
    _heartbeat_ticks_since_dream,
    _parse_dream_decision,
    _read_preservation_flags,
    _sessions_since_dream,
    _simple_dedup,
    _upsert_soul_section,
    record_session_end,
    record_heartbeat_tick,
    should_dream,
)


def _reset_state():
    _last_dream_time.clear()
    _sessions_since_dream.clear()
    _heartbeat_ticks_since_dream.clear()


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

    def test_record_dream_activity_skips_noop(self) -> None:
        """An idle heartbeat tick (OUTCOME:noop) is not dream-worthy activity —
        without this, the activity gate is a pure timer and silent agents dream."""
        from app.services.auto_dream import record_dream_activity

        _reset_state()
        agent_id = uuid.uuid4()
        record_dream_activity(agent_id, "noop")
        assert _sessions_since_dream.get(agent_id.hex, 0) == 0
        assert _heartbeat_ticks_since_dream.get(agent_id.hex, 0) == 0

        record_dream_activity(agent_id, "action_taken")
        assert _sessions_since_dream[agent_id.hex] == 1
        assert _heartbeat_ticks_since_dream[agent_id.hex] == 1

    def test_soft_dream_allowed_while_full_dream_waits_on_time(self, monkeypatch) -> None:
        """The relief valve must work during the 24h full-dream wait: sessions
        may exceed the activity gate long before the time gate opens, and soft
        dream must NOT stand aside for a full dream that is hours away."""
        import app.services.auto_dream as auto_dream
        from app.services.auto_dream import should_soft_dream

        _reset_state()
        agent_id = uuid.uuid4()
        for _ in range(MIN_SESSIONS_SINCE_DREAM + 1):  # activity gate long met
            record_session_end(agent_id)
        # last full dream: recent enough that the 24h gate is closed, old
        # enough that the soft-dream spacing has passed
        _last_dream_time[agent_id.hex] = datetime.now(timezone.utc) - timedelta(
            hours=auto_dream._MIN_HOURS_BETWEEN_SOFT_DREAMS + 1
        )
        monkeypatch.setattr(auto_dream, "_count_t3_entries", lambda _aid: 120)

        assert should_dream(agent_id) is False  # full dream still waiting on time
        assert should_soft_dream(agent_id) is True  # relief valve stays open

    def test_soft_dream_yields_when_full_dream_is_due(self, monkeypatch) -> None:
        import app.services.auto_dream as auto_dream
        from app.services.auto_dream import should_soft_dream

        _reset_state()
        agent_id = uuid.uuid4()
        for _ in range(MIN_SESSIONS_SINCE_DREAM):
            record_session_end(agent_id)
        # no last dream on record → time gate open → full dream due
        monkeypatch.setattr(auto_dream, "_count_t3_entries", lambda _aid: 120)

        assert should_dream(agent_id) is True
        assert should_soft_dream(agent_id) is False  # yields to the full dream

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

    def test_heartbeat_tick_gate_persists_across_in_memory_reset(self, monkeypatch, tmp_path) -> None:
        import app.services.auto_dream as auto_dream

        _reset_state()
        monkeypatch.setattr(
            auto_dream,
            "get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
            raising=False,
        )

        agent_id = uuid.uuid4()
        for _ in range(MIN_HEARTBEAT_TICKS_SINCE_DREAM):
            record_heartbeat_tick(agent_id)

        auto_dream._heartbeat_ticks_since_dream.clear()

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
        soul_path.write_text("# Soul\n\n## Learned Behaviors\n- always ask first\n", encoding="utf-8")
        added = _upsert_soul_section(soul_path, "Learned Behaviors", ["verify after each step"])
        assert added == 1
        content = soul_path.read_text(encoding="utf-8")
        assert "- always ask first" in content
        assert "- verify after each step" in content

    def test_dedup_skips_existing_entry(self, tmp_path: Path) -> None:
        soul_path = tmp_path / "soul.md"
        soul_path.write_text("# Soul\n\n## Learned Behaviors\n- Always Ask First\n", encoding="utf-8")
        added = _upsert_soul_section(soul_path, "Learned Behaviors", ["always ask first"])
        # Case-insensitive dedup: nothing new.
        assert added == 0


class TestApplyDreamDecisions:
    def _scaffold(self, tmp_path: Path) -> uuid.UUID:
        agent_id = uuid.uuid4()
        agent_dir = tmp_path / str(agent_id)
        (agent_dir / "memory").mkdir(parents=True)
        (agent_dir / "soul.md").write_text("# Soul\n\n## Identity\n- Name: Test\n", encoding="utf-8")
        return agent_id

    def test_writes_soul_promotions_into_targeted_sections(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        decision = {
            "soul_promotions": [
                {
                    "content": "always prefer concise output",
                    "source_file": "feedback.md",
                    "source_refs": ["t3:memory/feedback.md#entry:a", "t3:memory/feedback.md#entry:b"],
                    "evidence": "system_observed",
                    "section": "Learned Behaviors",
                    "reason": "repeated 4x",
                },
                {
                    "content": "break problems into 3 phases",
                    "source_file": "strategies.md",
                    "source_refs": ["t3:memory/strategies.md#entry:a", "t3:memory/strategies.md#entry:b"],
                    "evidence": "system_observed",
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


class TestDreamFrozenMissionGate:
    """D6 (docs/agent-memory-purity-spec.md): the dream contradiction gate must
    compare soul promotions against the soul's FROZEN Mission/charter, not only
    against other T3 entries.

    Production symptom: an agent whose frozen Mission says "scan three times
    daily + proactively push" accumulated a dream-promoted Learned Behavior
    "disable three-times scanning, switch to once weekly on Friday" — a direct
    self-contradiction. Spec §5 (dream does not bypass owner/charter gates) +
    §4.6 (soul = identity). Judging contradiction is an intelligent step →
    AI-Native L1: the LLM judge is the primary path; mechanical overlap is only
    an observable fallback when the judge is unavailable.
    """

    _MISSION_SOUL = (
        "# Soul — Radar\n\n"
        "## Identity & Mission\n"
        "- **Name**: Radar\n"
        "- **Role**: Scan the exhibition floor three times daily and proactively "
        "push fresh leads to the owner.\n\n"
        "## Frozen Owner Agency Charter\n"
        "**Full Authority**\n"
        "- Run the three-times-daily scan and prepare lead briefs.\n\n"
        "_These charter sections are frozen._\n"
    )

    def _scaffold(self, tmp_path: Path) -> uuid.UUID:
        agent_id = uuid.uuid4()
        agent_dir = tmp_path / str(agent_id)
        (agent_dir / "memory").mkdir(parents=True)
        (agent_dir / "soul.md").write_text(self._MISSION_SOUL, encoding="utf-8")
        return agent_id

    def _promotion(self, content: str) -> dict:
        return {
            "soul_promotions": [
                {
                    "content": content,
                    "source_file": "strategies.md",
                    "source_refs": [
                        "t3:memory/strategies.md#entry:a",
                        "t3:memory/strategies.md#entry:b",
                    ],
                    "evidence": "system_observed",
                    "section": "Learned Behaviors",
                    "reason": "observed twice",
                }
            ]
        }

    def test_promotion_contradicting_frozen_mission_is_held(self, tmp_path: Path) -> None:
        from app.services.auto_dream import _apply_dream_decisions_unlocked

        agent_id = self._scaffold(tmp_path)
        # Injected LLM judge = AI-Native L1 primary path. It sees the frozen
        # charter text + the candidate and returns a structured verdict.
        seen: dict[str, str] = {}

        def judge(frozen_charter: str, content: str) -> dict:
            seen["frozen_charter"] = frozen_charter
            seen["content"] = content
            return {"contradicts": True, "reason": "candidate disables the mandated three-times-daily scan"}

        decision = self._promotion("Disable the three-times-daily scan; scan only once per week on Friday.")
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions_unlocked(agent_id, decision, contradiction_judge=judge)

        # The judge must have received the FROZEN Mission/charter, not T3.
        assert "three times daily" in seen["frozen_charter"].lower()
        assert "Frozen Owner Agency Charter" in seen["frozen_charter"]
        # Contradicting candidate must NOT land in soul.
        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "once per week on Friday" not in soul
        assert report["soul_added"] == 0
        assert report["soul_contradicted_frozen"] == 1

    def test_aligned_promotion_passes_frozen_mission_gate(self, tmp_path: Path) -> None:
        from app.services.auto_dream import _apply_dream_decisions_unlocked

        agent_id = self._scaffold(tmp_path)

        def judge(frozen_charter: str, content: str) -> dict:
            return {"contradicts": False, "reason": "aligns with proactive lead delivery"}

        decision = self._promotion("Always include a one-line summary at the top of each lead brief.")
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions_unlocked(agent_id, decision, contradiction_judge=judge)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "- Always include a one-line summary at the top of each lead brief." in soul
        assert report["soul_added"] == 1
        assert report["soul_contradicted_frozen"] == 0

    def test_mechanical_fallback_blocks_negated_mission_when_judge_unavailable(self, tmp_path: Path) -> None:
        # No judge injected → mechanical overlap backstop (observable fallback,
        # never the primary path). A candidate that negates a frozen mission
        # token ("disable ... three-times ... scan") must still be caught.
        from app.services.auto_dream import _apply_dream_decisions_unlocked

        agent_id = self._scaffold(tmp_path)
        decision = self._promotion("Disable the three-times-daily scan; only scan once weekly on Friday.")
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions_unlocked(agent_id, decision, contradiction_judge=None)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "once weekly on Friday" not in soul
        assert report["soul_added"] == 0
        assert report["soul_contradicted_frozen"] == 1

    @pytest.mark.asyncio
    async def test_production_llm_judge_is_built_and_applied(self, tmp_path: Path) -> None:
        # Proves the AI-Native L1 path is wired: _build_frozen_mission_judge runs
        # the per-item LLM judge in the async layer, and the resulting verdict is
        # what _apply_dream_decisions enforces — not the mechanical fallback.
        from app.services.auto_dream import _apply_dream_decisions, _build_frozen_mission_judge

        agent_id = self._scaffold(tmp_path)
        tenant_id = uuid.uuid4()
        decision = self._promotion("Switch the cadence to monthly reviews only.")
        judged: list[tuple[str, str]] = []

        async def fake_judge(model_config, frozen_charter, content):
            judged.append((frozen_charter, content))
            return {"contradicts": True, "reason": "monthly cadence conflicts with three-times-daily mission"}

        with (
            patch("app.services.auto_dream.get_settings") as mock_settings,
            patch(
                "app.services.memory_service._get_summary_model_config",
                return_value={"provider": "x", "model": "y"},
            ),
            patch("app.services.auto_dream._judge_frozen_mission_contradiction", new=fake_judge),
        ):
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            judge = await _build_frozen_mission_judge(agent_id, tenant_id, decision)
            assert judge is not None, "production judge should be built when a summary model exists"
            report = _apply_dream_decisions(agent_id, decision, contradiction_judge=judge)

        # The per-item LLM judge saw the frozen charter + candidate (L1 visibility).
        assert judged and "three times daily" in judged[0][0].lower()
        assert judged[0][1] == "Switch the cadence to monthly reviews only."
        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "monthly reviews only" not in soul
        assert report["soul_contradicted_frozen"] == 1


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
        (mem_dir / "feedback.md").write_text("# Feedback\n\n" + "\n".join(lines) + "\n", encoding="utf-8")

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
        (mem_dir / "feedback.md").write_text("# Feedback\n\n" + "\n".join(lines) + "\n", encoding="utf-8")

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
        (mem_dir / "feedback.md").write_text("# Feedback\n\n- [2026-04-10] prefer concise\n", encoding="utf-8")

        with (
            patch("app.services.auto_dream.get_settings") as mock_settings,
            patch("app.services.auto_dream._dream_llm_consolidate", return_value=None) as mock_llm,
            patch(
                "app.services.auto_dream._read_all_t3",
                return_value={"feedback.md": "# Feedback\n\n- [2026-04-10] prefer concise\n"},
            ),
        ):
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
        (tmp_path / str(agent_id) / "soul.md").write_text("# Soul\n\n## Identity\n", encoding="utf-8")
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
                    "source_refs": ["t3:memory/feedback.md#entry:a", "t3:memory/feedback.md#entry:b"],
                    "evidence": "system_observed",
                    "section": "Learned Behaviors",
                    "reason": "repeated across sessions",
                }
            ],
        }

        async def fake_llm(*_args, **_kwargs):
            return fake_decision

        with (
            patch("app.services.auto_dream.get_settings") as mock_settings,
            patch("app.services.auto_dream._dream_llm_consolidate", side_effect=fake_llm),
        ):
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            await run_dream(agent_id, tenant_id)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "## Learned Behaviors" in soul
        assert "- always prefer concise output" in soul


# ── PR-13: dream prompt best-practices (XML + few-shot + anti-patterns) ──


class TestDreamSystemPromptStructure:
    def test_system_prompt_uses_xml_tags(self) -> None:
        for tag in ("<role>", "</role>", "<identity_stakes>", "<output_contract>"):
            assert tag in _AUTO_DREAM_SYSTEM_PROMPT, f"missing tag: {tag}"

    def test_system_prompt_warns_identity_stakes(self) -> None:
        # The whole reason dream is more cautious than heartbeat.
        assert "identity_stakes" in _AUTO_DREAM_SYSTEM_PROMPT
        assert "soul.md" in _AUTO_DREAM_SYSTEM_PROMPT
        assert "cannot be" in _AUTO_DREAM_SYSTEM_PROMPT.lower() or "cannot" in _AUTO_DREAM_SYSTEM_PROMPT.lower()
        assert "surgeon" in _AUTO_DREAM_SYSTEM_PROMPT.lower()

    def test_system_prompt_demands_json_only(self) -> None:
        # Guardrail against LLM wrapping output in prose or code fences.
        assert "No prose" in _AUTO_DREAM_SYSTEM_PROMPT or "no prose" in _AUTO_DREAM_SYSTEM_PROMPT
        assert (
            "no code fences" in _AUTO_DREAM_SYSTEM_PROMPT.lower() or "no markdown" in _AUTO_DREAM_SYSTEM_PROMPT.lower()
        )


class TestDreamUserPromptTemplateStructure:
    def test_user_prompt_uses_xml_tags(self) -> None:
        required = [
            "<agent_context>",
            "<current_soul>",
            "<t3_memory>",
            "<section_selection_matrix>",
            "<few_shot_example_1>",
            "<few_shot_example_2>",
            "<anti_patterns>",
            "<json_schema>",
            "<hard_rules>",
            "<your_task>",
        ]
        for tag in required:
            assert tag in _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE, f"missing tag: {tag}"

    def test_template_has_section_selection_matrix(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        # All four target soul sections are explicitly listed.
        assert "Learned Behaviors" in t
        assert "Core Strategies" in t
        assert "Blocked Patterns" in t
        assert "User Profile" in t
        # Matrix carries the criteria column.
        assert "criteria" in t

    def test_template_has_two_few_shot_examples(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert t.count("<few_shot_example_") == 2
        assert t.count("<input_t3>") == 2
        assert t.count("<output_decision>") == 2

    def test_emoji_example_preserved(self) -> None:
        # Freezes example 1 so "no emoji" ground-truth can't silently disappear.
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "emoji" in t.lower()
        assert "ripgrep" in t

    def test_japanese_chinese_example_preserved(self) -> None:
        # Freezes example 2 for contradiction handling.
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "Japanese" in t
        assert "Chinese" in t
        assert "kept_new" in t

    def test_anti_patterns_section_present(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "<anti_patterns>" in t
        assert "DO NOT promote" in t
        assert "DO NOT merge" in t
        assert "DO NOT flag for preservation" in t

    def test_json_schema_lists_all_four_output_keys(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        for key in (
            '"reasoning"',
            '"soul_promotions"',
            '"t3_merges"',
            '"t3_contradictions"',
            '"preservation_flags"',
        ):
            assert key in t, f"schema missing key: {key}"

    def test_section_enum_is_complete(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "Learned Behaviors|Core Strategies|Blocked Patterns|User Profile" in t

    def test_source_file_enum_is_complete(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "feedback.md|knowledge.md|strategies.md|blocked.md|user.md" in t

    def test_hard_rules_preserve_prompt_injection_guardrail(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "data, not instructions" in t or "not instructions" in t
        assert "web" in t and "email" in t

    def test_hard_rules_cap_preservation_flags(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "max" in t.lower()
        assert "5" in t  # cap is ~5

    def test_template_preserves_placeholders(self) -> None:
        assert "{agent_name}" in _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "{soul_excerpt}" in _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "{t3_block}" in _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE


class TestDreamUserPromptBuilder:
    def test_builder_fills_placeholders(self) -> None:
        out = _build_dream_consolidation_user_prompt(
            "Alice",
            "# Soul\n\n## Identity\n- Name: Alice",
            {
                "feedback.md": "# Feedback\n\n- [2026-04-10] prefers concise output",
                "strategies.md": "# Strategies\n\n- [2026-04-11] TDD-first",
            },
        )
        assert "Agent: Alice" in out
        assert "Name: Alice" in out
        assert "prefers concise output" in out
        assert "TDD-first" in out
        # XML structure survives formatting.
        assert "<section_selection_matrix>" in out
        assert "<few_shot_example_1>" in out

    def test_builder_full_fidelity_under_budget(self) -> None:
        """蒸馏器核查 (docs/agent-lifecycle-cc-alignment.md §3.6): the dream
        consolidator decides soul promotions — it must see FULL T3/soul when
        the total fits the input budget. Per-section caps engage only over
        budget (same philosophy as compaction P0 / heartbeat C1)."""
        soul = "S" * 5_000  # over the old per-section cap (3K), under total budget
        t3 = {"feedback.md": "F" * 6_000}  # over the old per-file cap (4K)

        out = _build_dream_consolidation_user_prompt("A", soul, t3)

        assert "truncated" not in out
        assert "S" * 5_000 in out  # soul intact
        assert "F" * 6_000 in out  # T3 file intact

    def test_builder_caps_only_over_total_budget(self) -> None:
        from app.services.auto_dream import _DREAM_INPUT_TOTAL_BUDGET_CHARS

        big = "X" * (_DREAM_INPUT_TOTAL_BUDGET_CHARS + 10_000)
        out = _build_dream_consolidation_user_prompt("A", "soul", {"feedback.md": big})

        assert "truncated" in out  # observable marker
        assert len(out) < len(big)  # actually bounded

    def test_builder_handles_no_t3_files(self) -> None:
        out = _build_dream_consolidation_user_prompt("A", "soul", {})
        assert "(no T3 files)" in out


def test_dream_consolidator_template_is_loaded_into_prompt() -> None:
    from app.services.auto_dream import _build_dream_consolidation_user_prompt

    out = _build_dream_consolidation_user_prompt(
        "Alice",
        "# Soul\n",
        {"feedback.md": "- [2026-05-02] User requires evidence-tagged memory"},
    )

    assert "memory_promotion_candidate" in out
    assert "source_refs" in out
    assert "rollback_ref" in out
    assert "dream may propose candidates" in out.lower()


def test_feedback_decision_chains_propose_charter_calibration_without_mutation() -> None:
    from app.services.auto_dream import propose_charter_calibrations_from_feedback
    from app.services.decision_trace import DecisionTraceStore

    store = DecisionTraceStore()
    decision = store.record_decision(
        action="send external vendor reply",
        chosen="ask",
        reasoning="External-visible reply needed owner confirmation.",
        alternatives_considered=["send directly"],
        situational_factors=["charter_confirm_first"],
        charter_zone="confirm_first",
        preflight={"decision": "ask"},
        sensitivity="PL1_public",
    )
    store.record_feedback(
        decision_id=decision.id,
        reaction="approved",
        polarity="positive",
        source="direct_owner",
        rationale_from_owner="Asking first was correct this time.",
    )

    proposals = propose_charter_calibrations_from_feedback(store)

    assert proposals == [
        {
            "decision_id": decision.id,
            "action": "send external vendor reply",
            "proposal": "consider_full_authority",
            "reason": "Owner approved a confirm-first action; repeated evidence may justify broader authority.",
        }
    ]
