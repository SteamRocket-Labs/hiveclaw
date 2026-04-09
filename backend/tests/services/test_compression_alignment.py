"""Tests for Phase 3 compression alignment (G1-G5)."""

from __future__ import annotations

import time

from app.kernel.engine import (
    _group_messages_by_api_round,
    _truncate_head_for_ptl,
    _MICROCOMPACT_GAP_SECONDS,
    _MICROCOMPACT_KEEP_RECENT,
    _PTL_MAX_RETRIES,
)
from app.services.conversation_summarizer import _extract_summary, _SUMMARIZE_SYSTEM_PROMPT
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


# ── G3: _truncate_head_for_ptl ──


class TestTruncateHeadForPTL:
    def test_drops_20_percent(self) -> None:
        msgs = []
        for i in range(10):
            msgs.append(LLMMessage(role="user", content=f"q{i}"))
            msgs.append(LLMMessage(role="assistant", content=f"a{i}"))
        result = _truncate_head_for_ptl(msgs, drop_ratio=0.2)
        # 10 rounds, drop 2 = keep 8 = 16 messages
        assert len(result) == 16

    def test_min_1_dropped(self) -> None:
        msgs = [
            LLMMessage(role="user", content="q1"),
            LLMMessage(role="assistant", content="a1"),
            LLMMessage(role="user", content="q2"),
            LLMMessage(role="assistant", content="a2"),
            LLMMessage(role="user", content="q3"),
            LLMMessage(role="assistant", content="a3"),
        ]
        result = _truncate_head_for_ptl(msgs, drop_ratio=0.2)
        # 3 rounds, drop max(1, 0.6) = 1 → keep 2 = 4 messages
        assert len(result) == 4

    def test_too_few_rounds(self) -> None:
        msgs = [
            LLMMessage(role="user", content="q1"),
            LLMMessage(role="assistant", content="a1"),
        ]
        # ≤2 rounds → return as-is
        result = _truncate_head_for_ptl(msgs, drop_ratio=0.2)
        assert len(result) == len(msgs)

    def test_preserves_recent(self) -> None:
        msgs = []
        for i in range(5):
            msgs.append(LLMMessage(role="user", content=f"q{i}"))
            msgs.append(LLMMessage(role="assistant", content=f"a{i}"))
        result = _truncate_head_for_ptl(msgs, drop_ratio=0.2)
        # Should keep the most recent rounds
        assert result[-1].content == "a4"
        assert result[-2].content == "q4"


# ── G5: Summarize prompt 11-section ──


class TestSummarizePrompt:
    def test_has_11_sections(self) -> None:
        sections = [
            "Primary Request and Intent",
            "Key Technical Decisions",
            "Files and Code Sections",
            "Problem Solving",
            "Errors and Fixes",
            "All User Messages",
            "User Preferences",
            "Tool Outcomes",
            "Pending Tasks",
            "Current Work",
            "Recovery Context",
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
        assert "automatically extracted to the memory system" in _SUMMARIZE_SYSTEM_PROMPT


# ── G5: _extract_summary 11-section ──


class TestExtractSummary:
    def test_has_new_sections(self) -> None:
        msgs = [
            {"role": "user", "content": "Fix the login bug in auth.py"},
            {"role": "assistant", "content": "I tried using session tokens but it failed. Then I fixed it with JWT."},
        ]
        result = _extract_summary(msgs)
        assert "**Primary Request and Intent:**" in result
        assert "**Problem Solving:**" in result
        assert "**Current Work:**" in result
        assert "**Recovery Context:**" in result

    def test_problem_solving_extracted(self) -> None:
        msgs = [
            {"role": "user", "content": "Debug the crash"},
            {"role": "assistant", "content": "I tried restarting the service but that didn't work."},
            {"role": "assistant", "content": "The fix was to update the config."},
        ]
        result = _extract_summary(msgs)
        assert "tried" in result.lower() or "fix" in result.lower()

    def test_empty_messages(self) -> None:
        result = _extract_summary([])
        assert "Primary Request and Intent" in result or "Task Ledger" in result

    def test_recovery_context_pointer(self) -> None:
        msgs = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}]
        result = _extract_summary(msgs)
        assert "logs/" in result
