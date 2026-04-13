from __future__ import annotations

from app.services.agent_manager import _render_focus_from_blueprint
from app.services.focus_state import extract_completed_focus_refs, parse_focus_tasks


def test_render_focus_from_blueprint_uses_canonical_checklist_ids() -> None:
    content = _render_focus_from_blueprint(
        focus_content="Follow up on onboarding tasks",
        first_tasks=[
            "confirm_slots :: confirm the attendee's available meeting slots",
            "send_invite :: send the calendar invite after confirmation",
        ],
    )

    assert "## Tasks" in content
    assert "- [ ] confirm_slots :: confirm the attendee's available meeting slots" in content
    assert "- [ ] send_invite :: send the calendar invite after confirmation" in content
    assert "## First 3 Tasks" not in content


def test_render_focus_from_blueprint_falls_back_to_stable_default_ids() -> None:
    content = _render_focus_from_blueprint(
        focus_content="Stabilize trigger follow-up flow",
        first_tasks=["Review existing trigger contracts"],
    )

    assert "- [ ] task_1 :: Review existing trigger contracts" in content


def test_extract_completed_focus_refs_reads_task_ids() -> None:
    focus_text = """
# Focus

## Tasks
- [x] confirm_slots :: confirm the attendee's available meeting slots
- [ ] send_invite :: send the calendar invite after confirmation
""".strip()

    completed = extract_completed_focus_refs(focus_text)

    assert completed == {"confirm_slots"}


def test_parse_focus_tasks_returns_canonical_entries() -> None:
    focus_text = """
# Focus

## Tasks
- [x] confirm_slots :: confirm the attendee's available meeting slots
- [ ] send_invite :: send the calendar invite after confirmation
""".strip()

    tasks = parse_focus_tasks(focus_text)

    assert [task.task_id for task in tasks] == ["confirm_slots", "send_invite"]
    assert tasks[0].completed is True
    assert tasks[1].completed is False
