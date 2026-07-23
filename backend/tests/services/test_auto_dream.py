"""Tests for auto-dream memory consolidation."""

from __future__ import annotations

import uuid
import json
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
    _SOUL_MEMORY_GATE_SYSTEM_PROMPT,
    _heartbeat_ticks_since_dream,
    _parse_dream_decision,
    _read_preservation_flags,
    _write_preservation_flags,
    _sessions_since_dream,
    record_session_end,
    record_heartbeat_tick,
    should_dream,
)


def _reset_state():
    _last_dream_time.clear()
    _sessions_since_dream.clear()
    _heartbeat_ticks_since_dream.clear()


def test_soul_memory_gate_prompt_has_metric_specific_score_standards() -> None:
    assert "<metric_score_standards>" in _SOUL_MEMORY_GATE_SYSTEM_PROMPT
    assert "evidence_strength: 0=no cited accepted T3/T2 source refs" in _SOUL_MEMORY_GATE_SYSTEM_PROMPT
    assert "stability: 0=one-off/transient" in _SOUL_MEMORY_GATE_SYSTEM_PROMPT
    assert "identity_fit: 0=ordinary task detail" in _SOUL_MEMORY_GATE_SYSTEM_PROMPT
    assert "conflict_safety: 0=conflicts with frozen charter" in _SOUL_MEMORY_GATE_SYSTEM_PROMPT
    assert "prompt_blast_radius: 0=broad always-on behavior change" in _SOUL_MEMORY_GATE_SYSTEM_PROMPT


def test_soul_platform_gate_honors_independent_model_promotion_without_score_cutoff() -> None:
    from app.services.auto_dream import _soul_review_passed

    review = {
        "reviewer": "soul_memory_gate_agent",
        "source": "independent_llm",
        "recommendation": "promote",
        "evidence_strength": {"score": 0, "rationale": "model-calibrated"},
        "stability": {"score": 1, "rationale": "model-calibrated"},
        "identity_fit": {"score": 2, "rationale": "model-calibrated"},
        "conflict_safety": {"score": 1, "rationale": "model-calibrated"},
        "prompt_blast_radius": {"score": 0, "rationale": "model-calibrated"},
    }

    assert _soul_review_passed(review) == (True, "Soul Memory Gate review passed")


@pytest.mark.asyncio
async def test_independent_soul_review_uses_covered_model_passes_for_oversized_input(tmp_path, monkeypatch) -> None:
    import app.services.llm_client as llm_client_mod
    from app.services.auto_dream import _review_soul_candidate_with_llm

    calls: list[str] = []
    profile_tail = "DECISIVE_SOUL_REVIEW_PROFILE_TAIL"
    candidate_tail = "DECISIVE_SOUL_REVIEW_CANDIDATE_TAIL"

    class FakeClient:
        async def stream(self, *, messages, max_tokens, temperature):
            del max_tokens, temperature
            prompt = messages[-1].content
            calls.append(prompt)
            if "<coverage_chunk" in prompt:
                return SimpleNamespace(content="coverage-notes:" + prompt[-300:])
            if "<coverage_note" in prompt:
                return SimpleNamespace(content="reduced-coverage-notes")
            if len(prompt) > 30_000:
                raise RuntimeError("provider context window exceeded")
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "candidate_id": "candidate-1",
                        "recommendation": "hold",
                        "evidence_strength": {"score": 2, "rationale": "reviewed"},
                        "stability": {"score": 2, "rationale": "reviewed"},
                        "identity_fit": {"score": 2, "rationale": "reviewed"},
                        "conflict_safety": {"score": 3, "rationale": "reviewed"},
                        "prompt_blast_radius": {"score": 3, "rationale": "reviewed"},
                    }
                )
            )

        async def close(self):
            return None

    monkeypatch.setattr(llm_client_mod, "create_llm_client_from_config", lambda _config: FakeClient())
    review = await _review_soul_candidate_with_llm(
        metered_model_config={"provider": "openai", "model": "gpt-4.1", "max_input_tokens": 10_000},
        candidate_id="candidate-1",
        candidate={"soul_md_next": ("candidate " * 4_000) + candidate_tail},
        current_soul=("soul " * 4_000) + "SOUL-TAIL",
        frozen_charter="frozen charter",
        t3_files={"memory/profiles/owner.md": ("profile " * 4_000) + profile_tail},
        coverage_path=tmp_path / "soul-review-coverage.json",
    )

    mapped = "\n".join(call for call in calls if "<coverage_chunk" in call)
    assert review is not None
    assert profile_tail in mapped
    assert candidate_tail in mapped
    coverage = json.loads((tmp_path / "soul-review-coverage.json").read_text(encoding="utf-8"))
    assert coverage["complete"] is True


