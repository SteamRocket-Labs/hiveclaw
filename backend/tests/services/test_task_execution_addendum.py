"""Tests for TASK_EXECUTION_ADDENDUM (PR-17).

This addendum is appended to the system prompt whenever a background task
runs autonomously. It shapes how the agent decomposes,
recovers from failures, decides when to stop, and formats its final report.
Parent tooling consumes the structured report, so format drift here silently
breaks downstream reporting.
"""

from __future__ import annotations

import pytest

from app.services.task_executor import TASK_EXECUTION_ADDENDUM


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return TASK_EXECUTION_ADDENDUM


class TestPromptStructure:
    def test_xml_tags_present(self, prompt_text: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<execution_model>",
            "<failure_handling>",
            "<completion_criteria>",
            "<final_report_format>",
            "<good_report_examples>",
            "<bad_report_examples>",
            "<hard_rules>",
        ]:
            assert tag in prompt_text, f"missing tag: {tag}"

    def test_role_demands_execution_not_planning(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "autonomously" in lowered
        assert "verifiable" in lowered
        assert "not a plan" in lowered


class TestExecutionModel:
    def test_kernel_first_then_escalate(self, prompt_text: str) -> None:
        assert "kernel tools" in prompt_text.lower()
        assert "load_skill" in prompt_text
        assert "tool_search" in prompt_text

    def test_external_actions_require_skill_first(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "load the matching skill first" in lowered or "skill first" in lowered


class TestFailureHandling:
    def test_3_strike_rule_differentiates_same_vs_different_errors(self, prompt_text: str) -> None:
        assert "3-strike" in prompt_text.lower() or "3 attempts" in prompt_text
        # Both branches must be distinguished.
        assert "DIFFERENT errors" in prompt_text
        assert "SAME error" in prompt_text

    def test_root_cause_required(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "root cause" in lowered
        assert "do not guess" in lowered or "don't guess" in lowered

    def test_no_fabricated_workarounds(self, prompt_text: str) -> None:
        assert "fabricate workarounds" in prompt_text.lower() or "bypass the original intent" in prompt_text.lower()


class TestCompletionCriteria:
    def test_four_criteria_present(self, prompt_text: str) -> None:
        # executed / concrete evidence / errors named / final format
        assert "EXECUTED" in prompt_text
        assert "CONCRETE evidence" in prompt_text
        assert "Errors encountered" in prompt_text or "errors encountered" in prompt_text.lower()

    def test_concrete_evidence_enumerated(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        for evidence_type in ["file path", "stdout", "url", "tool response"]:
            assert evidence_type in lowered, f"missing evidence type: {evidence_type}"


class TestFinalReportFormat:
    def test_four_sections_listed(self, prompt_text: str) -> None:
        assert "Outcome:" in prompt_text
        assert "Evidence:" in prompt_text
        assert "Blockers:" in prompt_text
        assert "Next Steps:" in prompt_text

    def test_next_steps_omission_rule(self, prompt_text: str) -> None:
        # Next Steps should be omitted entirely when not needed — explicit rule.
        assert "omit the section entirely" in prompt_text.lower() or "omit" in prompt_text.lower()


class TestGoodExamples:
    def test_three_good_examples(self, prompt_text: str) -> None:
        assert "Example A" in prompt_text
        assert "Example B" in prompt_text
        assert "Example C" in prompt_text

    def test_examples_show_concrete_evidence_shapes(self, prompt_text: str) -> None:
        # File:line, pytest output, feishu message_id — concrete verifiable evidence forms.
        assert "middleware.py:" in prompt_text
        assert "pytest" in prompt_text.lower()
        assert "message_id" in prompt_text or "feishu" in prompt_text.lower()

    def test_example_c_shows_real_blocker(self, prompt_text: str) -> None:
        # The blocker example must be specific (missing creds, specific error).
        assert "DATABASE_URL" in prompt_text or "Postgres credentials" in prompt_text


class TestBadExamples:
    def test_covers_plan_instead_of_execution(self, prompt_text: str) -> None:
        assert "Plan instead of execution" in prompt_text or "plan instead of execution" in prompt_text.lower()

    def test_covers_evidence_free_claims(self, prompt_text: str) -> None:
        assert "Evidence-free claims" in prompt_text or "evidence-free" in prompt_text.lower()

    def test_covers_retry_loop(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "retry loop" in lowered or "don't grind" in lowered or "grind" in lowered

    def test_covers_fabricated_verification(self, prompt_text: str) -> None:
        assert "Fabricated verification" in prompt_text or "fabricated verification" in prompt_text.lower()

    def test_covers_burying_blockers(self, prompt_text: str) -> None:
        assert "Burying blockers" in prompt_text or "burying blockers" in prompt_text.lower()


class TestHardRules:
    def test_never_fabricate_evidence(self, prompt_text: str) -> None:
        assert "Never fabricate evidence" in prompt_text or "never fabricate" in prompt_text.lower()

    def test_partial_not_equal_complete(self, prompt_text: str) -> None:
        assert "Partial" in prompt_text and "complete" in prompt_text.lower()

    def test_no_waiting_for_followup(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "do not wait" in lowered or "without waiting" in lowered
