from __future__ import annotations

from app.services.focus_state import normalize_focus_task_id


def test_normalize_focus_task_id_slugifies_and_lowercases() -> None:
    assert normalize_focus_task_id("Daily News Check") == "daily_news_check"
    assert normalize_focus_task_id("send-invite!") == "send_invite"


def test_normalize_focus_task_id_collapses_runs_and_trims() -> None:
    assert normalize_focus_task_id("  __Confirm   Slots__  ") == "confirm_slots"


def test_normalize_focus_task_id_falls_back_to_task_for_empty() -> None:
    assert normalize_focus_task_id("") == "task"
    assert normalize_focus_task_id("***") == "task"
