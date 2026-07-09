"""A2: continuation prompt must carry the three benchmark audit sections."""

from __future__ import annotations


def test_continuation_prompt_has_three_audit_sections():
    from app.runtime.prompts.goals import ThreadGoalPromptState, continuation_prompt

    goal = ThreadGoalPromptState(objective="Ship parity", tokens_used=10, token_budget=100)
    text = continuation_prompt(goal).lower()

    # Completion / Blocked / Fidelity audit gates (vendor-neutral wording).
    assert "completion" in text
    assert "blocked" in text
    assert "fidelity" in text or "scope" in text
    # The completion gate must route through the update_goal tool, not just prose.
    assert "update_goal" in text
    # Fidelity gate forbids scope drift.
    assert "drift" in text or "out of scope" in text or "outside" in text


def test_continuation_prompt_preserves_session_goal_state_block():
    from app.runtime.prompts.goals import ThreadGoalPromptState, continuation_prompt

    goal = ThreadGoalPromptState(objective="Ship <parity>", tokens_used=25, token_budget=100)
    text = continuation_prompt(goal)

    assert "<session_goal>" in text
    assert "&lt;parity&gt;" in text
    assert "<remaining_tokens>75</remaining_tokens>" in text


def test_continuation_prompt_is_vendor_neutral():
    from app.runtime.prompts.goals import ThreadGoalPromptState, continuation_prompt

    text = continuation_prompt(ThreadGoalPromptState(objective="x")).lower()
    for vendor in ("codex", "claude", "anthropic", "openai", "gpt"):
        assert vendor not in text
