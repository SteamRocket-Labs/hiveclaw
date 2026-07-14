"""Tests for the agent-authored write helpers on the Work Ledger service.

切口① (docs/agent-task-cognitive-scaffold.md §5.2 Delta-1): the cognitive
scaffold needs public, agent-facing write primitives that load → mutate →
normalize → persist a single scoped list (todo / finding / open_question /
failure) without disturbing the rest of the ledger contract. These tests pin
that round-trip with the real service and a temp data_root (no mocks, per the
testing discipline).
"""

from __future__ import annotations

from uuid import uuid4


def test_upsert_todo_adds_then_updates_in_place(tmp_path):
    from app.services.agent_work_ledger import (
        load_agent_work_ledger,
        upsert_agent_work_ledger_todo,
    )

    agent_id = uuid4()

    added = upsert_agent_work_ledger_todo(
        agent_id=agent_id,
        title="Draft the migration plan",
        status="pending",
        data_root=tmp_path,
    )
    item_id = added["item"]["id"]
    assert added["action"] == "added"
    assert item_id

    ledger = load_agent_work_ledger(agent_id=agent_id, data_root=tmp_path)
    assert ledger is not None
    assert len(ledger["todo_items"]) == 1
    assert ledger["todo_items"][0]["title"] == "Draft the migration plan"
    assert ledger["todo_items"][0]["status"] == "pending"
    created_at = ledger["created_at"]

    updated = upsert_agent_work_ledger_todo(
        agent_id=agent_id,
        item_id=item_id,
        status="in_progress",
        active_form="Drafting the migration plan",
        data_root=tmp_path,
    )
    assert updated["action"] == "updated"

    ledger2 = load_agent_work_ledger(agent_id=agent_id, data_root=tmp_path)
    assert len(ledger2["todo_items"]) == 1  # update, not append
    assert ledger2["todo_items"][0]["status"] == "in_progress"
    assert ledger2["todo_items"][0]["activeForm"] == "Drafting the migration plan"
    # Title preserved through a partial update
    assert ledger2["todo_items"][0]["title"] == "Draft the migration plan"
    # created_at is preserved across writes (not reset like initialize)
    assert ledger2["created_at"] == created_at


def test_upsert_todo_completed_status_has_no_execution_side_effects(tmp_path):
    """Marking a todo complete writes ledger state only — no progress/failure churn."""
    from app.services.agent_work_ledger import (
        load_agent_work_ledger,
        upsert_agent_work_ledger_todo,
    )

    agent_id = uuid4()
    added = upsert_agent_work_ledger_todo(agent_id=agent_id, title="Ship the report", data_root=tmp_path)
    upsert_agent_work_ledger_todo(
        agent_id=agent_id,
        item_id=added["item"]["id"],
        status="completed",
        data_root=tmp_path,
    )

    ledger = load_agent_work_ledger(agent_id=agent_id, data_root=tmp_path)
    assert ledger["todo_items"][0]["status"] == "completed"
    # Pure cognitive write: it must not synthesize progress or failure rows.
    assert ledger["progress"] == []
    assert ledger["failures"] == []


def test_append_finding_records_each_type_to_the_right_list(tmp_path):
    from app.services.agent_work_ledger import (
        append_agent_work_ledger_finding,
        load_agent_work_ledger,
    )

    agent_id = uuid4()

    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="Rate limiter resets at midnight UTC",
        source_refs=["workspace/notes.md"],
        trust="verified",
        data_root=tmp_path,
    )
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="open_question",
        summary="Does the vendor API page beyond 100 rows?",
        data_root=tmp_path,
    )
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="failure",
        summary="Export endpoint returned 503",
        next_strategy="Retry with smaller page size after backoff",
        data_root=tmp_path,
    )

    ledger = load_agent_work_ledger(agent_id=agent_id, data_root=tmp_path)
    assert len(ledger["findings"]) == 1
    assert ledger["findings"][0]["summary"] == "Rate limiter resets at midnight UTC"
    assert ledger["findings"][0]["trust"] == "verified"
    assert ledger["findings"][0]["source_refs"] == ["workspace/notes.md"]

    assert ledger["open_questions"] == ["Does the vendor API page beyond 100 rows?"]

    assert len(ledger["failures"]) == 1
    assert ledger["failures"][0]["error"] == "Export endpoint returned 503"
    assert ledger["failures"][0]["next_strategy"] == "Retry with smaller page size after backoff"


