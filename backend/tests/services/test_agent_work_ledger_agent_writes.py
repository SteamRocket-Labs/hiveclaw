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
