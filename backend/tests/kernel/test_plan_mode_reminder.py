"""Plan Mode reminder texts + activation notice.

The FULL-once / SPARSE-cooldown injection state machine moved to
``kernel/reminder_scheduler.py`` (T-G1) and is pinned in
``test_runtime_reminder_scheduler.py``. This file keeps the load-bearing
TEXT pins (what the reminders must teach) and the B3 activation notice,
which stays an engine concern.
"""

from __future__ import annotations

from app.kernel.reminder_scheduler import (
    _PLAN_MODE_REMINDER_FULL,
    _PLAN_MODE_REMINDER_SPARSE,
)


def test_full_reminder_carries_the_load_bearing_constraints():
    # Migrated assertions from the retired _interactive_plan_mode_suffix test:
    # the FULL reminder must still teach read-only + exit_plan_mode as the only
    # approval exit, plus the v4 fact-discipline ("assumption") rule.
    full = _PLAN_MODE_REMINDER_FULL
    assert "Plan Mode is active" in full
    assert "MUST NOT" in full
    assert "read-only" in full
    assert "exit_plan_mode" in full
    assert "assumption" in full


def test_sparse_reminder_is_shorter_but_keeps_exit_and_readonly():
    sparse = _PLAN_MODE_REMINDER_SPARSE
    assert "exit_plan_mode" in sparse
    assert "read-only" in sparse
    # Sparse is a per-round nudge, not the full briefing.
    assert len(sparse) < len(_PLAN_MODE_REMINDER_FULL)


# ── B3: explicit activation notice (docs/agent-lifecycle-cc-alignment.md 主题 B) ──


def test_plan_mode_activation_notice_names_blocked_action() -> None:
    """When tool-intercept flips the run into Plan Mode, the model must be told
    WHY (which action was gated) and WHAT to do now — not just 'suddenly read-only'."""
    from types import SimpleNamespace

    from app.kernel.engine import _plan_mode_activation_notice

    request = SimpleNamespace(
        session_context=SimpleNamespace(
            plan_mode=SimpleNamespace(
                active=True,
                tool_name="set_trigger",
                intent_type="autonomous_wake",
            )
        )
    )

    notice = _plan_mode_activation_notice(request)

    assert "set_trigger" in notice  # names the gated action
    assert "autonomous_wake" in notice  # carries the classified intent
    assert "Plan Mode" in notice
    assert "read-only" in notice  # explains the sudden constraint
    assert "exit_plan_mode" in notice  # the concrete next step
    assert "do NOT retry" in notice  # anti-drift: don't hammer the gated tool


def test_plan_mode_activation_notice_defaults_when_state_sparse() -> None:
    from types import SimpleNamespace

    from app.kernel.engine import _plan_mode_activation_notice

    request = SimpleNamespace(session_context=SimpleNamespace(plan_mode=None))

    notice = _plan_mode_activation_notice(request)

    assert "Plan Mode" in notice
    assert "exit_plan_mode" in notice
