"""Tests for A2A_SYSTEM_PROMPT_SUFFIX (PR-19).

Agent-to-Agent messaging is the protocol peer agents use to exchange
requests. The suffix shapes every reply a receiving agent produces, so
format/structure regressions silently degrade cross-agent collaboration.
"""

from __future__ import annotations

import pytest

from app.services.agent_tool_domains.messaging import A2A_SYSTEM_PROMPT_SUFFIX


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return A2A_SYSTEM_PROMPT_SUFFIX


class TestPromptStructure:
    def test_xml_tags_present(self, prompt_text: str) -> None:
        for tag in [
            "<role>",
            "</role>",
            "<reply_format>",
            "<privacy_boundary>",
            "<anti_patterns>",
            "<good_reply_example>",
            "<bad_reply_example>",
        ]:
            assert tag in prompt_text, f"missing tag: {tag}"

    def test_role_clarifies_peer_not_human(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "peer agent" in lowered
        assert "not a human" in lowered


class TestReplyFormat:
    def test_requires_concrete_evidence(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "file path" in lowered or "tool-result" in lowered
        assert "evidence" in lowered

    def test_handles_three_states(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        # 1. clear → concrete answer
        # 2. incomplete → name missing piece
        # 3. in-progress → status update with blocker
        # 4. cannot complete → specific missing/blocked
        assert "still working" in lowered
        assert "incomplete" in lowered or "ambiguous" in lowered
        assert "cannot complete" in lowered


class TestPrivacyBoundary:
    def test_protects_workspace_files(self, prompt_text: str) -> None:
        # Must name specific private files.
        for path in ["memory/*.md", "tasks.json", "soul.md", "logs/"]:
            assert path in prompt_text, f"missing protected path: {path}"

    def test_no_cross_sender_leakage(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "other senders" in lowered or "other conversations" in lowered


class TestAntiPatterns:
    def test_allows_bounded_nested_delegation(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "bounded nested delegation" in lowered
        assert "delegate_to_agent" in prompt_text
        assert "do not call `delegate_to_agent`" not in lowered
        assert "depth" in lowered and "cycle" in lowered and "budget" in lowered

    def test_rejects_pleasantries(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "pleasantries" in lowered or "filler" in lowered
        # Should cite specific filler examples the LLM might use.
        assert "happy to help" in lowered or "let me know if" in lowered

    def test_rejects_guessing_at_intent(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "guessing at ambiguous" in lowered or "silently pick" in lowered

    def test_rejects_evidence_free_completion(self, prompt_text: str) -> None:
        lowered = prompt_text.lower()
        assert "claiming completion without evidence" in lowered or "without evidence" in lowered


class TestGoodExample:
    def test_has_good_example(self, prompt_text: str) -> None:
        assert "<good_reply_example>" in prompt_text
        # Good example must show a CI-pipeline-style structured answer with
        # file:line evidence.
        assert "CI pipeline" in prompt_text
        assert ".github/workflows" in prompt_text


class TestBadExample:
    def test_has_bad_example(self, prompt_text: str) -> None:
        assert "<bad_reply_example>" in prompt_text
        # Bad example must show the exact pleasantry-laden non-answer anti-pattern.
        lowered = prompt_text.lower()
        assert "sure thing" in lowered or "pretty standard" in lowered
        assert "useless to a peer" in lowered


class TestTypeAndShape:
    def test_is_string(self) -> None:
        assert isinstance(A2A_SYSTEM_PROMPT_SUFFIX, str)
        # Meaningful length — the old version was ~700 chars; new one is ≥2k.
        assert len(A2A_SYSTEM_PROMPT_SUFFIX) > 1500
