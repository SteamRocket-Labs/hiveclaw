"""Tests for _SUMMARIZE_SYSTEM_PROMPT (PR-18).

Conversation summarization runs when context approaches its budget limit.
The summary is the ONLY state the next turn sees for compressed history,
so format regressions silently corrupt long-running sessions. Lock down
the scratchpad pattern, tool-rejection guardrail, required 11-field
structure, and good/bad examples.
"""

from __future__ import annotations

import pytest

from app.services.conversation_summarizer import _SUMMARIZE_SYSTEM_PROMPT


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return _SUMMARIZE_SYSTEM_PROMPT


class TestPromptStructure:
    def test_xml_tags_present(self, prompt_text: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<tool_contract>",
            "<scratchpad_pattern>",
            "<analysis_instructions>",
            "<summary_format>",
            "<good_summary_example>",
            "<bad_summary_examples>",
            "<hard_rules>",
        ]:
            assert tag in prompt_text, f"missing tag: {tag}"

    def test_role_distinguishes_summary_from_memory(self, prompt_text: str) -> None:
        # Must explicitly say this is NOT long-term memory.
        lowered = prompt_text.lower()
        assert "not generating long-term memory" in lowered
        assert "session-state preservation" in lowered


class TestToolContract:
    def test_explicit_tool_rejection(self, prompt_text: str) -> None:
        assert "TEXT ONLY" in prompt_text
        assert "Do NOT call any tools" in prompt_text
        assert "REJECTED" in prompt_text and "WASTE" in prompt_text

    def test_names_specific_forbidden_tools(self, prompt_text: str) -> None:
        for tool_name in ["read_file", "write_file", "web_search", "execute_code"]:
            assert tool_name in prompt_text


class TestScratchpadPattern:
    def test_explains_analysis_stripped_summary_kept(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "stripped" in lowered
        assert "summary" in lowered and "context budget" in lowered

    def test_motivates_why_split_matters(self, prompt_text: str) -> None:
        # The prompt must explain WHY, not just mandate.
        lowered = prompt_text.lower()
        assert "why this split matters" in lowered or "without a scratchpad" in lowered


class TestAnalysisInstructions:
    def test_five_coverage_areas(self, prompt_text: str) -> None:
        for area in [
            "Chronological",
            "Technical surface",
            "User corrections",
            "Errors and resolutions",
            "Problem-solving trajectory",
        ]:
            assert area in prompt_text, f"missing analysis area: {area}"


class TestSummaryFormat:
    def test_all_eleven_fields_present(self, prompt_text: str) -> None:
        for field in [
            "**Primary Request and Intent:**",
            "**Key Technical Decisions:**",
            "**Files and Code Sections:**",
            "**Problem Solving:**",
            "**Errors and Fixes:**",
            "**All User Messages:**",
            "**User Preferences:**",
            "**Tool Outcomes:**",
            "**Pending Tasks:**",
            "**Current Work:**",
            "**Recovery Context:**",
        ]:
            assert field in prompt_text, f"missing field: {field}"

    def test_empty_fields_use_none_not_omission(self, prompt_text: str) -> None:
        # (none) — explicit rule that empty fields must appear with "(none)".
        assert "(none)" in prompt_text


class TestGoodExample:
    def test_has_concrete_good_example(self, prompt_text: str) -> None:
        assert "<good_summary_example>" in prompt_text
        # The example must contain an actual <summary> payload the LLM can mimic.
        assert "<summary>" in prompt_text
        assert "</summary>" in prompt_text

    def test_example_shows_file_line_references(self, prompt_text: str) -> None:
        # Demonstrates the expected concreteness for Files and Code Sections.
        assert "middleware.py:" in prompt_text

    def test_example_preserves_user_correction_verbatim(self, prompt_text: str) -> None:
        # Example should show user-verbatim corrections preserved.
        assert "too magic" in prompt_text.lower() or "don't touch" in prompt_text.lower()


class TestBadExamples:
    def test_covers_over_compression(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "over-compressed" in lowered or "loses file paths" in lowered

    def test_covers_dropped_corrections(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "drops user corrections" in lowered or "never drop corrections" in lowered

    def test_covers_prose_instead_of_structure(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "narrates prose" in lowered or "prose breaks" in lowered

    def test_covers_session_state_vs_policy(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "policy" in lowered
        assert "session state" in lowered

    def test_covers_missing_current_work_anchor(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "current work" in lowered
        assert "anchor" in lowered or "resume" in lowered


class TestHardRules:
    def test_analysis_summary_only_output(self, prompt_text: str) -> None:
        assert "<analysis>...</analysis><summary>...</summary>" in prompt_text

    def test_same_language_rule(self, prompt_text: str) -> None:
        assert "same language" in prompt_text.lower()
        assert "Do not translate" in prompt_text or "do not translate" in prompt_text.lower()

    def test_preserves_file_paths_verbatim(self, prompt_text: str) -> None:
        assert "file paths" in prompt_text.lower()
        assert "verbatim" in prompt_text.lower()
