"""Phase 2 contract: per-round Plan Mode reminder injection (engine pure core).

The engine injects a FULL reminder on the first planning round (and re-arms it
after a compaction), then a SPARSE reminder every subsequent round. This keeps
the agent in read-only planning every round without bloating the frozen system
prompt — the reminder used to live in ``system_prompt_suffix`` and is now an
engine-injected ``role="system"`` message (paradigm-convergence doc §6.2).

These tests target the pure decision functions so they need no kernel mocks.
"""

from __future__ import annotations

from app.kernel.engine import (
    _PLAN_MODE_REMINDER_FULL,
    _PLAN_MODE_REMINDER_SPARSE,
    _plan_mode_reminder_content,
    _reset_plan_reminder,
)
from app.runtime.session import PlanModeState, SessionContext


def test_reminder_content_is_none_for_missing_or_inactive_state():
    assert _plan_mode_reminder_content(None) is None
    assert _plan_mode_reminder_content(PlanModeState()) is None
    assert _plan_mode_reminder_content(PlanModeState(active=False, reminded_full=True)) is None


def test_first_round_returns_full_reminder():
    state = PlanModeState(active=True)
    result = _plan_mode_reminder_content(state)
    assert result is not None
    content, is_full = result
    assert is_full is True
    assert content == _PLAN_MODE_REMINDER_FULL


def test_subsequent_rounds_return_sparse_reminder():
    state = PlanModeState(active=True, reminded_full=True)
    result = _plan_mode_reminder_content(state)
    assert result is not None
    content, is_full = result
    assert is_full is False
    assert content == _PLAN_MODE_REMINDER_SPARSE


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


def test_reset_plan_reminder_rearms_full_for_active_state():
    ctx = SessionContext()
    ctx.plan_mode = PlanModeState(active=True, reminded_full=True)
    _reset_plan_reminder(ctx)
    assert ctx.plan_mode.reminded_full is False


def test_reset_plan_reminder_is_noop_when_inactive_or_missing():
    # Inactive plan state is untouched.
    ctx = SessionContext()
    ctx.plan_mode = PlanModeState(active=False, reminded_full=True)
    _reset_plan_reminder(ctx)
    assert ctx.plan_mode.reminded_full is True
    # No session context / no plan_mode attribute must not raise.
    _reset_plan_reminder(None)
    _reset_plan_reminder(object())


def test_full_reminder_names_the_plan_file_when_provisioned():
    # Phase 4B: when a plan file is provisioned, the FULL reminder appends a hint
    # naming that exact writable path (base text preserved as the prefix).
    state = PlanModeState(active=True, plan_file_path="workspace/plans/s1.plan.md")
    result = _plan_mode_reminder_content(state)
    assert result is not None
    content, is_full = result
    assert is_full is True
    assert content.startswith(_PLAN_MODE_REMINDER_FULL)
    assert "workspace/plans/s1.plan.md" in content


def test_full_reminder_omits_file_hint_when_no_plan_file():
    # No plan file (Phase 3 behaviour / unattended fallback) → exact base text.
    result = _plan_mode_reminder_content(PlanModeState(active=True))
    assert result is not None
    content, _ = result
    assert content == _PLAN_MODE_REMINDER_FULL


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
