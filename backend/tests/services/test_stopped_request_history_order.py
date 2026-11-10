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

    event(1, "human_input", "accepted", content_parts=[{"type": "text", "text": "Stopped request"}])
    event(2, "result_commit", "round_committed")
    event(3, "run", "cancelling", control_id=control)
    for index in (4, 5):
        event(
            index,
            "human_input",
            "accepted",
            intent=intent,
            content_parts=[{"type": "text", "text": f"Later input {index}"}],
        )
    if late_commit:
        event(6, "result_commit", "round_committed")
    # Another control is not the stop point for this settled cancellation.
    event(7, "run", "cancelling", control_id=str(uuid.uuid4()))
    event(8, "run", "cancelled", control_id=control)

    async def load_views(*_args, **_kwargs):
        return views, {}

    async def rounds(*_args, **_kwargs):
        sequence = 6 if late_commit else 2
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
    assert stop.sequence_start == (6 if late_commit else 3)
    assert stop.id == f"session-run-cancelled:{views[-1].source.id}"