@pytest.mark.asyncio
async def test_frozen_charter_judge_uses_covered_model_passes_for_oversized_input(tmp_path, monkeypatch) -> None:
    import app.services.llm_client as llm_client_mod
    from app.services.auto_dream import _judge_frozen_mission_contradiction

    calls: list[str] = []
    candidate_tail = "DECISIVE_FROZEN_JUDGE_TAIL"

    class FakeClient:
        async def stream(self, *, messages, max_tokens, temperature):
            del max_tokens, temperature
            prompt = messages[-1].content
            calls.append(prompt)
            if "<coverage_chunk" in prompt:
                return SimpleNamespace(content="coverage-notes:" + prompt[-300:])
            if "<coverage_note" in prompt:
                return SimpleNamespace(content="reduced-coverage-notes")
            if len(prompt) > 30_000:
                raise RuntimeError("provider context window exceeded")
            return SimpleNamespace(content='{"contradicts": false, "reason": "compatible"}')

        async def close(self):
            return None

    monkeypatch.setattr(llm_client_mod, "create_llm_client_from_config", lambda _config: FakeClient())
    verdict = await _judge_frozen_mission_contradiction(
        {"provider": "openai", "model": "gpt-4.1", "max_input_tokens": 10_000},
        "frozen charter",
        ("candidate evidence " * 4_000) + candidate_tail,
        coverage_path=tmp_path / "frozen-judge-coverage.json",
    )

    assert verdict == {"contradicts": False, "reason": "compatible"}
    mapped = "\n".join(call for call in calls if "<coverage_chunk" in call)
    assert candidate_tail in mapped
    coverage = json.loads((tmp_path / "frozen-judge-coverage.json").read_text(encoding="utf-8"))
    assert coverage["complete"] is True


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

    def test_dream_state_migrates_to_memory_control(self, tmp_path: Path, monkeypatch) -> None:
        import app.services.auto_dream as auto_dream
        from app.config import get_settings

        _reset_state()
        auto_dream._dream_version.clear()
        auto_dream._dream_history.clear()
        monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
        agent_id = uuid.uuid4()
        mem_dir = tmp_path / str(agent_id) / "memory"
        mem_dir.mkdir(parents=True)
        legacy = mem_dir / "auto_dream_state.json"
        legacy.write_text(
            json.dumps(
                {
                    "last_dream_time": "2026-06-01T00:00:00+00:00",
                    "sessions_since_dream": 2,
                    "heartbeat_ticks_since_dream": 3,
                    "version": 4,
                    "history": [{"version": 4, "timestamp": "2026-06-01T00:00:00+00:00"}],
                }
            ),
            encoding="utf-8",
        )

        last, sessions = auto_dream._load_dream_state(agent_id)

        canonical = mem_dir / "control" / "auto_dream_state.json"
        assert last is not None
        assert sessions == 2
        assert canonical.exists()
        assert not legacy.exists()

        record_session_end(agent_id)
        payload = json.loads(canonical.read_text(encoding="utf-8"))
        assert payload["sessions_since_dream"] == 3
        assert not legacy.exists()

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


# ── PR-10: LLM consolidation + decision application ──


class TestParseDreamDecision:
    def test_parses_raw_json(self) -> None:
        raw = '{"reasoning": "merged 2 duplicates", "soul_candidate": null}'
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


class TestSoulCandidatePackage:
    @staticmethod
    def passing_candidate(*, soul_next: str, source_refs: list[str] | None = None) -> dict:
        refs = source_refs or ["t3:memory/t3/worker.md#block:soul-principle-1"]
        candidate = {
            "target": "soul.md",
            "soul_pitch_md": "# Soul Pitch\n\nPromote a stable verification principle from accepted T3 evidence.\n",
            "soul_patch_md": (
                "# Soul Patch\n\n"
                '<soul_principle id="verification-loop" stability="stable">\n'
                "Always verify after material changes.\n"
                "<source_refs>\n"
                f'<source_ref ref="{refs[0]}" />\n'
                "</source_refs>\n"
                "</soul_principle>\n"
            ),
            "soul_md_next": soul_next,
            "source_refs": refs,
            "requires_owner_approval": False,
            "memory_gate_review": {
                "candidate_id": "",
                "reviewer": "soul_memory_gate_agent",
                "source": "independent_llm",
                "recommendation": "promote",
                "evidence_strength": {"score": 4, "rationale": "multiple accepted T3 refs support it"},
                "stability": {"score": 4, "rationale": "stable across sessions"},
                "identity_fit": {"score": 3, "rationale": "affects always-on quality behavior"},
                "conflict_safety": {"score": 4, "rationale": "does not alter frozen charter"},
                "prompt_blast_radius": {"score": 3, "rationale": "bounded verification rule"},
            },
        }
        from app.services.auto_dream import _soul_candidate_id

        candidate["memory_gate_review"]["candidate_id"] = _soul_candidate_id(candidate)
        return candidate

    @staticmethod
    def soul_v2_with_principle(name: str = "Test") -> str:
        return (
            "---\n"
            "schema: hive.soul.v2\n"
            "role: agent_identity\n"
            "---\n\n"
            f"# Soul — {name}\n\n"
            '<soul_identity frozen="true">\n'
            f"<name>{name}</name>\n"
            "</soul_identity>\n\n"
            '<soul_principle id="verification-loop" stability="stable">\n'
            "Always verify after material changes.\n"
            "<source_refs>\n"
            '<source_ref ref="t3:memory/t3/worker.md#block:soul-principle-1" />\n'
            "</source_refs>\n"
            "<applies_when>Code, docs, prompt, or memory behavior changed.</applies_when>\n"
            "<does_not_apply_when>User explicitly asks for discussion only.</does_not_apply_when>\n"
            "</soul_principle>\n"
        )


