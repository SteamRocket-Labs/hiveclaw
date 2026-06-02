"""Tests for prompt caching — frozen prefix reuse within a session."""

import uuid

import pytest

from app.runtime.session import SessionContext


@pytest.fixture
def session_ctx():
    return SessionContext(session_id=str(uuid.uuid4()), source="test")


@pytest.fixture
def agent_context_text():
    return "You are TestAgent, a helpful assistant."


@pytest.fixture
def memory_snapshot():
    return "[Memory] User prefers concise answers."


def test_frozen_prefix_cached_on_first_call(session_ctx, agent_context_text, memory_snapshot):
    """First call should build and cache the frozen prefix."""
    from app.runtime.prompt_builder import build_frozen_prompt_prefix

    prefix = build_frozen_prompt_prefix(
        agent_context=agent_context_text,
        memory_snapshot=memory_snapshot,
        skill_catalog="",
    )
    assert agent_context_text in prefix
    assert memory_snapshot not in prefix

    session_ctx.prompt_prefix = prefix
    session_ctx.prompt_fingerprint = "test_fp"

    assert session_ctx.prompt_prefix is not None
    assert session_ctx.prompt_fingerprint is not None


def test_frozen_prefix_stable_across_calls(agent_context_text, memory_snapshot):
    """Same inputs should produce identical frozen prefix."""
    from app.runtime.prompt_builder import build_frozen_prompt_prefix

    prefix1 = build_frozen_prompt_prefix(
        agent_context=agent_context_text,
        memory_snapshot=memory_snapshot,
        skill_catalog="",
    )
    prefix2 = build_frozen_prompt_prefix(
        agent_context=agent_context_text,
        memory_snapshot=memory_snapshot,
        skill_catalog="",
    )
    assert prefix1 == prefix2


def test_new_session_gets_fresh_prefix():
    """Different sessions should not share cached prefix."""
    s1 = SessionContext(session_id="session-1")
    s2 = SessionContext(session_id="session-2")

    s1.prompt_prefix = "prefix-for-s1"
    assert s2.prompt_prefix is None


def test_dynamic_suffix_changes_with_active_packs():
    """Dynamic suffix should reflect active_packs changes."""
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix1 = build_dynamic_prompt_suffix(
        active_tool_groups=[],
        retrieval_context="",
        system_prompt_suffix="",
    )

    suffix2 = build_dynamic_prompt_suffix(
        active_tool_groups=[{"name": "web_pack", "summary": "Web search", "tools": ["web_search"]}],
        retrieval_context="",
        system_prompt_suffix="",
    )

    assert suffix1 != suffix2
    assert "web_pack" in suffix2


def test_dynamic_suffix_includes_retrieval():
    """Dynamic suffix should include retrieval context."""
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(
        active_tool_groups=[],
        retrieval_context="Found: quarterly report Q3 2026",
        system_prompt_suffix="",
    )
    assert "quarterly report" in suffix


def test_session_learning_projection_changes_dynamic_suffix_not_frozen_prefix(agent_context_text):
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix, build_frozen_prompt_prefix

    prefix = build_frozen_prompt_prefix(
        agent_context=agent_context_text,
        memory_snapshot="## Session Learning\n- Use npm.",
        skill_catalog="",
    )
    suffix_without_learning = build_dynamic_prompt_suffix(memory_snapshot="T3 memory")
    suffix_with_learning = build_dynamic_prompt_suffix(
        memory_snapshot="T3 memory",
        session_learning_projection="## Session Learning\n- [candidate] Use npm.",
    )

    assert "Session Learning" not in prefix
    assert suffix_with_learning != suffix_without_learning
    assert "Use npm" in suffix_with_learning


def test_assemble_combines_prefix_and_suffix():
    """assemble_runtime_prompt should combine frozen prefix + dynamic suffix."""
    from app.runtime.prompt_builder import (
        assemble_runtime_prompt,
        build_dynamic_prompt_suffix,
        build_frozen_prompt_prefix,
    )

    prefix = build_frozen_prompt_prefix(
        agent_context="I am Agent.",
        memory_snapshot="Memory here.",
        skill_catalog="",
    )
    suffix = build_dynamic_prompt_suffix(
        memory_snapshot="Memory here.",
        active_tool_groups=[{"name": "web_pack", "summary": "Web", "tools": ["web_search"]}],
        retrieval_context="",
        system_prompt_suffix="Extra instructions.",
    )
    full = assemble_runtime_prompt(prefix, suffix)

    assert "I am Agent." in full
    assert "Memory here." in full
    assert "## Your Memory System" in full
    assert "web_pack" in full
    assert "Extra instructions." in full
