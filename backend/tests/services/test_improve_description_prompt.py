"""Tests for scripts__improve_description.py prompt (PR-21).

This script is a standalone skill-eval helper (imported via the skill
creator pipeline, not the backend runtime). It can't be imported directly
because it references `scripts.utils` — a relative import only valid when
the skill is extracted into a working directory. We lock down the prompt
structure by reading the source as text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "app" / "services" / "skill_creator_files" / "scripts__improve_description.py"
)


@pytest.fixture(scope="module")
def source_text() -> str:
    return _SCRIPT_PATH.read_text(encoding="utf-8")


class TestPromptStructure:
    def test_xml_tags_present(self, source_text: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<progressive_disclosure_context>",
            "<current_description>",
            "<skill_content>",
            "<optimization_tips>",
            "<anti_patterns>",
            "<constraints>",
            "<output_format>",
        ]:
            assert tag in source_text, f"missing tag: {tag}"

    def test_role_focuses_on_trigger_accuracy(self, source_text: str) -> None:
        assert "Trigger accuracy" in source_text
        assert "the metric" in source_text


class TestProgressiveDisclosureContext:
    def test_explains_selector_vs_body(self, source_text: str) -> None:
        assert "available_skills" in source_text
        assert "progressive disclosure" in source_text.lower()

    def test_explains_token_cost(self, source_text: str) -> None:
        # The description lives in every turn, so the model must be aware
        # of the token trade-off.
        assert "injected into every LLM turn" in source_text or "every LLM turn" in source_text


class TestOptimizationTips:
    def test_imperative_voice_tip(self, source_text: str) -> None:
        assert "Imperative voice" in source_text
        assert '"Use this skill for' in source_text

    def test_intent_over_implementation_tip(self, source_text: str) -> None:
        assert "Intent over implementation" in source_text

    def test_distinctive_phrasing_tip(self, source_text: str) -> None:
        assert "Distinctive phrasing" in source_text
        assert "competes" in source_text.lower()

    def test_generalize_dont_enumerate_tip(self, source_text: str) -> None:
        assert "Generalize, don't enumerate" in source_text
        assert "overfits" in source_text.lower()

    def test_change_up_on_failure_tip(self, source_text: str) -> None:
        assert "Change it up" in source_text or "structurally different angle" in source_text


class TestAntiPatterns:
    def test_overfitting(self, source_text: str) -> None:
        assert "Overfitting to failed queries" in source_text

    def test_implementation_leakage(self, source_text: str) -> None:
        assert "Implementation leakage" in source_text

    def test_generic_boilerplate(self, source_text: str) -> None:
        assert "Generic boilerplate" in source_text

    def test_trigger_word_stuffing(self, source_text: str) -> None:
        assert "Trigger-word stuffing" in source_text

    def test_prompt_injection_bait(self, source_text: str) -> None:
        # Security boundary — the description is data, not instructions.
        assert "Prompt-injection" in source_text or "prompt-injection" in source_text.lower()
        assert "ignore prior" in source_text.lower()
        assert "data, not instructions" in source_text.lower()

    def test_repeating_prior_attempt(self, source_text: str) -> None:
        assert "Repeating a prior attempt" in source_text


class TestConstraints:
    def test_length_cap_1024_chars(self, source_text: str) -> None:
        assert "1024 characters" in source_text

    def test_format_raw_text_no_fences(self, source_text: str) -> None:
        assert "No markdown fences" in source_text
        assert "No prose before or after" in source_text or "no prose" in source_text.lower()

    def test_language_match_rule(self, source_text: str) -> None:
        assert "match the language" in source_text.lower() or "Do not translate" in source_text


class TestOutputFormat:
    def test_new_description_tag_required(self, source_text: str) -> None:
        assert "<new_description>" in source_text
        assert "</new_description>" in source_text

    def test_shows_example_shape(self, source_text: str) -> None:
        # The prompt must show an example to ground the LLM.
        assert "Example shape" in source_text or "Use this skill when" in source_text
