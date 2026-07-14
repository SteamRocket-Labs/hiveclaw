"""Tests for Phase 3 compression alignment (G1-G5)."""

from __future__ import annotations

import time

from app.kernel.engine import (
    _group_messages_by_api_round,
    _MICROCOMPACT_GAP_SECONDS,
    _MICROCOMPACT_KEEP_RECENT,
    _PTL_MAX_RETRIES,
)
from app.services.conversation_summarizer import _SUMMARIZE_SYSTEM_PROMPT
from app.services.llm_client import LLMMessage


# ── G1: LLMMessage created_at ──


class TestLLMMessageTimestamp:
    def test_default_timestamp(self) -> None:
        before = time.time()
        msg = LLMMessage(role="user", content="test")
        after = time.time()
        assert before <= msg.created_at <= after

    def test_explicit_timestamp(self) -> None:
        msg = LLMMessage(role="user", content="test", created_at=1000.0)
        assert msg.created_at == 1000.0


# ── G1: Microcompact constants ──


class TestMicrocompactConstants:
    def test_gap_60_minutes(self) -> None:
        assert _MICROCOMPACT_GAP_SECONDS == 3600

    def test_keep_recent_5(self) -> None:
        assert _MICROCOMPACT_KEEP_RECENT == 5


def test_microcompact_only_replaces_results_with_truthful_durable_artifact_refs(tmp_path) -> None:
    from app.kernel.engine import _maybe_evict_tool_result, _microcompact_artifact_replacement

    raw = "old tool evidence\n" * 5_000
    inline = _maybe_evict_tool_result("run_code", "call_old", raw, eviction_dir=tmp_path)

    replacement = _microcompact_artifact_replacement(inline)

    assert replacement is not None
    assert "artifact_ref=workspace/tool_results/call_old.txt" in replacement
    assert "sha256=" in replacement
    assert f"char_range=0-{len(raw)}" in replacement
    assert _microcompact_artifact_replacement("unpersisted old tool evidence") is None


# ── G3: PTL constants ──


class TestPTLConstants:
    def test_max_retries_3(self) -> None:
        assert _PTL_MAX_RETRIES == 3


# ── G3: _group_messages_by_api_round ──


class TestGroupMessagesByApiRound:
    def test_single_round(self) -> None:
        msgs = [
            LLMMessage(role="user", content="hi"),
            LLMMessage(role="assistant", content="hello"),
        ]
        groups = _group_messages_by_api_round(msgs)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_multiple_rounds(self) -> None:
        msgs = [
            LLMMessage(role="user", content="q1"),
            LLMMessage(role="assistant", content="a1"),
            LLMMessage(role="user", content="q2"),
            LLMMessage(role="assistant", content="a2"),
        ]
        groups = _group_messages_by_api_round(msgs)
        assert len(groups) == 2

    def test_tool_calling_round(self) -> None:
        """Assistant with tool_calls doesn't end a round."""
        msgs = [
            LLMMessage(role="user", content="search"),
            LLMMessage(role="assistant", content="", tool_calls=[{"id": "tc1", "function": {"name": "search"}}]),
            LLMMessage(role="tool", tool_call_id="tc1", content="results"),
            LLMMessage(role="assistant", content="Here are the results"),
        ]
        groups = _group_messages_by_api_round(msgs)
        assert len(groups) == 1
        assert len(groups[0]) == 4

    def test_empty(self) -> None:
        assert _group_messages_by_api_round([]) == []

    def test_incomplete_round(self) -> None:
        """Trailing messages without final assistant go into last group."""
        msgs = [
            LLMMessage(role="user", content="q1"),
            LLMMessage(role="assistant", content="a1"),
            LLMMessage(role="user", content="q2"),
        ]
        groups = _group_messages_by_api_round(msgs)
        assert len(groups) == 2
        assert len(groups[1]) == 1  # incomplete round


# ── G5: Summarize prompt 11-section ──


class TestSummarizePrompt:
    def test_has_11_sections(self) -> None:
        sections = [
            "Primary Request and Intent",
            "Key Technical Concepts & Decisions",
            "Files and Code Sections",
            "Problem Solving",
            "Errors and Fixes",
            "All User Messages",
            "User Preferences",
            "Tool Outcomes",
            "Pending Tasks",
            "Current Work",
            "Next Step",
        ]
        for section in sections:
            assert section in _SUMMARIZE_SYSTEM_PROMPT, f"Missing section: {section}"

    def test_old_sections_removed(self) -> None:
        assert "Task Ledger" not in _SUMMARIZE_SYSTEM_PROMPT
        assert "Narrative Snapshot" not in _SUMMARIZE_SYSTEM_PROMPT
        assert "Code Snapshot" not in _SUMMARIZE_SYSTEM_PROMPT

    def test_analysis_step_5(self) -> None:
        assert "problem-solving" in _SUMMARIZE_SYSTEM_PROMPT.lower()

    def test_memory_system_mention(self) -> None:
        # PR-18 rewrote _SUMMARIZE_SYSTEM_PROMPT with XML structure. The
        # separation-of-concerns (memory extraction is a separate pipeline)
        # is preserved in the new <role> block — normalize whitespace since
        # the phrase is word-wrapped.
        normalized = " ".join(_SUMMARIZE_SYSTEM_PROMPT.lower().split())
        assert "memory extraction runs as a separate pipeline" in normalized


# G5's mechanical ``_extract_summary`` 11-section fallback was removed as dead
# code (B-6): it had no live caller — compaction summarization runs through the
# LLM ``_SUMMARIZE_SYSTEM_PROMPT`` path covered above. No replacement tests are
# needed; the deleted helper is no longer importable.
