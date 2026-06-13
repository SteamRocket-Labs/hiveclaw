"""Tests for _SOUL_REFINE_PROMPT (PR-14).

The soul-refine prompt is the HR→soul.md entry point. Every new agent's
identity contract is produced by this prompt. Regressions here corrupt the
foundation of the entire 4-layer memory pyramid, so we lock down its
best-practice structure: XML tags, pipeline context, few-shot examples,
anti-patterns, hard rules, and JSON-only output contract.
"""

from __future__ import annotations

import pytest

from app.tools.handlers.hr import _SOUL_REFINE_PROMPT


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return _SOUL_REFINE_PROMPT


class TestPromptStructure:
    def test_xml_tags_present(self, prompt_text: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<pipeline_context>",
            "</pipeline_context>",
            "<raw_inputs>",
            "<output_schema>",
            "<field_requirements>",
            "<few_shot_examples>",
            "<anti_patterns>",
            "<hard_rules>",
        ]:
            assert tag in prompt_text, f"missing tag: {tag}"

    def test_pipeline_context_documents_pyramid(self, prompt_text: str) -> None:
        assert "T0" in prompt_text
        assert "T2" in prompt_text
        assert "T3" in prompt_text
        assert "soul.md" in prompt_text
        assert "dream" in prompt_text.lower()
        assert "heartbeat" in prompt_text.lower()

    def test_pipeline_context_explains_why_output_matters(self, prompt_text: str) -> None:
        # The prompt must motivate the LLM: soul shapes everything downstream.
        assert "frozen prefix" in prompt_text.lower() or "gravitational" in prompt_text.lower()


class TestOutputSchema:
    def test_all_seven_keys_listed(self, prompt_text: str) -> None:
        for key in [
            "role_description",
            "personality",
            "boundaries",
            "primary_users",
            "core_outputs",
            "quality_standards",
            "first_tasks",
        ]:
            assert f'"{key}"' in prompt_text, f"missing key: {key}"

    def test_json_only_contract_stated(self, prompt_text: str) -> None:
        assert "JSON" in prompt_text
        assert "no markdown fences" in prompt_text.lower() or "no markdown" in prompt_text.lower()
        assert "json.loads" in prompt_text.lower() or "json-only" in prompt_text.lower()


class TestFieldRequirements:
    def test_each_field_has_bad_good_pair(self, prompt_text: str) -> None:
        # Best-practice: every field requirement shows a BAD (rejected) and
        # a GOOD (accepted) example so the LLM can contrast.
        bad_count = prompt_text.count("BAD:")
        good_count = prompt_text.count("GOOD")  # "GOOD:" and "GOOD (...role):"
        assert bad_count >= 6, f"expected ≥6 BAD markers, got {bad_count}"
        assert good_count >= 6, f"expected ≥6 GOOD markers, got {good_count}"

    def test_first_tasks_exact_count_enforced(self, prompt_text: str) -> None:
        assert "EXACTLY 3" in prompt_text or "exactly 3" in prompt_text


class TestFewShotExamples:
    def test_has_two_full_examples(self, prompt_text: str) -> None:
        assert "Example 1" in prompt_text
        assert "Example 2" in prompt_text

    def test_examples_cover_thin_and_rich_inputs(self, prompt_text: str) -> None:
        # Example 1 is explicitly thin-input; Example 2 is rich.
        assert "thin inputs" in prompt_text.lower()
        assert "rich inputs" in prompt_text.lower()

    def test_examples_contain_json_payload(self, prompt_text: str) -> None:
        # Both examples must show an actual JSON output, not just prose.
        assert "```json" in prompt_text
        # At least two fenced json blocks (one per example).
        assert prompt_text.count("```json") >= 2

    def test_examples_cover_chinese_and_english(self, prompt_text: str) -> None:
        # Example 1 is Chinese, Example 2 English — forces LLM to internalize
        # language-match rule instead of defaulting to one locale.
        assert "投研" in prompt_text or "研报" in prompt_text
        assert "DevOps" in prompt_text or "postmortem" in prompt_text.lower()


class TestAntiPatterns:
    def test_anti_patterns_cover_key_failure_modes(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        # Genericity
        assert "generic" in lowered
        # Adjective soup
        assert "adjective" in lowered
        # Date-anchored content
        assert "date" in lowered
        # Tool-name leakage
        assert "tool" in lowered and "config" in lowered
        # Empty fields
        assert "empty" in lowered
        # Markdown fences / trailing prose
        assert "fences" in lowered or "prose" in lowered


class TestHardRules:
    def test_language_match_rule(self, prompt_text: str) -> None:
        assert "Language match" in prompt_text or "language match" in prompt_text.lower()
        assert "Chinese" in prompt_text
        assert "English" in prompt_text

    def test_durability_rule(self, prompt_text: str) -> None:
        assert "6 months" in prompt_text or "six months" in prompt_text.lower()

    def test_infer_when_empty_rule(self, prompt_text: str) -> None:
        assert "infer" in prompt_text.lower()
        assert "empty" in prompt_text.lower() or "vague" in prompt_text.lower()


class TestFormatContract:
    def test_prompt_format_placeholders_preserved(self, prompt_text: str) -> None:
        # _refine_soul_inputs calls .format(name=..., role_description=..., ...)
        # so these placeholders MUST remain exactly.
        for placeholder in [
            "{name}",
            "{role_description}",
            "{personality}",
            "{boundaries}",
            "{primary_users}",
            "{core_outputs}",
        ]:
            assert placeholder in prompt_text, f"missing format placeholder: {placeholder}"

    def test_example_json_braces_are_escaped_for_str_format(self, prompt_text: str) -> None:
        # JSON in few-shot blocks must use {{ }} because we .format() this string.
        # Any single { or } outside the named placeholders would break str.format.
        # We validate by actually calling .format with dummy args — it must not raise.
        try:
            _ = prompt_text.format(
                name="test",
                role_description="test",
                personality="test",
                boundaries="test",
                primary_users="test",
                core_outputs="test",
            )
        except (KeyError, IndexError, ValueError) as exc:
            pytest.fail(f"_SOUL_REFINE_PROMPT.format() failed: {exc}")
