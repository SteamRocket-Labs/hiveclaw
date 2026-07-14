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
            "<coordination_tools>",
            "<final_report_format>",
        ]:
            assert tag in prompt_text, f"missing tag: {tag}"

    def test_role_preserves_direct_execution_judgment(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "retain your assigned tools" in lowered
        assert "execute work directly" in lowered
        assert "strategy, not a restriction" in lowered

    def test_pipeline_context_documents_upstream_and_downstream(self, prompt_text: str) -> None:
        assert "Upstream" in prompt_text
        assert "Downstream" in prompt_text
        assert "free-form" in prompt_text.lower() or "digest" in prompt_text.lower()
        assert "wake policy" in prompt_text.lower()

    def test_prompt_does_not_force_worker_or_final_return_shapes(self, prompt_text: str) -> None:
        assert "Completed/Evidence/Blockers" not in prompt_text
        assert "Every user-facing reply from coordinator mode has exactly this shape" not in prompt_text
        assert "<return_format>" not in prompt_text


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

    def test_matrix_is_advisory_not_a_mechanical_policy(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "non-binding decision guide" in lowered
        assert "do not mechanically" in lowered
        assert "no hard-coded tiebreaker" in lowered


class TestGoodExamples:
    def test_has_two_full_examples(self, prompt_text: str) -> None:
        assert "Example A" in prompt_text
        assert "Example B" in prompt_text

    def test_examples_show_research_impl_verify_flow(self, prompt_text: str) -> None:
        # Example A preserves verification while leaving the model free to pick
        # direct inspection or an independent context proportionate to the claim.
        normalized = " ".join(prompt_text.lower().split())
        assert "proportionate verification" in normalized
        assert "independent context would materially improve confidence" in normalized
        assert "fresh worker" not in normalized

    def test_examples_show_parallel_read_only(self, prompt_text: str) -> None:
        # Example B must show parallel fan-out explicitly as a safe case.
        assert "parallel" in prompt_text.lower()
        assert "read-only" in prompt_text.lower() or "disjoint" in prompt_text.lower()


class TestAntiPatterns:
    def test_covers_key_failure_modes(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        # Must explicitly name each of the canonical coordinator failure modes.
        assert "delegate understanding" in lowered
        assert "uncontrolled conflicting writes" in lowered
        assert "ownership, isolation, or merge protocol" in lowered
        assert "uncritical self-confirmation" in lowered
        assert "recursive" in lowered
        assert "false completion" in lowered
        assert "mechanical delegation" in lowered
        assert "vague delegation" in lowered
        assert "skipping synthesis" in lowered or "verbatim" in lowered


class TestToolSurfaceAlignment:
    def test_prompt_lists_strict_dispatcher_coordination_set(self, prompt_text: str) -> None:
        for tool_name in COORDINATOR_ALLOWED_TOOLS:
            assert tool_name in prompt_text, f"allowed tool not in prompt: {tool_name}"

    def test_prompt_preserves_assigned_domain_tools_by_default(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "all other tools assigned to you remain available" in lowered
        assert "explicit strict-dispatcher mode" in lowered


class TestFinalReportFormat:
    def test_report_has_three_sections(self, prompt_text: str) -> None:
        assert "## Status" in prompt_text
        assert "## Synthesis" in prompt_text
        assert "## Next Actions" in prompt_text

    def test_report_handles_running_workers(self, prompt_text: str) -> None:
        # Running work remains visible, without imposing a mechanical report shape.
        assert "running work" in prompt_text.lower()
        assert "fabricate" in prompt_text.lower() or "Never fabricate" in prompt_text


class TestHelperStillWorks:
    def test_get_coordinator_prompt_returns_text(self) -> None:
        out = get_coordinator_prompt()
        assert isinstance(out, str)
        assert "<role>" in out
        assert "<decision_matrix>" in out

    def test_strict_dispatcher_requires_explicit_selection(self) -> None:
        out = get_coordinator_prompt(dispatcher_only=True)
        assert "explicitly selected strict dispatcher mode" in out
