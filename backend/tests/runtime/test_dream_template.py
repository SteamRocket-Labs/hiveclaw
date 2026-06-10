"""Tests for app/templates/DREAM.md (PR-27).

DREAM.md is the procedural protocol the dream sub-agent follows for
memory consolidation. The LLM consolidator (auto_dream._dream_llm_consolidate)
handles JSON-schema-driven decisions; this template owns the file-
maintenance side: phases, caps, preservation, and output tag.

Existing tests (test_memory_integration.test_v17_dream_md_has_4_phases)
assert the legacy markdown phase headers. We preserve those AND add XML
tags + few-shot + anti-patterns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "templates"
    / "DREAM.md"
)


@pytest.fixture(scope="module")
def template_text() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


class TestLegacyHeadersPreserved:
    """The existing v17 prompt contract still expects these H2 headers."""

    def test_has_orient_header(self, template_text: str) -> None:
        assert "## Phase 1: ORIENT" in template_text

    def test_has_consolidate_header(self, template_text: str) -> None:
        assert "## Phase 2: CONSOLIDATE" in template_text

    def test_has_promote_header(self, template_text: str) -> None:
        assert "## Phase 3: PROMOTE" in template_text

    def test_has_index_cleanup_header(self, template_text: str) -> None:
        assert "## Phase 4: INDEX + CLEANUP" in template_text

    def test_has_constraints_section(self, template_text: str) -> None:
        assert "## Constraints" in template_text

    def test_has_required_output_section(self, template_text: str) -> None:
        assert "## Required Output Format" in template_text


class TestXmlStructure:
    def test_xml_tags_present(self, template_text: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<pipeline_context>",
            "</pipeline_context>",
            "<phase_1_orient>",
            "</phase_1_orient>",
            "<phase_2_consolidate>",
            "</phase_2_consolidate>",
            "<good_consolidation_examples>",
            "</good_consolidation_examples>",
            "<bad_consolidation_examples>",
            "</bad_consolidation_examples>",
            "<phase_3_promote>",
            "</phase_3_promote>",
            "<phase_4_index>",
            "</phase_4_index>",
            "<constraints>",
            "</constraints>",
            "<required_output>",
            "</required_output>",
        ]:
            assert tag in template_text, f"missing tag: {tag}"


class TestRoleAndPipelineContext:
    def test_role_states_maintenance_not_conversation(self, template_text: str) -> None:
        assert "maintenance cycle" in template_text
        assert "NOT a conversation" in template_text

    def test_divides_responsibility_with_llm_consolidator(self, template_text: str) -> None:
        # Must explicitly explain that the LLM consolidator handles JSON
        # decisions, this template handles procedural file work.
        assert "auto_dream._dream_llm_consolidate" in template_text
        assert "Stay in your lane" in template_text

    def test_pipeline_context_names_upstream_and_downstream(self, template_text: str) -> None:
        assert "**Upstream**" in template_text
        assert "**Downstream**" in template_text
        assert "INDEX.md" in template_text
        assert "soul.md" in template_text

    def test_cadence_documented(self, template_text: str) -> None:
        assert "once a day" in template_text or "daily" in template_text
        assert "3 session ends" in template_text


class TestConsolidationExamples:
    def test_has_three_good_examples(self, template_text: str) -> None:
        assert "Example A" in template_text
        assert "Example B" in template_text
        assert "Example C" in template_text

    def test_example_b_shows_vague_to_specific(self, template_text: str) -> None:
        # Example B must show a vague → specific rewrite with file:line.
        assert "middleware.py" in template_text

    def test_example_c_shows_contradiction_resolution(self, template_text: str) -> None:
        assert "Contradictory" in template_text or "contradictory" in template_text.lower()
        assert "newer supersedes older" in template_text


class TestBadExamples:
    def test_covers_over_rewrite(self, template_text: str) -> None:
        assert 'Rewrite ALL entries "for style"' in template_text

    def test_covers_false_collapse(self, template_text: str) -> None:
        lowered = template_text.lower()
        assert "distinct facts" in lowered or "do not merge" in lowered

    def test_covers_deleting_unknown_entries(self, template_text: str) -> None:
        assert "Delete pre-existing entries you don't recognize" in template_text

    def test_covers_scope_violation(self, template_text: str) -> None:
        assert "permanent identity" in template_text
        assert "Learned Behaviors" in template_text

    def test_covers_reordering_for_aesthetics(self, template_text: str) -> None:
        assert "Re-order entries for aesthetics" in template_text


class TestPhase2Rules:
    def test_cap_enforcement_50_entries(self, template_text: str) -> None:
        assert "50 entries" in template_text

    def test_feedback_and_blocked_are_higher_priority(self, template_text: str) -> None:
        assert "`feedback.md` and `blocked.md` are HIGHER priority" in template_text

    def test_preservation_flags_respected(self, template_text: str) -> None:
        assert ".preservation.json" in template_text
        assert "must NOT be demoted" in template_text


class TestPhase3Promotion:
    def test_20_behaviors_cap(self, template_text: str) -> None:
        assert "Maximum **20** learned behaviors" in template_text

    def test_3_plus_frequency_threshold(self, template_text: str) -> None:
        assert "**3+ times**" in template_text

    def test_first_person_format(self, template_text: str) -> None:
        assert "I [behavior description] because [reason]" in template_text

    def test_coordinates_with_consolidator(self, template_text: str) -> None:
        # Must check consolidator's prior work before re-promoting.
        assert "structured consolidator" in template_text.lower() or "already promoted" in template_text.lower()


class TestConstraints:
    def test_25_tool_rounds_budget(self, template_text: str) -> None:
        assert "Maximum **25** tool rounds" in template_text

    def test_edits_not_deletes(self, template_text: str) -> None:
        assert "do not delete entire files" in template_text

    def test_keep_on_doubt(self, template_text: str) -> None:
        assert "When in doubt, KEEP entries" in template_text
        assert "false positive" in template_text.lower()


class TestRequiredOutput:
    def test_output_tag_format(self, template_text: str) -> None:
        assert "[DREAM:complete] [FILES:{N}] [PROMOTED:{N}]" in template_text

    def test_noop_variant(self, template_text: str) -> None:
        assert "[DREAM:noop]" in template_text
