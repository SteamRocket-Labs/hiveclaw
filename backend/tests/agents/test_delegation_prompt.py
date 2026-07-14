"""Tests for _build_delegated_worker_prompt (F-1: dispatch symmetry slim).

Delegated workers run in isolated child sessions. Their framing prompt keeps the
legitimate L2 harness context — isolation contract + per-profile tool policy + a
light descriptive line naming the delegating peer. F-1 REMOVED the heavy
``<return_format>`` / good+bad return examples: freezing the return shape is an
L1 violation (it boxes in the worker's thinking product). The worker now returns
a free-form digest (CC-style), and the instruction itself reaches the worker
verbatim (see test_dispatch_symmetry.py).
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
    def test_keeps_isolation_and_tool_policy_tags(self, worker_safe_prompt: str) -> None:
        for tag in [
            "<isolation_contract>",
            "</isolation_contract>",
            "<tool_policy>",
            "</tool_policy>",
        ]:
            assert tag in worker_safe_prompt, f"missing tag: {tag}"


class TestIsolationContract:
    def test_declares_task_as_only_context(self, worker_safe_prompt: str) -> None:
        assert "ONLY authoritative context" in worker_safe_prompt

    def test_declares_parent_history_unavailable(self, worker_safe_prompt: str) -> None:
        assert "NOT available" in worker_safe_prompt

    def test_describes_governed_bounded_nested_delegation(self, worker_safe_prompt: str) -> None:
        lowered = worker_safe_prompt.lower()
        assert "nested delegation" in lowered
        assert "depth" in lowered
        assert "cycle" in lowered
        assert "delegation tools are disabled" not in lowered

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


class TestSlimReturnNoForcedFormat:
    """F-1: the forced 3-section return template + examples are GONE (L1 fix)."""

    def test_no_return_format_template_or_examples(self, worker_safe_prompt: str) -> None:
        assert "<return_format>" not in worker_safe_prompt
        assert "<good_return_examples>" not in worker_safe_prompt
        assert "<bad_return_examples>" not in worker_safe_prompt

    def test_no_forced_three_section_scaffold(self, worker_safe_prompt: str) -> None:
        # The rigid "Completed:/Evidence:/Blockers:" return scaffold is removed —
        # the worker is not told how to shape its return.
        assert "Completed:" not in worker_safe_prompt
        assert "Evidence:" not in worker_safe_prompt


class TestPeerFraming:
    def test_names_delegating_peer_when_given(self) -> None:
        prompt = _build_delegated_worker_prompt(_DELEGATION_TOOL_PROFILES["worker_safe"], parent_name="Atlas")
        assert "Atlas" in prompt
        # Descriptive colleague framing, not a return-format template.
        assert "委派" in prompt

    def test_generic_framing_without_peer_name(self, worker_safe_prompt: str) -> None:
        assert "委派" in worker_safe_prompt


class TestExportedSuffix:
    def test_suffix_uses_worker_safe_profile(self) -> None:
        # _DELEGATED_WORKER_PROMPT_SUFFIX should equal the worker_safe build.
        expected = _build_delegated_worker_prompt(_DELEGATION_TOOL_PROFILES["worker_safe"])
        assert _DELEGATED_WORKER_PROMPT_SUFFIX == expected

    def test_suffix_is_non_empty_string(self) -> None:
        assert isinstance(_DELEGATED_WORKER_PROMPT_SUFFIX, str)
        assert len(_DELEGATED_WORKER_PROMPT_SUFFIX) > 300