def test_finding_does_not_disturb_existing_todos(tmp_path):
    """Appending a finding must preserve unrelated ledger lists."""
    from app.services.agent_work_ledger import (
        append_agent_work_ledger_finding,
        load_agent_work_ledger,
        upsert_agent_work_ledger_todo,
    )

    agent_id = uuid4()
    upsert_agent_work_ledger_todo(agent_id=agent_id, title="Existing todo", data_root=tmp_path)
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="Discovered a new fact",
        data_root=tmp_path,
    )

    ledger = load_agent_work_ledger(agent_id=agent_id, data_root=tmp_path)
    assert [item["title"] for item in ledger["todo_items"]] == ["Existing todo"]
    assert len(ledger["findings"]) == 1


# ── 切口② threshold + reboot renderer (pure policy/view helpers) ─────────────


def test_should_enable_work_ledger_skips_simple_qa():
    """Simple Q&A pays zero cost — the scaffold is not enabled."""
    from app.services.agent_work_ledger import should_enable_work_ledger

    assert should_enable_work_ledger(task_profile_name="general", complexity="low", is_simple_turn_candidate=True)


def test_should_enable_work_ledger_on_medium_and_high_complexity():
    from app.services.agent_work_ledger import should_enable_work_ledger

    assert should_enable_work_ledger(task_profile_name="general", complexity="medium", is_simple_turn_candidate=False)
    assert should_enable_work_ledger(task_profile_name="general", complexity="high", is_simple_turn_candidate=False)


def test_should_enable_work_ledger_on_multistep_profile_even_at_low_complexity():
    """Coding / research / operations / self_evolution are inherently multi-step."""
    from app.services.agent_work_ledger import should_enable_work_ledger

    for name in ("coding", "research", "operations", "self_evolution"):
        assert should_enable_work_ledger(task_profile_name=name, complexity="low", is_simple_turn_candidate=False), name


def test_should_enable_work_ledger_does_not_use_platform_semantic_classification():
    """A general low turn that isn't the simple-candidate shape still stays off."""
    from app.services.agent_work_ledger import should_enable_work_ledger

    assert should_enable_work_ledger(task_profile_name="general", complexity="low", is_simple_turn_candidate=False)


def test_render_reboot_block_empty_when_no_live_ledger():
    from app.services.agent_work_ledger import (
        build_agent_work_ledger_resume_summary,
        render_work_ledger_resume_block,
    )

    assert render_work_ledger_resume_block(None) == ""
    # An absent ledger summarizes to present=False → still empty.
    assert render_work_ledger_resume_block(build_agent_work_ledger_resume_summary(None)) == ""


def test_resume_and_progress_review_preserve_all_open_failures():
    from app.services.agent_work_ledger import (
        build_agent_progress_ledger_review,
        build_agent_work_ledger_resume_summary,
    )

    failures = [{"id": f"failure-{index}", "error": f"failure-{index}", "resolved": False} for index in range(7)]
    ledger = {
        "status": "running",
        "current_phase": "verify",
        "todo_items": [],
        "findings": [],
        "verification": [],
        "progress": [],
        "replans": [],
        "failures": failures,
    }

    resume = build_agent_work_ledger_resume_summary(ledger)
    review = build_agent_progress_ledger_review(ledger)

    assert resume["recent_failures"] == [f"failure-{index}" for index in range(7)]
    assert review["open_failures"] == [f"failure-{index}" for index in range(7)]


