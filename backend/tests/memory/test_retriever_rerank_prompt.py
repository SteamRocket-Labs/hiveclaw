"""Tests for the memory-rerank prompt in retriever._rerank_semantic_items (PR-26).

Reranking is invoked when semantic candidates exceed _RERANK_THRESHOLD. A
weak prompt here silently degrades semantic memory quality for the agent.
We read the source text and lock down the XML structure, selection
criteria, anti-patterns, and hard JSON output contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SOURCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "memory"
    / "retriever.py"
)


def _extract_literal(var_name: str) -> str:
    """Pull the `var_name = (...)` literal out of retriever.py."""
    text = _SOURCE_PATH.read_text(encoding="utf-8")
    marker = var_name + " = ("
    start = text.index(marker) + len(marker)
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start:i - 1]


@pytest.fixture(scope="module")
def system_prompt() -> str:
    return _extract_literal("system_prompt")


@pytest.fixture(scope="module")
def user_prompt() -> str:
    return _extract_literal("user_prompt")


class TestSystemPromptStructure:
    def test_xml_tags_present(self, system_prompt: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<selection_criteria>",
            "</selection_criteria>",
            "<anti_patterns>",
            "</anti_patterns>",
            "<output_contract>",
            "</output_contract>",
        ]:
            assert tag in system_prompt, f"missing tag: {tag}"

    def test_role_is_reranker(self, system_prompt: str) -> None:
        assert "memory reranker" in system_prompt

    def test_role_forbids_mutation(self, system_prompt: str) -> None:
        assert "you only pick" in system_prompt


class TestSelectionCriteria:
    def test_relevance_first(self, system_prompt: str) -> None:
        assert "Relevance to the literal query intent comes first" in system_prompt

    def test_prefer_concrete_artifacts(self, system_prompt: str) -> None:
        lowered = system_prompt.lower()
        assert "concrete artifacts" in lowered
        assert "file paths" in lowered

    def test_dedup_near_duplicates(self, system_prompt: str) -> None:
        assert "near-duplicates" in system_prompt
        assert "keep ONE" in system_prompt


class TestAntiPatterns:
    def test_no_invention_rule(self, system_prompt: str) -> None:
        assert "Do NOT invent" in system_prompt

    def test_no_out_of_bounds_indices(self, system_prompt: str) -> None:
        assert "indices outside [0, len(items) - 1]" in system_prompt

    def test_no_padding_to_cap(self, system_prompt: str) -> None:
        assert "Do NOT pad the selection" in system_prompt
        assert "empty selection is valid" in system_prompt.lower()

    def test_no_markdown_fences(self, system_prompt: str) -> None:
        assert "Do NOT wrap the JSON in markdown fences" in system_prompt


class TestOutputContract:
    def test_json_schema_specified(self, system_prompt: str) -> None:
        assert '{"selected": [<int>, <int>, ...]}' in system_prompt

    def test_zero_based_indices(self, system_prompt: str) -> None:
        assert "0-based" in system_prompt

    def test_order_by_relevance(self, system_prompt: str) -> None:
        assert "descending relevance" in system_prompt


class TestUserPromptShape:
    def test_has_query_tag(self, user_prompt: str) -> None:
        assert "<query>" in user_prompt
        assert "</query>" in user_prompt

    def test_has_candidate_memories_tag(self, user_prompt: str) -> None:
        assert "<candidate_memories>" in user_prompt
        assert "</candidate_memories>" in user_prompt

    def test_has_task_tag(self, user_prompt: str) -> None:
        assert "<task>" in user_prompt
        assert "</task>" in user_prompt

    def test_manifest_format_documented(self, user_prompt: str) -> None:
        assert "Format: `<index>: <content preview" in user_prompt


class TestRuntimeBindings:
    def test_query_and_manifest_still_injected(self) -> None:
        source = _SOURCE_PATH.read_text(encoding="utf-8")
        # The user_prompt literal concatenates the real query + manifest.
        assert "<query>\" + query + \"</query>" in source
        assert "+ manifest" in source

    def test_max_select_still_injected(self) -> None:
        source = _SOURCE_PATH.read_text(encoding="utf-8")
        assert "+ str(max_select)" in source

    def test_system_and_user_still_wired(self) -> None:
        source = _SOURCE_PATH.read_text(encoding="utf-8")
        assert 'LLMMessage(role="system", content=system_prompt)' in source
        assert 'LLMMessage(role="user", content=user_prompt)' in source
