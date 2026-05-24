from __future__ import annotations


def test_dynamic_suffix_includes_session_learning_projection() -> None:
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(
        memory_snapshot="T3 snapshot: existing durable memory",
        session_learning_projection="## Session Learning\n- [candidate][user_stated] Use npm. candidate=cand-1",
    )

    assert "## Session Learning" in suffix
    assert "Use npm" in suffix
    assert "T3 snapshot" in suffix


def test_session_learning_projection_never_enters_frozen_prefix() -> None:
    from app.runtime.prompt_builder import build_frozen_prompt_prefix

    prefix = build_frozen_prompt_prefix(
        agent_context="You are TestAgent.",
        memory_snapshot="## Session Learning\n- [candidate] Use npm.",
    )

    assert "## Session Learning" not in prefix
    assert "Use npm" not in prefix