class TestApplyDreamDecisions:
    def _scaffold(self, tmp_path: Path) -> uuid.UUID:
        agent_id = uuid.uuid4()
        agent_dir = tmp_path / str(agent_id)
        (agent_dir / "memory").mkdir(parents=True)
        (agent_dir / "soul.md").write_text("# Soul\n\n## Identity\n- Name: Test\n", encoding="utf-8")
        return agent_id

    def test_commits_reviewed_soul_candidate_package_as_exact_next_file(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        next_soul = TestSoulCandidatePackage.soul_v2_with_principle()
        decision = {
            "reasoning": "identity-grade verification rule",
            "soul_candidate": TestSoulCandidatePackage.passing_candidate(soul_next=next_soul),
        }
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions(agent_id, decision)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert soul == next_soul
        assert report["soul_candidate_committed"] == 1
        assert report["soul_added"] == 1

        candidate_root = tmp_path / str(agent_id) / "memory" / ".staging" / "soul_candidates"
        candidate_dirs = list(candidate_root.iterdir())
        assert len(candidate_dirs) == 1
        candidate_dir = candidate_dirs[0]
        assert (candidate_dir / "soul_pitch.md").exists()
        assert (candidate_dir / "soul_patch.md").exists()
        assert (candidate_dir / "soul.md.next").read_text(encoding="utf-8") == next_soul
        manifest = json.loads((candidate_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["schema"] == "soul_candidate_package.v1"
        assert manifest["status"] == "committed"
        assert manifest["target_path"] == "soul.md"
        assert manifest["memory_gate_review"]["recommendation"] == "promote"
        assert manifest["memory_gate_review"]["reviewer"] == "soul_memory_gate_agent"
        assert not (tmp_path / str(agent_id) / "evolution" / "evolution_ledger.jsonl").exists()
        audit_path = tmp_path / str(agent_id) / "memory" / "distillation_audit.jsonl"
        audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        audit = audit_rows[-1]
        assert audit["stage"] == "soul_candidate"
        assert audit["outcome"] == "committed"
        assert audit["reason"] == "candidate passed Platform Soul Gate"
        assert audit["detail"]["candidate_id"] == manifest["candidate_id"]
        assert (
            audit["detail"]["candidate_package_path"] == f"memory/.staging/soul_candidates/{manifest['candidate_id']}"
        )
        assert audit["detail"]["target_path"] == "soul.md"
        assert audit["detail"]["rollback_ref"].startswith("memory/.rollback/soul/")
        assert audit["detail"]["semantic_writer"] == "Dream / Soul Writer Agent"
        assert audit["detail"]["reviewer"] == "Soul Memory Gate Agent"
        assert audit["detail"]["physical_committer"] == "Platform Soul Gate"
        revision_payload = json.loads(
            (tmp_path / str(agent_id) / "runtime_artifacts/asset_transactions/revision.json").read_text()
        )
        assert revision_payload["revision"] == 1
        journal = json.loads(
            (
                tmp_path
                / str(agent_id)
                / "runtime_artifacts/asset_transactions/transactions"
                / revision_payload["last_transaction_id"]
                / "journal.json"
            ).read_text()
        )
        assert journal["status"] == "committed"
        assert {operation["path"] for operation in journal["operations"]} >= {
            "soul.md",
            "memory/distillation_audit.jsonl",
            f"memory/.staging/soul_candidates/{manifest['candidate_id']}/manifest.json",
            f"memory/.rollback/soul/{manifest['candidate_id']}.soul.md.before",
        }

    def test_self_reviewed_soul_candidate_is_held(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        before = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        next_soul = TestSoulCandidatePackage.soul_v2_with_principle()
        candidate = TestSoulCandidatePackage.passing_candidate(soul_next=next_soul)
        candidate["review"] = candidate.pop("memory_gate_review")
        candidate["review"]["source"] = "dream_writer_self_review"
        decision = {"reasoning": "self-reviewed candidate must not commit", "soul_candidate": candidate}

        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions(agent_id, decision)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert soul == before
        assert report["soul_candidate_committed"] == 0
        assert report["soul_candidate_held"] == 1
        assert not (tmp_path / str(agent_id) / "evolution" / "evolution_ledger.jsonl").exists()
        audit_path = tmp_path / str(agent_id) / "memory" / "distillation_audit.jsonl"
        audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        audit = audit_rows[-1]
        assert audit["stage"] == "soul_candidate"
        assert audit["outcome"] == "held"
        assert audit["reason"] in {
            "Soul Memory Gate review candidate_id mismatch",
            "memory_gate_review must come from Soul Memory Gate, not Dream self-review",
        }
        assert audit["detail"]["target_path"] == "soul.md"
        assert audit["detail"]["rollback_ref"] is None

    def test_legacy_soul_promotions_are_held_not_written(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        before = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        decision = {
            "soul_promotions": [
                {
                    "content": "always prefer concise output",
                    "source_file": "t3/user.md",
                    "source_refs": ["t3:memory/t3/user.md#entry:a", "t3:memory/t3/user.md#entry:b"],
                    "evidence": "system_observed",
                    "section": "Learned Behaviors",
                    "reason": "legacy schema must not write soul",
                }
            ]
        }
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions(agent_id, decision)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert soul == before
        assert "always prefer concise output" not in soul
        assert report["legacy_soul_promotions_held"] == 1
        assert report["soul_added"] == 0

    def test_t3_merges_drop_duplicate_lines(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        feedback_path = tmp_path / str(agent_id) / "memory" / "t3" / "user.md"
        feedback_path.parent.mkdir(parents=True, exist_ok=True)
        feedback_path.write_text(
            "# T3 User\n\n"
            "- [2026-04-10] user prefers concise output\n"
            "- [2026-04-12] prefers concise output please\n"
            "- [2026-04-14] wants concise answers\n",
            encoding="utf-8",
        )
        original_content = feedback_path.read_text(encoding="utf-8")
        decision = {
            "t3_merges": [
                {
                    "file": "t3/user.md",
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
        assert new_content == original_content
        assert report["t3_merges_applied"] == 0
        assert report["t3_patch_candidates_held"] == 1

    def test_t3_merge_concern_is_held_when_keep_is_synthesized(self, tmp_path: Path) -> None:
        """Dream may detect a synthesized merge, but it must not write accepted T3."""
        agent_id = self._scaffold(tmp_path)
        feedback_path = tmp_path / str(agent_id) / "memory" / "t3" / "user.md"
        feedback_path.parent.mkdir(parents=True, exist_ok=True)
        feedback_path.write_text(
            "# T3 User\n\n"
            "- [2026-04-10] User rejected emoji in responses\n"
            "- [2026-04-12] User rejected adding emojis to answer\n"
            "- [2026-04-14] User corrected agent's emoji use again\n",
            encoding="utf-8",
        )
        original_content = feedback_path.read_text(encoding="utf-8")
        decision = {
            "t3_merges": [
                {
                    "file": "t3/user.md",
                    # Synthesized canonical keep — NOT present verbatim in the file.
                    "keep": "- [2026-04-14] User rejected emoji in responses (3rd confirmation)",
                    "drop": [
                        "User rejected emoji in responses",
                        "User rejected adding emojis to answer",
                        "User corrected agent's emoji use again",
                    ],
                    "reason": "3 restatements; keep merged context",
                }
            ]
        }
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions(agent_id, decision)

        new_content = feedback_path.read_text(encoding="utf-8")
        assert new_content == original_content
        assert "User rejected emoji in responses (3rd confirmation)" not in new_content
        assert report["t3_merges_applied"] == 0
        assert report["t3_patch_candidates_held"] == 1

    def test_contradictions_kept_new_drops_old(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        feedback_path = tmp_path / str(agent_id) / "memory" / "t3" / "user.md"
        feedback_path.parent.mkdir(parents=True, exist_ok=True)
        feedback_path.write_text(
            "# T3 User\n\n- [2026-04-10] use Japanese for responses\n- [2026-04-14] please respond in Chinese always\n",
            encoding="utf-8",
        )
        original_content = feedback_path.read_text(encoding="utf-8")
        decision = {
            "t3_contradictions": [
                {
                    "file": "t3/user.md",
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
        assert new_content == original_content
        assert report["contradictions_resolved"] == 0
        assert report["t3_patch_candidates_held"] == 1

    def test_preservation_flags_persisted_to_sidecar(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        decision = {
            "preservation_flags": [
                {
                    "file": "t3/user.md",
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
        assert flags[0]["file"] == "t3/user.md"
        assert "Never skip verification" in flags[0]["content"]

    def test_preservation_flags_dedup_on_reapply(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        decision = {
            "preservation_flags": [
                {"file": "t3/user.md", "content": "principle A", "reason": "x"},
            ]
        }
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            _apply_dream_decisions(agent_id, decision)
            report2 = _apply_dream_decisions(agent_id, decision)
            flags = _read_preservation_flags(agent_id)

        assert report2["preservation_flags_added"] == 0  # already present
        assert len(flags) == 1

    def test_preservation_sidecar_never_discards_older_foundational_flags(self, tmp_path: Path) -> None:
        agent_id = self._scaffold(tmp_path)
        flags = [
            {
                "file": "t3/worker.md",
                "content": f"foundational-principle-{index}",
                "reason": "owner-governed identity evidence",
            }
            for index in range(75)
        ]

        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            _write_preservation_flags(agent_id, flags)
            persisted = _read_preservation_flags(agent_id)

        assert persisted == flags


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

    def _candidate_decision(self, content: str) -> dict:
        next_soul = (
            "---\n"
            "schema: hive.soul.v2\n"
            "role: agent_identity\n"
            "---\n\n"
            "# Soul — Radar\n\n"
            '<soul_identity frozen="true">\n'
            "<name>Radar</name>\n"
            "<mission>Scan the exhibition floor three times daily and proactively push fresh leads.</mission>\n"
            "</soul_identity>\n\n"
            '<soul_principle id="lead-brief-rule" stability="stable">\n'
            f"{content}\n"
            "<source_refs>\n"
            '<source_ref ref="t3:memory/t3/capabilities.md#entry:a" />\n'
            '<source_ref ref="t3:memory/t3/capabilities.md#entry:b" />\n'
            "</source_refs>\n"
            "<applies_when>Preparing lead briefs.</applies_when>\n"
            "<does_not_apply_when>The owner explicitly changes the scan cadence.</does_not_apply_when>\n"
            "</soul_principle>\n"
        )
        candidate = TestSoulCandidatePackage.passing_candidate(
            soul_next=next_soul,
            source_refs=[
                "t3:memory/t3/capabilities.md#entry:a",
                "t3:memory/t3/capabilities.md#entry:b",
            ],
        )
        candidate["soul_patch_md"] = (
            "# Soul Patch\n\n"
            '<soul_principle id="lead-brief-rule" stability="stable">\n'
            f"{content}\n"
            "<source_refs>\n"
            '<source_ref ref="t3:memory/t3/capabilities.md#entry:a" />\n'
            '<source_ref ref="t3:memory/t3/capabilities.md#entry:b" />\n'
            "</source_refs>\n"
            "</soul_principle>\n"
        )
        from app.services.auto_dream import _soul_candidate_id

        candidate["memory_gate_review"]["candidate_id"] = _soul_candidate_id(candidate)
        return {"reasoning": "candidate patch", "soul_candidate": candidate}

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

        decision = self._candidate_decision("Disable the three-times-daily scan; scan only once per week on Friday.")
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions_unlocked(agent_id, decision, contradiction_judge=judge)

        # The judge must have received the FROZEN Mission/charter, not T3.
        assert "three times daily" in seen["frozen_charter"].lower()
        assert "Frozen Owner Agency Charter" in seen["frozen_charter"]
        assert "once per week on Friday" in seen["content"]
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

        decision = self._candidate_decision("Always include a one-line summary at the top of each lead brief.")
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions_unlocked(agent_id, decision, contradiction_judge=judge)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "Always include a one-line summary at the top of each lead brief." in soul
        assert report["soul_added"] == 1
        assert report["soul_contradicted_frozen"] == 0
        assert report["soul_candidate_committed"] == 1

    def test_mechanical_overlap_only_holds_for_semantic_review_when_judge_unavailable(self, tmp_path: Path) -> None:
        # No judge injected: mechanical overlap is an observation only. The
        # candidate is preserved in the held package for retry, never classified
        # as a semantic contradiction by the platform.
        from app.services.auto_dream import _apply_dream_decisions_unlocked

        agent_id = self._scaffold(tmp_path)
        decision = self._candidate_decision("Disable the three-times-daily scan; only scan once weekly on Friday.")
        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            report = _apply_dream_decisions_unlocked(agent_id, decision, contradiction_judge=None)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "once weekly on Friday" not in soul
        assert report["soul_added"] == 0
        assert report["soul_candidate_held"] == 1
        assert report["soul_contradicted_frozen"] == 0
        review_files = list((tmp_path / str(agent_id) / "memory/.staging/soul_candidates").glob("*/review.md"))
        assert len(review_files) == 1
        review = review_files[0].read_text(encoding="utf-8")
        assert "semantic_review_unavailable" in review
        assert "mechanical_overlap_signal" in review

    @pytest.mark.asyncio
    async def test_repeated_feedback_is_not_mechanically_promoted_to_soul(self, tmp_path: Path) -> None:
        from app.services.auto_dream import run_dream

        agent_id = self._scaffold(tmp_path)
        tenant_id = uuid.uuid4()
        t3_path = tmp_path / str(agent_id) / "memory" / "t3" / "user.md"
        t3_path.parent.mkdir(parents=True, exist_ok=True)
        feedback = (
            "# T3 User\n\n"
            "- [2026-06-01] Disable the three-times-daily scan; scan only once per week on Friday.\n"
            "- [2026-06-02] Disable the three-times-daily scan; scan only once per week on Friday.\n"
            "- [2026-06-03] Disable the three-times-daily scan; scan only once per week on Friday.\n"
        )
        t3_path.write_text(feedback, encoding="utf-8")

        with (
            patch("app.services.auto_dream.get_settings") as mock_settings,
            patch("app.services.auto_dream._dream_llm_consolidate", return_value=None),
            patch("app.services.auto_dream._build_frozen_mission_judge") as mock_judge_builder,
        ):
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            result = await run_dream(agent_id, tenant_id)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "once per week on Friday" not in soul
        assert result["added"] == 0
        assert result["repeated_feedback_held"] == 0
        mock_judge_builder.assert_not_called()

    @pytest.mark.asyncio
    async def test_production_llm_judge_is_built_and_applied(self, tmp_path: Path) -> None:
        # Proves the AI-Native L1 path is wired: _build_frozen_mission_judge runs
        # the per-item LLM judge in the async layer, and the resulting verdict is
        # what _apply_dream_decisions enforces — not the safety blocker fallback.
        from app.services.auto_dream import _apply_dream_decisions, _build_frozen_mission_judge

        agent_id = self._scaffold(tmp_path)
        tenant_id = uuid.uuid4()
        decision = self._candidate_decision("Switch the cadence to monthly reviews only.")
        judged: list[tuple[str, str]] = []

        async def fake_judge(model_config, frozen_charter, content, *, coverage_path=None):
            assert coverage_path is not None
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
        assert "Switch the cadence to monthly reviews only." in judged[0][1]
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
        t3_path = mem_dir / "t3" / "user.md"
        t3_path.parent.mkdir(parents=True, exist_ok=True)
        t3_path.write_text("# T3 User\n\n" + "\n".join(lines) + "\n", encoding="utf-8")

        # Write preservation sidecar flagging the foundational line.
        import json

        (mem_dir / ".preservation.json").write_text(
            json.dumps({"protected": [{"file": "t3/user.md", "content": "foundational principle"}]}),
            encoding="utf-8",
        )

        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            _consolidate_t3_files(agent_id)

        final = t3_path.read_text(encoding="utf-8")
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
        t3_path = mem_dir / "t3" / "user.md"
        t3_path.parent.mkdir(parents=True, exist_ok=True)
        t3_path.write_text("# T3 User\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
        original_content = t3_path.read_text(encoding="utf-8")

        with patch("app.services.auto_dream.get_settings") as mock_settings:
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            stats = _consolidate_t3_files(agent_id)

        final = t3_path.read_text(encoding="utf-8")
        assert final == original_content
        assert sum(stats.values()) == 0


@pytest.mark.asyncio
class TestRunDreamIntegration:
    async def test_run_dream_holds_semantic_work_when_llm_unavailable(self, tmp_path: Path) -> None:
        """Provider failure may rebuild indexes, but must not mutate or cap semantic T3."""
        from app.services.auto_dream import dream_state_snapshot, run_dream

        agent_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        mem_dir = tmp_path / str(agent_id) / "memory"
        (mem_dir / "t3").mkdir(parents=True)
        (tmp_path / str(agent_id) / "soul.md").write_text("# Soul\n\n## Identity\n", encoding="utf-8")
        (mem_dir / "t3" / "user.md").write_text("# T3 User\n\n- [2026-04-10] prefer concise\n", encoding="utf-8")

        original_t3 = (mem_dir / "t3" / "user.md").read_text(encoding="utf-8")
        with (
            patch("app.services.auto_dream.get_settings") as mock_settings,
            patch("app.services.auto_dream._dream_llm_consolidate", return_value=None) as mock_llm,
            patch(
                "app.services.auto_dream._read_all_t3",
                return_value={"memory/profiles/owner.md": "# Owner Profile\n\n- [2026-04-10] prefer concise\n"},
            ),
            patch("app.services.auto_dream._consolidate_t3_files") as semantic_cleanup,
        ):
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            result = await run_dream(agent_id, tenant_id)

        mock_llm.assert_called_once()
        semantic_cleanup.assert_not_called()
        assert result["status"] == "degraded"
        assert result["retryable"] is True
        assert result["reason"] == "semantic_consolidator_unavailable"
        assert result["coverage"]["reviewed"] == 0
        assert result["coverage"]["total"] == 2
        assert (mem_dir / "t3" / "user.md").read_text(encoding="utf-8") == original_t3
        assert dream_state_snapshot(agent_id)["version"] == 0

    async def test_run_dream_applies_llm_decision_when_available(self, tmp_path: Path) -> None:
        """If LLM returns a decision, run_dream applies it before pure-Python cleanup."""
        from app.services.auto_dream import run_dream

        agent_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        mem_dir = tmp_path / str(agent_id) / "memory"
        (mem_dir / "t3").mkdir(parents=True)
        (tmp_path / str(agent_id) / "soul.md").write_text("# Soul\n\n## Identity\n", encoding="utf-8")
        (mem_dir / "t3" / "user.md").write_text(
            "# T3 User\n\n- [2026-04-10] user always prefers concise output\n",
            encoding="utf-8",
        )

        fake_decision = {
            "reasoning": "clear repeated preference, promoting through candidate package",
            "soul_candidate": TestSoulCandidatePackage.passing_candidate(
                soul_next=(
                    "---\n"
                    "schema: hive.soul.v2\n"
                    "role: agent_identity\n"
                    "---\n\n"
                    "# Soul — Test\n\n"
                    '<soul_user_model id="concise-output" stability="stable">\n'
                    "Always prefer concise output.\n"
                    "<source_refs>\n"
                    '<source_ref ref="t3:memory/t3/user.md#entry:a" />\n'
                    '<source_ref ref="t3:memory/t3/user.md#entry:b" />\n'
                    "</source_refs>\n"
                    "<applies_when>User asks for status, implementation summaries, or next steps.</applies_when>\n"
                    "<does_not_apply_when>User explicitly asks for exhaustive detail.</does_not_apply_when>\n"
                    "</soul_user_model>\n"
                ),
                source_refs=["t3:memory/t3/user.md#entry:a", "t3:memory/t3/user.md#entry:b"],
            ),
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
        assert "schema: hive.soul.v2" in soul
        assert "Always prefer concise output." in soul

    async def test_run_dream_does_not_build_frozen_judge_without_llm_decision(self, tmp_path: Path) -> None:
        from app.services.auto_dream import run_dream

        agent_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        agent_dir = tmp_path / str(agent_id)
        mem_dir = agent_dir / "memory"
        (mem_dir / "t3").mkdir(parents=True)
        (agent_dir / "soul.md").write_text(TestDreamFrozenMissionGate._MISSION_SOUL, encoding="utf-8")
        feedback = (
            "# T3 User\n\n"
            "- [2026-06-01] Disable the three-times-daily scan; scan only once per week on Friday.\n"
            "- [2026-06-02] Disable the three-times-daily scan; scan only once per week on Friday.\n"
            "- [2026-06-03] Disable the three-times-daily scan; scan only once per week on Friday.\n"
        )
        (mem_dir / "t3" / "user.md").write_text(feedback, encoding="utf-8")

        with (
            patch("app.services.auto_dream.get_settings") as mock_settings,
            patch("app.services.auto_dream._dream_llm_consolidate", return_value=None),
            patch("app.services.auto_dream._build_frozen_mission_judge") as mock_judge_builder,
        ):
            mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
            result = await run_dream(agent_id, tenant_id)

        soul = (tmp_path / str(agent_id) / "soul.md").read_text(encoding="utf-8")
        assert "once per week on Friday" not in soul
        assert result["added"] == 0
        assert result["soul_contradicted_frozen"] == 0
        mock_judge_builder.assert_not_called()


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

    def test_system_prompt_loads_dream_protocol_template(self) -> None:
        assert "Dream — Soul Reconsolidation Protocol" in _AUTO_DREAM_SYSTEM_PROMPT
        assert "You are not the T3 writer" in _AUTO_DREAM_SYSTEM_PROMPT
        assert "Platform Gate" in _AUTO_DREAM_SYSTEM_PROMPT

    def test_system_prompt_excludes_legacy_worker_output_tag(self) -> None:
        assert "[DREAM:complete]" not in _AUTO_DREAM_SYSTEM_PROMPT
        assert "[DREAM:noop]" not in _AUTO_DREAM_SYSTEM_PROMPT


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
        # Soul v2 block targets are explicitly listed; old section buckets are not the write contract.
        assert "soul_principle" in t
        assert "soul_user_model" in t
        assert "soul_quality_bar" in t
        assert "soul_redline" in t
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
        assert "DO NOT rewrite T3" in t
        assert "DO NOT flag for preservation" in t

    def test_json_schema_lists_canonical_soul_candidate_artifacts(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        for key in (
            '"reasoning"',
            '"soul_candidate"',
            '"soul_pitch_md"',
            '"soul_patch_md"',
            '"soul_md_next"',
            '"t3_patch_concerns"',
            '"preservation_flags"',
        ):
            assert key in t, f"schema missing key: {key}"
        assert '"review"' not in t
        assert "Do not review, approve, or score your own soul_candidate" in t
        assert '"soul_promotions"' not in t
        assert '"section"' not in t

    def test_soul_block_type_enum_is_complete(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "soul_principle|soul_user_model|soul_quality_bar|soul_redline" in t

    def test_source_file_enum_is_complete(self) -> None:
        t = _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE
        assert "memory/self/self.md|memory/profiles/owner.md|memory/profiles/collaborators.md" in t
        assert "memory/knowledge/<slug>.md|memory/milestones/<slug>.md" in t

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
                "memory/profiles/owner.md": "# Owner Profile\n\n- [2026-04-10] prefers concise output",
                "memory/knowledge/tdd-first.md": "# TDD First\n\n- [2026-04-11] TDD-first",
            },
        )
        assert "Agent: Alice" in out
        assert "Name: Alice" in out
        assert "prefers concise output" in out
        assert "TDD-first" in out
        # XML structure survives formatting.
        assert "<section_selection_matrix>" in out
        assert "<few_shot_example_1>" in out

    def test_dream_prompt_includes_candidate_evidence_digest(self) -> None:
        out = _build_dream_consolidation_user_prompt(
            "Alice",
            "# Soul\n",
            {"t3/capabilities.md": "- [2026-06-17] Use governed memory writes"},
            candidate_evidence=[
                {
                    "candidate_id": "cand-heartbeat-1",
                    "source": "heartbeat_reflection",
                    "container": "memory_candidate",
                    "lesson": "Direct writes to evolution are audit violations; use governed candidates.",
                    "source_refs": ["heartbeat_session:hb-1", "chat_message:msg-1"],
                    "decision": "held",
                    "reason": "needs repeated evidence",
                }
            ],
        )

        assert "<candidate_evidence>" in out
        assert "cand-heartbeat-1" in out
        assert "heartbeat_reflection" in out
        assert "chat_message:msg-1" in out
        assert "lineage.md" not in out

    def test_dream_prompt_does_not_cap_retirement_or_candidate_evidence(self) -> None:
        retirement = [
            {"entry_id": f"retire-{index}", "heat": index, "filename": "owner.md", "content": f"entry-{index}"}
            for index in range(15)
        ]
        candidates = [
            {
                "candidate_id": f"candidate-{index}",
                "source": "learning_brain",
                "container": "skill_candidate",
                "lesson": ("lesson " * 100) + f"LESSON-TAIL-{index}",
                "source_refs": [f"t0://source/{index}/{ref}" for ref in range(8)],
                "decision": "held",
                "reason": ("reason " * 60) + f"REASON-TAIL-{index}",
            }
            for index in range(16)
        ]

        out = _build_dream_consolidation_user_prompt(
            "Alice",
            "# Soul\n",
            {"memory/profiles/owner.md": "owner evidence"},
            retirement_candidates=retirement,
            candidate_evidence=candidates,
        )

        assert "retire-14" in out
        assert "candidate-15" in out
        assert "LESSON-TAIL-15" in out
        assert "REASON-TAIL-15" in out
        assert "t0://source/15/7" in out

    def test_builder_full_fidelity_under_budget(self) -> None:
        """蒸馏器核查 (docs/agent-lifecycle-cc-alignment.md §3.6): the dream
        consolidator decides soul promotions — it must see FULL T3/soul when
        the total fits the input budget. Per-section caps engage only over
        budget (same philosophy as compaction P0 / heartbeat C1)."""
        soul = "S" * 5_000  # over the old per-section cap (3K), under total budget
        t3 = {"memory/profiles/owner.md": "F" * 6_000}  # over the old per-file cap (4K)

        out = _build_dream_consolidation_user_prompt("A", soul, t3)

        assert "truncated" not in out
        assert "S" * 5_000 in out  # soul intact
        assert "F" * 6_000 in out  # T3 file intact

    def test_builder_preserves_tail_evidence_and_emits_hash_manifest_over_old_budget(self) -> None:
        from app.services.auto_dream import _DREAM_INPUT_TOTAL_BUDGET_CHARS

        tail_contradiction = "TAIL-CONTRADICTION-MUST-BE-REVIEWED"
        big = "X" * (_DREAM_INPUT_TOTAL_BUDGET_CHARS + 10_000) + tail_contradiction
        out = _build_dream_consolidation_user_prompt("A", "soul", {"memory/profiles/owner.md": big})

        assert "truncated" not in out
        assert tail_contradiction in out
        assert '<dream_input_manifest schema_version="dream.coverage.v1">' in out
        assert 'path="memory/profiles/owner.md"' in out
        assert 'sha256="' in out

    def test_coverage_receipt_must_match_every_offered_file_hash(self) -> None:
        from app.services.auto_dream import _build_dream_input_manifest, _validate_dream_coverage_receipt

        manifest = _build_dream_input_manifest("# Soul", {"memory/t3/user.md": "tail truth"})
        complete = {
            "coverage_receipt": [
                {"path": item["path"], "sha256": item["sha256"], "status": "reviewed"} for item in manifest
            ]
        }
        missing_tail = {"coverage_receipt": complete["coverage_receipt"][:-1]}

        assert _validate_dream_coverage_receipt(complete, manifest) == []
        assert "missing:memory/t3/user.md" in _validate_dream_coverage_receipt(missing_tail, manifest)

    def test_builder_handles_no_t3_files(self) -> None:
        out = _build_dream_consolidation_user_prompt("A", "soul", {})
        assert "(no T3 files)" in out


@pytest.mark.asyncio
async def test_live_dream_consolidator_holds_decision_without_complete_coverage(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    from app.services import auto_dream, llm_client, memory_service

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_root = tmp_path / str(agent_id)
    agent_root.mkdir(parents=True)
    (agent_root / "soul.md").write_text("# Soul", encoding="utf-8")

    class FakeClient:
        async def stream(self, **_kwargs):
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "reasoning": "I reviewed only one section.",
                        "soul_candidate": None,
                        "t3_patch_concerns": [],
                        "preservation_flags": [],
                    }
                )
            )

        async def close(self):
            return None

    model_config = {"provider": "openai", "model": "test", "api_key": "test"}
    with (
        patch.object(auto_dream, "get_settings", return_value=SimpleNamespace(AGENT_DATA_DIR=str(tmp_path))),
        patch.object(memory_service, "_get_summary_model_config", AsyncMock(return_value=model_config)),
        patch.object(llm_client, "create_llm_client_from_config", return_value=FakeClient()),
        patch.object(auto_dream, "_write_dream_audit_event", AsyncMock()) as audit,
    ):
        decision = await auto_dream._dream_llm_consolidate(
            agent_id,
            tenant_id,
            {"memory/profiles/owner.md": "tail truth"},
            "Agent",
        )

    assert decision is None
    assert audit.await_args.kwargs["outcome"] == "held"
    assert audit.await_args.kwargs["reason"].startswith("incomplete_coverage:")


def test_dream_consolidator_template_is_loaded_into_prompt() -> None:
    from app.services.auto_dream import _build_dream_consolidation_user_prompt

    out = _build_dream_consolidation_user_prompt(
        "Alice",
        "# Soul\n",
        {"memory/profiles/owner.md": "- [2026-05-02] User requires evidence-tagged memory"},
    )

    assert "memory_promotion_candidate" not in out
    assert "evolution/evolution_ledger.jsonl" not in out
    assert "soul_candidate" in out
    assert "memory/distillation_audit.jsonl" in out
    assert "source_refs" in out
    assert "rollback_ref" in out
    assert "dream may propose candidates" in out.lower()


def test_dream_fact_backups_use_staging_path(tmp_path: Path) -> None:
    from app.services import auto_dream

    agent_id = uuid.uuid4()
    legacy_dir = tmp_path / str(agent_id) / "memory" / "dream_backups"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "dream_backup_legacy.json").write_text("[]", encoding="utf-8")

    with patch("app.services.auto_dream.get_settings") as mock_settings:
        mock_settings.return_value.AGENT_DATA_DIR = str(tmp_path)
        auto_dream._backup_facts(agent_id, [{"content": "fact"}])

    staging_dir = tmp_path / str(agent_id) / "memory" / ".staging" / "dream_backups"
    backups = list(staging_dir.glob("dream_backup_*.json"))
    assert backups
    assert not legacy_dir.exists()


def test_auto_dream_does_not_use_legacy_evolution_ledger_for_soul_writeback() -> None:
    source = Path("app/services/auto_dream.py").read_text(encoding="utf-8")

    assert "record_memory_promotion_candidate" not in source
    assert "record_memory_promotion_decision" not in source
    assert "load_evolution_ledger" not in source
    assert "sync_t3_to_memory_enhancement" not in source
    assert "_review_blocklist" not in source
    assert "blocklist.md" not in source
