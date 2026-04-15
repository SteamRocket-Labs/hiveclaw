"""Tests for COORDINATOR_SYSTEM_PROMPT (PR-15).

Coordinator mode drives multi-agent orchestration. The prompt must carry a
clear role, a decision matrix for parallelism/verification/reporting choices,
good/bad examples, and an anti-pattern list. Regressions here cause silent
quality loss across every coordinator-driven workflow, so we lock down the
best-practice surface.
"""

from __future__ import annotations

import pytest

from app.runtime.coordinator import (
    COORDINATOR_ALLOWED_TOOLS,
    COORDINATOR_SYSTEM_PROMPT,
    get_coordinator_prompt,
)


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return COORDINATOR_SYSTEM_PROMPT


class TestPromptStructure:
    def test_xml_tags_present(self, prompt_text: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<pipeline_context>",
            "<decision_matrix>",
            "<good_coordination_examples>",
            "<anti_patterns>",
            "<allowed_tools>",
            "<final_report_format>",
        ]:
            assert tag in prompt_text, f"missing tag: {tag}"

    def test_role_is_dispatcher_not_executor(self, prompt_text: str) -> None:
        assert "dispatcher" in prompt_text.lower()
        assert "domain tools directly" in prompt_text.lower() or "not to execute" in prompt_text.lower()

    def test_pipeline_context_documents_upstream_and_downstream(self, prompt_text: str) -> None:
        assert "Upstream" in prompt_text
        assert "Downstream" in prompt_text
        assert "Completed/Evidence/Blockers" in prompt_text


class TestDecisionMatrix:
    def test_matrix_table_headers(self, prompt_text: str) -> None:
        assert "| Phase" in prompt_text
        assert "| Question" in prompt_text
        assert "| Action" in prompt_text

    def test_matrix_covers_core_phases(self, prompt_text: str) -> None:
        for phase_marker in [
            "Decompose",
            "Fan-out",
            "Write path",
            "Worker pick",
            "Synthesize",
            "Verify",
            "Report",
        ]:
            assert phase_marker in prompt_text, f"missing phase: {phase_marker}"

    def test_matrix_has_tiebreakers(self, prompt_text: str) -> None:
        assert "Tiebreakers" in prompt_text or "tiebreaker" in prompt_text.lower()
        # Tiebreakers must give explicit defaults for the 3 common dilemmas.
        assert "parallel" in prompt_text.lower() and "serial" in prompt_text.lower()
        assert "continue" in prompt_text.lower() and "spawn" in prompt_text.lower()
        assert "verify" in prompt_text.lower()


class TestGoodExamples:
    def test_has_two_full_examples(self, prompt_text: str) -> None:
        assert "Example A" in prompt_text
        assert "Example B" in prompt_text

    def test_examples_show_research_impl_verify_flow(self, prompt_text: str) -> None:
        # Example A must walk through research → synthesize → implement → FRESH verify
        assert "FRESH" in prompt_text or "fresh worker" in prompt_text.lower()

    def test_examples_show_parallel_read_only(self, prompt_text: str) -> None:
        # Example B must show parallel fan-out explicitly as a safe case.
        assert "parallel" in prompt_text.lower()
        assert "read-only" in prompt_text.lower() or "disjoint" in prompt_text.lower()


class TestAntiPatterns:
    def test_covers_key_failure_modes(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        # Must explicitly name each of the canonical coordinator failure modes.
        assert "delegate understanding" in lowered
        assert "parallelize writes" in lowered or "editing the same file" in lowered
        assert "self-verify" in lowered
        assert "recursive" in lowered  # no nested coordination
        assert "silent wait" in lowered or "pretend the answer is ready" in lowered
        assert "domain execution" in lowered or "domain tool" in lowered
        assert "vague delegation" in lowered
        assert "skipping synthesis" in lowered or "verbatim" in lowered


class TestAllowedToolsAlignment:
    def test_prompt_tools_match_allowed_set(self, prompt_text: str) -> None:
        # The prompt explicitly lists what's allowed. It must agree with
        # COORDINATOR_ALLOWED_TOOLS so the LLM's mental model matches runtime.
        for tool_name in COORDINATOR_ALLOWED_TOOLS:
            assert tool_name in prompt_text, f"allowed tool not in prompt: {tool_name}"

    def test_prompt_rejects_domain_tools_by_name(self, prompt_text: str) -> None:
        # Must name at least two domain tools as unavailable so the LLM
        # doesn't try them.
        assert "web_search" in prompt_text
        assert "execute_code" in prompt_text


class TestFinalReportFormat:
    def test_report_has_three_sections(self, prompt_text: str) -> None:
        assert "## Status" in prompt_text
        assert "## Synthesis" in prompt_text
        assert "## Next Actions" in prompt_text

    def test_report_handles_running_workers(self, prompt_text: str) -> None:
        # If workers are still running, omit synthesis; never fabricate.
        assert "still running" in prompt_text.lower() or "Waiting on" in prompt_text
        assert "fabricate" in prompt_text.lower() or "Never fabricate" in prompt_text


class TestHelperStillWorks:
    def test_get_coordinator_prompt_returns_text(self) -> None:
        out = get_coordinator_prompt()
        assert isinstance(out, str)
        assert "<role>" in out
        assert "<decision_matrix>" in out