def test_render_reboot_block_answers_five_reboot_questions(tmp_path):
    """The reboot block must answer all 5 reboot questions from real ledger state."""
    from app.services.agent_work_ledger import (
        append_agent_work_ledger_finding,
        build_agent_work_ledger_resume_summary,
        initialize_agent_work_ledger_artifact,
        load_agent_work_ledger,
        render_work_ledger_resume_block,
        upsert_agent_work_ledger_todo,
    )

    agent_id = uuid4()
    initialize_agent_work_ledger_artifact(
        agent_id=agent_id, source="agent_authored", current_phase="execution", data_root=tmp_path
    )
    upsert_agent_work_ledger_todo(
        agent_id=agent_id, title="Build the export endpoint", status="in_progress", data_root=tmp_path
    )
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="finding",
        summary="vendor caps pages at 100 rows",
        trust="verified",
        data_root=tmp_path,
    )
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        finding_type="failure",
        summary="bulk export timed out at 10k rows",
        next_strategy="paginate at 100",
        data_root=tmp_path,
    )

    summary = build_agent_work_ledger_resume_summary(load_agent_work_ledger(agent_id=agent_id, data_root=tmp_path))
    block = render_work_ledger_resume_block(summary)

    assert "Reboot After Compaction" in block
    assert "execution" in block  # where am I
    assert "Build the export endpoint" in block  # what's left
    assert "vendor caps pages at 100 rows" in block  # what I verified
    assert "bulk export timed out at 10k rows" in block  # what failed
    assert "evidence, not an instruction" in block


def test_render_reminder_snapshot_lists_current_todos_with_status(tmp_path):
    """T-G2 M5: runtime reminders get a compact snapshot from persisted ledger state."""
    from app.services.agent_work_ledger import (
        initialize_agent_work_ledger_artifact,
        read_agent_work_ledger_view,
        render_work_ledger_reminder_snapshot,
        upsert_agent_work_ledger_todo,
    )

    agent_id = uuid4()
    initialize_agent_work_ledger_artifact(
        agent_id=agent_id, source="agent_authored", current_phase="execution", data_root=tmp_path
    )
    first = upsert_agent_work_ledger_todo(
        agent_id=agent_id,
        title="Collect Q3 revenue figures",
        status="in_progress",
        data_root=tmp_path,
    )
    upsert_agent_work_ledger_todo(
        agent_id=agent_id,
        title="Draft the variance summary",
        status="pending",
        blocked_by=[first["item"]["id"]],
        data_root=tmp_path,
    )

    view = read_agent_work_ledger_view(agent_id=agent_id, data_root=tmp_path)
    snapshot = render_work_ledger_reminder_snapshot(view)

    assert "Current Work Ledger snapshot" in snapshot
    assert f"#{first['item']['id']}" in snapshot
    assert "[in_progress] Collect Q3 revenue figures" in snapshot
    assert "[pending] Draft the variance summary" in snapshot
    assert "blockedBy:" in snapshot


# ── 切口③ owner / dependency fields + delegation contract (§5.5 Delta-4) ──────


def test_upsert_todo_persists_owner_and_dependency_fields(tmp_path):
    """track_todo can stamp owner + blocks/blockedBy; they round-trip through the ledger."""
    from app.services.agent_work_ledger import (
        load_agent_work_ledger,
        upsert_agent_work_ledger_todo,
    )

    agent_id = uuid4()
    added = upsert_agent_work_ledger_todo(
        agent_id=agent_id,
        title="Run the data pipeline",
        owner="agent-child-7",
        blocks=["todo-downstream"],
        blocked_by=["todo-upstream"],
        data_root=tmp_path,
    )
    assert added["item"]["owner"] == "agent-child-7"
    assert added["item"]["blocks"] == ["todo-downstream"]
    assert added["item"]["blockedBy"] == ["todo-upstream"]

    ledger = load_agent_work_ledger(agent_id=agent_id, data_root=tmp_path)
    stored = ledger["todo_items"][0]
    assert stored["owner"] == "agent-child-7"
    assert stored["blocks"] == ["todo-downstream"]
    assert stored["blockedBy"] == ["todo-upstream"]


