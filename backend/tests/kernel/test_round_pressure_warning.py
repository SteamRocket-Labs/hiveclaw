"""B2 (docs/agent-lifecycle-cc-alignment.md 主题 B): round-pressure warnings
carry real data (CC token-budget-nudge style), not bare milestones."""

from __future__ import annotations


def test_round_pressure_warning_carries_real_data() -> None:
    from app.kernel.engine import _build_round_pressure_warning

    text = _build_round_pressure_warning(
        round_i=160,
        max_rounds=200,
        total_tool_calls=87,
        failed_tool_calls=3,
        context_tokens=145_000,
        final=False,
    )

    assert "160/200" in text  # rounds used
    assert "87" in text  # tool calls so far
    assert "3" in text  # failures
    assert "145,000" in text  # context size estimate
    assert "Objective Ledger" in text  # existing guidance preserved


def test_round_pressure_final_warning_states_two_rounds_left() -> None:
    from app.kernel.engine import _build_round_pressure_warning

    text = _build_round_pressure_warning(
        round_i=198,
        max_rounds=200,
        total_tool_calls=120,
        failed_tool_calls=10,
        context_tokens=180_000,
        final=True,
    )

    assert "2" in text and "remaining" in text.lower()
    assert "120" in text
    assert "Objective Ledger" in text
    assert "stop cleanly" in text  # wind-down guidance preserved
