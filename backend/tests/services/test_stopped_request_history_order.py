"""Stop ordering is a transcript-sequence rule, including a late round commit."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("intent", ["steer_current_turn", "interrupt_and_replace", "queue_next_turn"])
@pytest.mark.parametrize("late_commit", [False, True])
async def test_after_click_inputs_follow_matching_stop_and_last_committed_round(monkeypatch, intent, late_commit):
    from app.services import session_semantic_history as history

    run = uuid.uuid4()
    control = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    views = []

    def event(sequence, kind, lifecycle, **payload):
        row = SimpleNamespace(
            id=uuid.uuid4(),
            sequence=sequence,
            item_kind=kind,
            lifecycle=lifecycle,
            run_id=run,
            schema_version=2,
            metadata_json={"v2_payload": payload},
            created_at=now,
            input_id=uuid.uuid4(),
            scope_json={},
            item_id=uuid.uuid4(),
        )
        views.append(history._EventView(target=row, source=row))
        return row

    event(1, "human_input", "accepted", content_parts=[{"type": "text", "text": "Stopped request"}])
    event(2, "result_commit", "round_committed")
    cancel_sequence, commit_sequence = 3, 6 if late_commit else 2
    if intent == "interrupt_and_replace":
        # Real replacement admission precedes both saga creation and cancelling.
        replacement = event(3, "human_input", "accepted", intent=intent,
            content_parts=[{"type": "text", "text": "Later input 4"}])
        saga = event(4, "turn_replacement", "requested", old_run_id=str(run),
            replacement_input_id=str(replacement.input_id))
        saga.input_id = replacement.input_id
        cancel_sequence, commit_sequence = 5, 7 if late_commit else 2
        later_inputs = [(6, 5)]
    else:
        later_inputs = [(4, 4), (5, 5)]
    event(cancel_sequence, "run", "cancelling", control_id=control)
    for sequence, index in later_inputs:
        event(
            sequence,
            "human_input",
            "accepted",
            intent=intent,
            content_parts=[{"type": "text", "text": f"Later input {index}"}],
        )
    if late_commit:
        event(commit_sequence, "result_commit", "round_committed")
    # Another control is not the stop point for this settled cancellation.
    event(8, "run", "cancelling", control_id=str(uuid.uuid4()))
    event(9, "run", "cancelled", control_id=control)

    async def load_views(*_args, **_kwargs):
        return views, {}

    async def rounds(*_args, **_kwargs):
        sequence = commit_sequence
        return (
            [
                history.SessionSemanticMessage(
                    id="last-round",
                    role="assistant",
                    content="Committed work",
                    created_at=now,
                    sequence_start=sequence,
                    sequence_end=sequence,
                    group_id="round",
                    source_event_ids=(),
                )
            ],
            [],
            0,
        )

    class DB:
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [])

    monkeypatch.setattr(history, "_load_event_views", load_views)
    monkeypatch.setattr(history, "_committed_round_messages", rounds)
    result = await history.load_session_semantic_history(
        DB(), tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(), session_id=uuid.uuid4()
    )
    assert [message.content for message in result.messages] == [
        "Stopped request",
        "Committed work",
        "[Request interrupted by user; its plan and todos are stopped. Resume only if asked.]",
        "Later input 4",
        "Later input 5",
    ]
    stop = result.messages[2]
    assert stop.sequence_start == max(cancel_sequence, commit_sequence)
    assert stop.id == f"session-run-cancelled:{views[-1].source.id}"


@pytest.fixture
def many_stopped_events():
    """20k events with 50 stops, branch copies, late commits and replacements."""
    from app.services import session_semantic_history as history

    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    views, expected, windows = [], [], []
    for index in range(50):
        run, control, replacement = uuid.uuid4(), str(uuid.uuid4()), uuid.uuid4()
        block = []
        for offset in range(1, 401):
            payload = {}
            kind, lifecycle, input_id = "provider_call", "observed", None
            if offset == 389:
                kind, lifecycle, input_id = "human_input", "accepted", replacement
            elif offset == 390:
                kind, lifecycle, input_id = "turn_replacement", "requested", replacement
                payload = {"old_run_id": str(run)}
            elif offset in {391, 392}:
                kind, lifecycle = "run", "cancelling"
                payload = {"control_id": control if offset == 392 else str(uuid.uuid4())}
                if offset == 392 and index % 5 == 0:
                    lifecycle = "waiting"  # Missing matching cancelling: use settled event.
            elif offset == 394:
                kind, lifecycle = "result_commit", "round_committed"
            elif offset in {393, 395, 396}:
                kind, lifecycle = "run", "cancelled"
                payload = {"control_id": control} if offset != 393 else {}
            row = SimpleNamespace(
                id=uuid.uuid4(), sequence=index * 400 + offset, item_kind=kind,
                lifecycle=lifecycle, run_id=run if index % 2 else None,
                scope_json={"run_id": str(run)}, metadata_json={"v2_payload": payload},
                input_id=input_id, item_id=uuid.uuid4(), created_at=now,
            )
            target = SimpleNamespace(**vars(row))
            if index % 2:
                target.id = uuid.uuid4()
            block.append(history._EventView(target=target, source=row))
        views.extend(block)
        point = index * 400 + (395 if index % 5 == 0 else 394)
        expected.append(history.SessionSemanticMessage(
            id=f"session-run-cancelled:{block[394].source.id}", role="user",
            content=history._USER_STOP_MARKER, created_at=now,
            sequence_start=point, sequence_end=point, group_id=f"run-stop:{run}",
            source_event_ids=(block[394].target_event_id,),
        ))
        windows.append((index * 400 + 389, point))
    return views, expected, windows


@pytest.mark.parametrize("exclude_current", [False, True])
def test_many_stops_preserve_scan_projection(many_stopped_events, exclude_current):
    from app.services import session_semantic_history as history

    views, expected, windows = many_stopped_events
    current = history._source_scope_run_id(views[0].source) if exclude_current else None
    result = history._cancelled_run_messages(views, current_run_id=current)
    assert result[:2] == (expected[int(exclude_current):], windows[int(exclude_current):])


@pytest.mark.parametrize("target_starts_after_stop", [False, True])
async def test_pre_click_queue_follows_stop_only_when_consumed_by_successor(monkeypatch, target_starts_after_stop):
    from app.services import session_semantic_history as history

    run, successor, input_id, control = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    views = []

    def event(sequence, kind, lifecycle, run_id=None, input_id=None, **payload):
        row = SimpleNamespace(id=uuid.uuid4(), sequence=sequence, item_kind=kind, lifecycle=lifecycle,
            run_id=run_id, input_id=input_id, item_id=uuid.uuid4(), scope_json={}, created_at=now,
            schema_version=2, metadata_json={"v2_payload": payload})
        views.append(history._EventView(target=row, source=row))

    event(1, "human_input", "accepted", input_id=uuid.uuid4(),
        content_parts=[{"type": "text", "text": "Plan the migration"}])
    event(4, "human_input", "accepted", input_id=input_id, intent="queue_next_turn",
        content_parts=[{"type": "text", "text": "List the rollback"}])
    event(10, "run", "cancelling", run_id=run, control_id=control)
    event(11, "run", "cancelled", run_id=run, control_id=control)
    event(20 if target_starts_after_stop else 5, "run", "queued", run_id=successor, input_id=input_id)
    event(21, "human_input", "bound", input_id=input_id, target_run_id=str(successor))
    event(30, "result_commit", "round_committed", run_id=successor)
    views.sort(key=lambda view: view.sequence)

    async def load_views(*_args, **_kwargs):
        return views, {}

    async def rounds(*_args, **_kwargs):
        return [history.SessionSemanticMessage(id="answer", role="assistant", content="Rollback answer",
            created_at=now, sequence_start=30, sequence_end=30, group_id="round", source_event_ids=())], [], 0

    class DB:
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [])

    monkeypatch.setattr(history, "_load_event_views", load_views)
    monkeypatch.setattr(history, "_committed_round_messages", rounds)
    result = await history.load_session_semantic_history(
        DB(), tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(), session_id=uuid.uuid4()
    )
    stop_and_queue = [history._USER_STOP_MARKER, "List the rollback"]
    if not target_starts_after_stop:
        stop_and_queue.reverse()
    assert [message.content for message in result.messages] == [
        "Plan the migration", *stop_and_queue, "Rollback answer",
    ]


@pytest.mark.parametrize("later_outcome,sequence,active,task_type,metadata,reason", [
    (None, 0, True, "web_chat_turn", {}, None),
    ("failed", 12, True, "web_chat_turn", {}, None),
    ("completed", 5, True, "web_chat_turn", {}, None),
    ("completed", 12, False, "web_chat_turn", {}, None),
    ("completed", 12, True, "goal_continuation", {}, None),
    ("completed", 12, True, "web_chat_turn", {"task_notification": True}, None),
    ("completed", 12, True, "web_chat_turn", {"runtime_mailbox_role": "system"}, None),
    ("completed", 12, True, "web_chat_turn", {"trigger_origin": {"trigger_id": "scheduled"}}, None),
    ("completed", 12, True, "team_member", {}, None),
    ("completed", 12, True, "web_chat_turn", {}, "company_approval_required"),
])
async def test_stop_tail_eligibility_expires_only_after_later_normal_completion(
    monkeypatch, later_outcome, sequence, active, task_type, metadata, reason,
):
    from app.models.runtime_task import RuntimeTask
    from app.services import session_semantic_history as history

    now, stopped_run, control_id = datetime.now(timezone.utc), uuid.uuid4(), str(uuid.uuid4())
    views = []

    def event(point, lifecycle, run_id, **payload):
        row = SimpleNamespace(id=uuid.uuid4(), sequence=point, item_kind="run", lifecycle=lifecycle,
            run_id=run_id, input_id=None, item_id=run_id, scope_json={}, created_at=now,
            schema_version=2, metadata_json={"v2_payload": payload})
        views.append(history._EventView(target=row, source=row))

    event(10, "cancelled", stopped_run, control_id=control_id)
    later_run = uuid.uuid4()
    if later_outcome:
        event(sequence, later_outcome, later_run, reason_code=reason)

    async def load_views(*_args, **_kwargs):
        return views, {}

    async def rounds(*_args, **_kwargs):
        return [], [], 0

    class DB:
        async def execute(self, statement):
            rows = [SimpleNamespace(id=later_run, task_type=task_type, metadata_json=metadata,
                machine_credential_id=None)] if statement.column_descriptions[0]["entity"] is RuntimeTask else []
            return SimpleNamespace(all=lambda: rows)

    monkeypatch.setattr(history, "_load_event_views", load_views)
    monkeypatch.setattr(history, "_committed_round_messages", rounds)
    result = await history.load_session_semantic_history(
        DB(), tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(), session_id=uuid.uuid4())
    marker, = result.messages
    assert marker.content == history._USER_STOP_MARKER
    assert marker.id == f"session-run-cancelled:{views[0].source.id}"
    assert marker.stop_notice_active is active


@pytest.mark.parametrize("completed_after_second_stop", [False, True])
async def test_only_completion_after_latest_stop_clears_its_notice(monkeypatch, completed_after_second_stop):
    from app.models.runtime_task import RuntimeTask
    from app.services import session_semantic_history as history

    now = datetime.now(timezone.utc)
    views, queried_runs = [], []
    first_stop, between, second_stop, after = (uuid.uuid4() for _ in range(4))

    def event(sequence, lifecycle, run_id, **payload):
        row = SimpleNamespace(id=uuid.uuid4(), sequence=sequence, item_kind="run", lifecycle=lifecycle,
            run_id=run_id, input_id=None, item_id=run_id, scope_json={}, created_at=now,
            schema_version=2, metadata_json={"v2_payload": payload})
        views.append(history._EventView(target=row, source=row))

    event(10, "cancelled", first_stop, control_id=str(uuid.uuid4()))
    event(15, "completed", between)
    event(20, "cancelled", second_stop, control_id=str(uuid.uuid4()))
    if completed_after_second_stop:
        event(25, "completed", after)

    async def load_views(*_args, **_kwargs):
        return views, {}

    async def rounds(*_args, **_kwargs):
        return [], [], 0

    class DB:
        async def execute(self, statement):
            rows = []
            if statement.column_descriptions[0]["entity"] is RuntimeTask:
                run_ids = set(statement.compile().params["id_1"])
                queried_runs.append(run_ids)
                rows = [SimpleNamespace(id=run_id, task_type="web_chat_turn", metadata_json={}) for run_id in run_ids]
            return SimpleNamespace(all=lambda: rows)

    monkeypatch.setattr(history, "_load_event_views", load_views)
    monkeypatch.setattr(history, "_committed_round_messages", rounds)
    result = await history.load_session_semantic_history(
        DB(), tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(), session_id=uuid.uuid4())
    first, latest = result.messages
    assert first.id == f"session-run-cancelled:{views[0].source.id}"
    assert latest.id == f"session-run-cancelled:{views[2].source.id}"
    assert latest.stop_notice_active is not completed_after_second_stop
    assert queried_runs == ([{after}] if completed_after_second_stop else [])
