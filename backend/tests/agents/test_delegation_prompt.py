"""Tests for _build_delegated_worker_prompt (PR-16).

Delegated workers run in isolated child sessions. Their prompt must lock
down: isolation contract, tool policy (per-profile), strict return format,
good/bad examples. Parent parses the structured return block — regressions
in format discipline silently break coordinator synthesis downstream.
"""

from __future__ import annotations

import pytest

from app.agents.orchestrator import (
    _build_delegated_worker_prompt,
    _DELEGATED_WORKER_PROMPT_SUFFIX,
    _DELEGATION_TOOL_PROFILES,
)


@pytest.fixture(scope="module")
def worker_safe_prompt() -> str:
    return _build_delegated_worker_prompt(_DELEGATION_TOOL_PROFILES["worker_safe"])


@pytest.fixture(scope="module")
def research_prompt() -> str:
    return _build_delegated_worker_prompt(_DELEGATION_TOOL_PROFILES["research_readonly"])


class TestPromptStructure:
    def test_xml_tags_present(self, worker_safe_prompt: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<isolation_contract>",
            "</isolation_contract>",
            "<tool_policy>",
            "</tool_policy>",
            "<return_format>",
            "</return_format>",
            "<good_return_examples>",
            "</good_return_examples>",
            "<bad_return_examples>",
            "</bad_return_examples>",
        ]:
            assert tag in worker_safe_prompt, f"missing tag: {tag}"

    def test_role_declares_worker_not_chat(self, worker_safe_prompt: str) -> None:
        assert "delegated worker" in worker_safe_prompt.lower()
        assert "NOT a chat assistant" in worker_safe_prompt or "not a chat" in worker_safe_prompt.lower()


class TestIsolationContract:
    def test_declares_brief_as_only_context(self, worker_safe_prompt: str) -> None:
        assert "ONLY authoritative context" in worker_safe_prompt

    def test_declares_parent_history_unavailable(self, worker_safe_prompt: str) -> None:
        assert "NOT available" in worker_safe_prompt

    def test_forbids_nested_delegation(self, worker_safe_prompt: str) -> None:
        assert (
            "nested workers" in worker_safe_prompt.lower()
            or "delegation tools are disabled" in worker_safe_prompt.lower()
        )
        assert "Blocker" in worker_safe_prompt

    def test_forbids_context_leak(self, worker_safe_prompt: str) -> None:
        assert "leak" in worker_safe_prompt.lower()


class TestToolPolicyInjection:
    def test_worker_safe_policy_present(self, worker_safe_prompt: str) -> None:
        # The per-profile tool_rule and memory_rule must be baked into the prompt.
        assert _DELEGATION_TOOL_PROFILES["worker_safe"].tool_rule in worker_safe_prompt
        assert _DELEGATION_TOOL_PROFILES["worker_safe"].memory_rule in worker_safe_prompt

    def test_research_readonly_policy_present(self, research_prompt: str) -> None:
        assert _DELEGATION_TOOL_PROFILES["research_readonly"].tool_rule in research_prompt
        assert _DELEGATION_TOOL_PROFILES["research_readonly"].memory_rule in research_prompt


class TestReturnFormat:
    def test_three_sections_specified(self, worker_safe_prompt: str) -> None:
        assert "Completed:" in worker_safe_prompt
        assert "Evidence:" in worker_safe_prompt
        assert "Blockers:" in worker_safe_prompt

    def test_no_prose_outside_sections_rule(self, worker_safe_prompt: str) -> None:
        assert "No prose outside" in worker_safe_prompt or "no prose outside" in worker_safe_prompt.lower()

    def test_specifies_parent_parses_structure(self, worker_safe_prompt: str) -> None:
        assert "parent parses" in worker_safe_prompt.lower()


class TestGoodExamples:
    def test_has_three_good_examples(self, worker_safe_prompt: str) -> None:
        assert "Example A" in worker_safe_prompt
        assert "Example B" in worker_safe_prompt
        assert "Example C" in worker_safe_prompt

    def test_examples_cover_impl_research_and_partial_completion(self, worker_safe_prompt: str) -> None:
        lowered = worker_safe_prompt.lower()
        assert "implementation task" in lowered
        assert "research task" in lowered
        assert "couldn't fully complete" in lowered or "partial" in lowered

    def test_examples_include_file_line_evidence(self, worker_safe_prompt: str) -> None:
        # Good examples must demonstrate file:line references so workers learn
        # the expected evidence shape.
        assert "middleware.py:142" in worker_safe_prompt or "file:line" in worker_safe_prompt


class TestBadExamples:
    def test_covers_empty_claim(self, worker_safe_prompt: str) -> None:
        assert "Empty Completed claim" in worker_safe_prompt or "empty completed" in worker_safe_prompt.lower()

    def test_covers_prose_wrapping(self, worker_safe_prompt: str) -> None:
        assert "Prose wrapping" in worker_safe_prompt or "prose wrapping" in worker_safe_prompt.lower()

    def test_covers_fabricated_evidence(self, worker_safe_prompt: str) -> None:
        assert "Fabricated evidence" in worker_safe_prompt or "fabricated evidence" in worker_safe_prompt.lower()
        assert "Never claim evidence" in worker_safe_prompt

    def test_covers_context_leak(self, worker_safe_prompt: str) -> None:
        assert "Leaking parent context" in worker_safe_prompt or "leaking parent" in worker_safe_prompt.lower()


class TestExportedSuffix:
    def test_suffix_uses_worker_safe_profile(self) -> None:
        # _DELEGATED_WORKER_PROMPT_SUFFIX should equal the worker_safe build.
        expected = _build_delegated_worker_prompt(_DELEGATION_TOOL_PROFILES["worker_safe"])
        assert _DELEGATED_WORKER_PROMPT_SUFFIX == expected

    def test_suffix_is_non_empty_string(self) -> None:
        assert isinstance(_DELEGATED_WORKER_PROMPT_SUFFIX, str)
        assert len(_DELEGATED_WORKER_PROMPT_SUFFIX) > 500
