"""Tests for the session-recall summarization prompt (PR-25).

The inline prompt in session_recall._maybe_rerank_recall_hits drives the
1-2 sentence blurb shown next to each cross-session recall hit in the UI.
Regressions here degrade the main surface users see when asking "what did
we decide last time?". We read the prompt source text and lock down its
best-practice structure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SOURCE_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "session_recall.py"


def _extract_prompt_literal() -> str:
    """Isolate the prompt = (...) literal built inside the recall rerank loop."""
    text = _SOURCE_PATH.read_text(encoding="utf-8")
    # Anchor on the session-recall summarizer string that only appears in the
    # rewritten prompt block.
    role_idx = text.index("session-recall summarizer")
    start = text.rfind("prompt = (", 0, role_idx)
    assert start >= 0, "prompt = ( literal not found before session-recall summarizer"
    start = start + len("prompt = (")
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start : i - 1]


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return _extract_prompt_literal()


class TestPromptStructure:
    def test_xml_tags_present(self, prompt_text: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<summary_rules>",
            "</summary_rules>",
            "<good_examples>",
            "</good_examples>",
            "<bad_examples>",
            "</bad_examples>",
            "<input>",
            "</input>",
            "<output_contract>",
            "</output_contract>",
        ]:
            assert tag in prompt_text, f"missing tag: {tag}"

    def test_role_is_recall_summarizer(self, prompt_text: str) -> None:
        assert "session-recall summarizer" in prompt_text

    def test_role_distinguishes_from_long_term_memory(self, prompt_text: str) -> None:
        assert "NOT synthesizing memory for long-term storage" in prompt_text


class TestSummaryRules:
    def test_length_cap(self, prompt_text: str) -> None:
        # 1-2 sentence cap must be explicit.
        assert "1-2 sentences" in prompt_text

    def test_lead_with_outcome(self, prompt_text: str) -> None:
        assert "DECIDED or PRODUCED" in prompt_text

    def test_name_concrete_artifacts(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "file paths" in lowered
        assert "commit hashes" in lowered or "ticket ids" in lowered

    def test_no_invention_rule(self, prompt_text: str) -> None:
        assert "Do not infer, extrapolate, or fabricate" in prompt_text

    def test_language_match_rule(self, prompt_text: str) -> None:
        assert "Match the query's language" in prompt_text


class TestExamples:
    def test_two_good_examples(self, prompt_text: str) -> None:
        # Good example block should cover both Chinese and English.
        assert "Good summary:" in prompt_text
        # Chinese example mention
        assert "auth token" in prompt_text
        # English example with concrete artifact (PR number)
        assert "PR #482" in prompt_text

    def test_bad_examples_cover_failure_modes(self, prompt_text: str) -> None:
        # Each canonical bad pattern must be named.
        lowered = prompt_text.lower()
        assert "no outcome" in lowered
        assert "vague" in lowered
        assert "speculative" in lowered
        assert "length cap violated" in lowered


class TestOutputContract:
    def test_no_prefix_no_markdown(self, prompt_text: str) -> None:
        # Output must be raw text, no markdown fences, no quotes, no prefix.
        assert "No prefix" in prompt_text or "no prefix" in prompt_text.lower()
        assert "no markdown" in prompt_text.lower()


class TestFormatBindings:
    def test_placeholders_present_in_source(self) -> None:
        # The original f-string placeholders for Query/Headline/Evidence must
        # survive the rewrite (or the prompt breaks at runtime).
        source = _SOURCE_PATH.read_text(encoding="utf-8")
        assert 'f"Query: {query}\\n"' in source
        assert "f\"Headline: {hit.get('headline', _FALLBACK_HEADLINE)}\\n\"" in source
        assert "f\"Evidence passthrough: {hit.get('focused_recap', '')}\\n\"" in source
        assert 'f"Evidence:\\n{evidence_block}\\n"' in source

    def test_prompt_still_wired_to_llm_call(self) -> None:
        # The prompt must still reach the LLM via client.stream(...) with a
        # user-role LLMMessage.
        source = _SOURCE_PATH.read_text(encoding="utf-8")
        # Minimal sanity: the message construction still wraps the prompt.
        assert 'LLMMessage(role="user", content=prompt)' in source
        assert "output_tokens = get_max_tokens(" in source
        assert "max_tokens=output_tokens" in source
