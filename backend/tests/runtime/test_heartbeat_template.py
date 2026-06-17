"""Tests for app/templates/HEARTBEAT.md (PR-12).

Locks down the structural properties of the template so future regressions
(removing the decision matrix, dropping the good/bad examples, etc.) fail
loud. The template is read by `_load_heartbeat_instruction` and concatenated
verbatim into each tick's user prompt, so its quality directly shapes how
heartbeat ticks behave.
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
    def test_xml_tags_present(self, template_text: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<pipeline_context>",
            "<session_context>",
            "<decision_matrix>",
            "<phase_1_observe>",
            "<phase_2_curate>",
            "<t3_entry_rules>",
            "<phase_3_log>",
            "<scope_and_boundaries>",
            "<persistent_session_notes>",
            "<required_output>",
            "<constraints>",
        ]:
            assert tag in template_text, f"missing tag: {tag}"

    def test_template_documents_upstream_and_downstream(self, template_text: str) -> None:
        assert "Upstream" in template_text
        assert "extract_agent" in template_text
        assert "Downstream" in template_text
        assert "dream" in template_text.lower()
        assert "soul.md" in template_text


class TestDecisionMatrix:
    def test_matrix_table_headers(self, template_text: str) -> None:
        # A proper markdown table with the agreed columns.
        assert "| w" in template_text
        assert "| cat" in template_text
        assert "| action" in template_text

    def test_matrix_covers_all_weight_tiers(self, template_text: str) -> None:
        assert "≥ 0.85" in template_text
        assert "0.50–0.85" in template_text or "0.50-0.85" in template_text
        assert "< 0.50" in template_text

    def test_matrix_maps_each_category_to_file(self, template_text: str) -> None:
        for target in [
            "memory/feedback.md",
            "memory/blocked.md",
            "memory/strategies.md",
            "memory/knowledge.md",
            "memory/user.md",
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
        assert "runtime records the heartbeat outcome into `evolution/`" in template_text


class TestCurationExamples:
    def test_has_good_curation_examples_block(self, template_text: str) -> None:
        assert "<good_curation_examples>" in template_text
        # Contains at least two named examples.
        assert "Example A" in template_text
        assert "Example B" in template_text

    def test_has_bad_curation_examples_block(self, template_text: str) -> None:
        assert "<bad_curation_examples>" in template_text
        assert "Anti-Example" in template_text

    def test_examples_show_before_after_rewrite(self, template_text: str) -> None:
        # The "rewrite for long-term reusability" section must show good/bad pair.
        assert "User rejects emoji" in template_text  # canonical good-rewrite example
        # Promotions are expressed as governed save_memory calls (spec §12 P2).
        assert "save_memory(" in template_text


class TestT3EntryRules:
    def test_format_is_owned_by_save_memory_runtime(self, template_text: str) -> None:
        # The curator passes clean content; the governed API stamps the
        # `- [YYYY-MM-DD]` format, entry id, and lifecycle record.
        assert "- [YYYY-MM-DD] description" in template_text
        assert "save_memory" in template_text

    def test_template_forbids_raw_memory_file_writes(self, template_text: str) -> None:
        # Direct write_file/edit_file under memory/ is refused by the runtime;
        # the template must say so to prevent the curator from drifting back.
        assert "refused" in template_text
        assert "ONLY write path" in template_text or "only write path" in template_text.lower()

    def test_template_instructs_to_drop_t2_metadata(self, template_text: str) -> None:
        assert "Drop T2 metadata" in template_text or "drop the T2 metadata" in template_text.lower()
        assert "[w=]" in template_text

    def test_template_says_dedup_is_tool_enforced(self, template_text: str) -> None:
        assert "Dedup is enforced by the tool" in template_text or "[Skipped]" in template_text


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

    def test_tool_round_budget_preserved(self, template_text: str) -> None:
        # Operational guardrail that protects token budget.
        assert "Maximum" in template_text
        assert "tool rounds" in template_text
        assert "40" in template_text

    def test_template_does_not_instruct_external_actions(self, template_text: str) -> None:
        # Heartbeat must not post to plaza or send outbound messages.
        assert "Do NOT" in template_text or "Do not" in template_text
        assert "plaza" in template_text.lower()
