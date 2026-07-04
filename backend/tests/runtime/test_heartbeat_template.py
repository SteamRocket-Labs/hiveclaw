"""Tests for app/templates/HEARTBEAT.md.

Heartbeat is now a direct T3 LLM core protocol, not a full agent/session/tool
prompt. These tests lock that boundary while preserving the memory curation
rules the platform still needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "templates"
    / "HEARTBEAT.md"
)


@pytest.fixture(scope="module")
def template_text() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


class TestTemplateStructure:
    def test_template_is_direct_core_protocol_not_full_agent_prompt(self, template_text: str) -> None:
        for required in (
            "Direct T3 Core Protocol",
            "T3 Consolidator",
            "direct LLM core",
            "not a full agent session",
            "does not receive tools",
            "does not start subagents",
            "does not perform external actions",
        ):
            assert required in template_text

        for retired in (
            "<role>",
            "<session_context>",
            "<phase_1_observe>",
            "<phase_2_curate>",
            "<persistent_session_notes>",
            "tool rounds",
            "submit_t3_",
        ):
            assert retired not in template_text

    def test_template_documents_upstream_and_downstream(self, template_text: str) -> None:
        assert "T2 Segment Package" in template_text
        assert "source_bundle.json" in template_text
        assert "t3_neighborhood.md" in template_text
        assert "extract_agent" not in template_text
        assert "semantic memory body" in template_text
        assert "soul.md" in template_text


class TestDecisionMatrix:
    def test_matrix_table_headers(self, template_text: str) -> None:
        # A proper markdown table with the agreed columns.
        assert "| w" in template_text
        assert "| cat" in template_text
        assert "| action" in template_text

    def test_matrix_covers_all_weight_tiers(self, template_text: str) -> None:
        assert ">= 0.85" in template_text or "≥ 0.85" in template_text
        assert "0.50–0.85" in template_text or "0.50-0.85" in template_text
        assert "< 0.50" in template_text

    def test_matrix_maps_each_category_to_file(self, template_text: str) -> None:
        for target in [
            "memory/self/self.md",
            "memory/profiles/owner.md",
            "memory/knowledge/<slug>.md",
            "memory/milestones/<slug>.md",
        ]:
            assert target in template_text

    def test_matrix_has_tiebreaker_guidance(self, template_text: str) -> None:
        assert "Tiebreakers" in template_text or "tiebreaker" in template_text.lower()
        assert "false negative" in template_text.lower()

    def test_template_does_not_tell_curator_to_write_platform_managed_evolution_files(
        self,
        template_text: str,
    ) -> None:
        forbidden_phrases = [
            "Append to `evolution/",
            "Update `evolution/",
            "Write to evolution/",
            "edit_file` under `evolution/",
            "write_file` under `evolution/",
        ]
        for phrase in forbidden_phrases:
            assert phrase not in template_text
        assert "runtime records heartbeat evidence into T0/session audit paths" in template_text
        assert "do not write legacy `evolution/scorecard.md`" in template_text


class TestCurationExamples:
    def test_template_keeps_profile_and_milestone_curation_rules(self, template_text: str) -> None:
        assert "80%" in template_text
        assert "15%" in template_text
        assert "5%" in template_text
        assert "counter-example" in template_text or "反例下调" in template_text
        assert "retroactive" in template_text or "追认" in template_text
        assert "ms-" in template_text


class TestT3EntryRules:
    def test_format_is_owned_by_platform_gate_runtime(self, template_text: str) -> None:
        assert "XML-block based" in template_text
        assert "<t3_consolidation_patch" in template_text

    def test_template_forbids_raw_memory_file_writes(self, template_text: str) -> None:
        assert "refused" in template_text
        assert "Platform Gate owns physical commit" in template_text
        assert "not the physical committer" in template_text

    def test_template_instructs_to_drop_t2_metadata(self, template_text: str) -> None:
        assert "Drop T2 metadata" in template_text or "drop the T2 metadata" in template_text.lower()
        assert "source refs" in template_text

    def test_template_says_dedup_is_platform_gate_enforced(self, template_text: str) -> None:
        assert "Dedup is enforced by Platform Gate" in template_text


class TestOperationalInvariants:
    def test_required_output_tags_specified(self, template_text: str) -> None:
        assert "[OUTCOME:" in template_text
        assert "[SCORE:" in template_text

    def test_t0_backfill_provenance_note_preserved(self, template_text: str) -> None:
        # PR-7 regression guard — heartbeat must know t0_backfill = human bucket.
        assert "t0_backfill" in template_text
        assert "human bucket" in template_text

    def test_external_content_is_data_guardrail_preserved(self, template_text: str) -> None:
        # The prompt-injection guardrail must ride through the rewrite.
        assert "data, not instruction" in template_text.lower()

    def test_tool_runtime_is_not_reintroduced(self, template_text: str) -> None:
        assert "tool rounds" not in template_text
        assert "does not receive tools" in template_text

    def test_template_does_not_instruct_external_actions(self, template_text: str) -> None:
        assert "Do not" in template_text
        assert "plaza" in template_text.lower()
        assert "Do not send messages" in template_text
        assert "Do not create Skill files or Workflow JSON" in template_text