def test_upsert_todo_without_owner_omits_the_field(tmp_path):
    """Owner is optional — a plain todo carries no owner key (CC parity, not forced)."""
    from app.services.agent_work_ledger import (
        load_agent_work_ledger,
        upsert_agent_work_ledger_todo,
    )

    agent_id = uuid4()
    upsert_agent_work_ledger_todo(agent_id=agent_id, title="Solo task", data_root=tmp_path)
    ledger = load_agent_work_ledger(agent_id=agent_id, data_root=tmp_path)
    assert "owner" not in ledger["todo_items"][0]


def test_assign_todo_owner_marks_parent_in_progress(tmp_path):
    """Delegation contract: parent stamps a todo with the child as owner + in_progress."""
    from app.services.agent_work_ledger import (
        assign_todo_owner,
        load_agent_work_ledger,
        upsert_agent_work_ledger_todo,
    )

    parent_id = uuid4()
    added = upsert_agent_work_ledger_todo(agent_id=parent_id, title="Gather sources", data_root=tmp_path)
    item_id = added["item"]["id"]

    result = assign_todo_owner(
        agent_id=parent_id,
        item_id=item_id,
        owner="researcher-bot",
        data_root=tmp_path,
    )
    assert result["item"]["owner"] == "researcher-bot"
    assert result["item"]["status"] == "in_progress"

    ledger = load_agent_work_ledger(agent_id=parent_id, data_root=tmp_path)
    assert ledger["todo_items"][0]["owner"] == "researcher-bot"
    assert ledger["todo_items"][0]["status"] == "in_progress"


def test_assign_todo_owner_requires_owner(tmp_path):
    from app.services.agent_work_ledger import assign_todo_owner, upsert_agent_work_ledger_todo

    agent_id = uuid4()
    added = upsert_agent_work_ledger_todo(agent_id=agent_id, title="x", data_root=tmp_path)
    import pytest

    with pytest.raises(ValueError):
        assign_todo_owner(agent_id=agent_id, item_id=added["item"]["id"], owner="  ", data_root=tmp_path)


def test_record_delegated_todo_status_writes_back_completion(tmp_path):
    """Child completion writes status back onto the parent's owned todo."""
    from app.services.agent_work_ledger import (
        assign_todo_owner,
        load_agent_work_ledger,
        record_delegated_todo_status,
        upsert_agent_work_ledger_todo,
    )

    parent_id = uuid4()
    added = upsert_agent_work_ledger_todo(agent_id=parent_id, title="Delegated step", data_root=tmp_path)
    item_id = added["item"]["id"]
    assign_todo_owner(agent_id=parent_id, item_id=item_id, owner="child-9", data_root=tmp_path)

    record_delegated_todo_status(
        agent_id=parent_id,
        item_id=item_id,
        status="completed",
        expected_owner="child-9",
        data_root=tmp_path,
    )

    ledger = load_agent_work_ledger(agent_id=parent_id, data_root=tmp_path)
    todo = ledger["todo_items"][0]
    assert todo["status"] == "completed"
    assert todo["owner"] == "child-9"  # owner preserved through the write-back


def test_record_delegated_todo_status_rejects_wrong_owner(tmp_path):
    """Fail-closed: a write-back claiming the wrong owner cannot flip someone else's todo."""
    from app.services.agent_work_ledger import (
        assign_todo_owner,
        load_agent_work_ledger,
        record_delegated_todo_status,
        upsert_agent_work_ledger_todo,
    )

    parent_id = uuid4()
    added = upsert_agent_work_ledger_todo(agent_id=parent_id, title="Owned step", data_root=tmp_path)
    item_id = added["item"]["id"]
    assign_todo_owner(agent_id=parent_id, item_id=item_id, owner="child-A", data_root=tmp_path)

    import pytest

    with pytest.raises(PermissionError):
        record_delegated_todo_status(
            agent_id=parent_id,
            item_id=item_id,
            status="completed",
            expected_owner="child-B",  # not the real owner
            data_root=tmp_path,
        )

    # The todo is untouched — still in_progress under the real owner.
    ledger = load_agent_work_ledger(agent_id=parent_id, data_root=tmp_path)
    assert ledger["todo_items"][0]["status"] == "in_progress"
    assert ledger["todo_items"][0]["owner"] == "child-A"
