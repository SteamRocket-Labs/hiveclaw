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
    assert "only valid ways to end" in full
    assert "decision-complete" in full
    assert "targeted read-only exploration" in full
    assert "private scratchpad" in full
    assert "not the approval artifact" in full


def test_sparse_reminder_is_shorter_but_keeps_exit_and_readonly():
    sparse = _PLAN_MODE_REMINDER_SPARSE
    assert "exit_plan_mode" in sparse
    assert "read-only" in sparse
    assert "only valid ways to end" in sparse
    # Sparse is a per-round nudge, not the full briefing.
    assert len(sparse) < len(_PLAN_MODE_REMINDER_FULL)


def test_full_reminder_is_domain_neutral_and_clarification_first():
    """Plan-mode CC alignment Phase A: the reminder must be domain-neutral (not
    coding-default), route blocking clarifications to ask_user_question, and make
    plan_markdown the plan body — not a field-filling checklist."""
    full = _PLAN_MODE_REMINDER_FULL
    lower = full.lower()
    # domain-neutral: research/non-coding tasks are first-class, no coding default
    assert "research" in lower
    assert "software" in lower  # the "do not assume ... software" guard
    # clarification is a first-class tool, not prose-or-assume
    assert "ask_user_question" in full
    # plan_markdown is the user-facing plan body
    assert "plan_markdown" in full
    # no longer dumps the field-filling checklist as the main job
    assert "motivation, ordered steps" not in full


def test_full_reminder_scopes_workspace_reads_to_relevant_context():
    """Plan Mode may read workspace context, but it must not default to browsing
    old artifacts. The reminder should teach need-scoped reads and provenance so
    historical reports/JSON do not silently become the current plan input."""
    full = _PLAN_MODE_REMINDER_FULL
    assert "Do not browse the workspace root" in full
    assert "historical artifact" in full
    assert "need-scoped" in full
    assert "reference" in full


def test_full_reminder_teaches_narrow_readonly_helper_lane():
    full = _PLAN_MODE_REMINDER_FULL
    assert "preview_workflow" in full
    assert "spawn_subagent" in full
    assert "explorer/critic" in full
    assert "run_in_background" in full
    assert "worker" in full


def test_sparse_reminder_routes_clarification_and_plan_body():
    sparse = _PLAN_MODE_REMINDER_SPARSE
    assert "ask_user_question" in sparse
    assert "plan_markdown" in sparse


def test_plan_file_hint_makes_file_the_plan_source():
    from app.runtime.prompts.runtime_reminders import PLAN_MODE_FILE_HINT

    hint = PLAN_MODE_FILE_HINT.format(plan_file="workspace/plans/session.plan.md")
    assert "workspace/plans/session.plan.md" in hint
    assert "source of plan_markdown" in hint
    assert "exact file" in hint


# Tool-intercept activation notices were removed with explicit-only Plan Mode.
