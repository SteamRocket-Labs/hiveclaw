from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_worker_binds_initial_transcript_sequence_into_durable_runtime_metadata(monkeypatch) -> None:
    import app.services.web_chat_runtime as runtime

    event_id = uuid4()
    user_event = SimpleNamespace(
        event_id=event_id,
        sequence=88,
        transcript_event=SimpleNamespace(metadata_json={}),
    )

    async def append_session_event(**_kwargs):
        return user_event

    async def mark_answered(**_kwargs):
        return None

    monkeypatch.setattr(runtime, "append_session_event", append_session_event)
    monkeypatch.setattr(runtime, "mark_latest_pending_clarification_answered", mark_answered)

    runtime_task = SimpleNamespace(
        id=uuid4(),
        parent_session_id=str(uuid4()),
        prompt="hello",
        metadata_json={
            "initial_user_message_t0_materialized": False,
            "initial_user_message": {
                "message_id": str(uuid4()),
                "content": "hello",
                "source": "web",
                "metadata": {},
            },
        },
    )

    await runtime._materialize_initial_user_turn_for_worker(
        db=SimpleNamespace(),
        runtime_task=runtime_task,
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        user=SimpleNamespace(id=uuid4(), external_principal_id=None),
        session=None,
    )

    assert runtime_task.metadata_json["initial_user_message_t0_event_id"] == str(event_id)
    assert runtime_task.metadata_json["initial_user_message_t0_sequence"] == 88


@pytest.mark.asyncio
async def test_web_runtime_session_carries_root_task_and_base_transcript_authority() -> None:
    from app.runtime.session import SessionContext
    from app.services.web_chat_run_orchestrator import _configure_runtime_session

    context = SessionContext(session_id="session-a")

    class Broker:
        async def get_or_create_runtime_session(self, _agent_id, _session_id):
            return context

    context_port = SimpleNamespace(
        broker=Broker(),
        sync_permission_metadata=lambda *_args: None,
        channel_delivery_suffix=lambda *_args: "",
        clear_stale_plan_mode=lambda *_args, **_kwargs: None,
    )
    run_uuid = uuid4()
    root_runtime_task_id = uuid4()
    root_session_id = uuid4()
    state = SimpleNamespace(
        ports=SimpleNamespace(context=context_port),
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        session_id="session-a",
        metadata={
            "source": "web",
            "initial_user_message_t0_sequence": 91,
        },
        run_uuid=run_uuid,
        runtime_task=SimpleNamespace(
            root_runtime_task_id=root_runtime_task_id,
            root_session_id=root_session_id,
            budget_run_id=None,
            trace_id=f"web-chat:{run_uuid.hex}",
        ),
        summary_turn_mode=False,
        history_messages=[],
        runtime_session_context=None,
        channel_delivery_suffix="",
    )

    await _configure_runtime_session(state)

    assert context.metadata["runtime_task_id"] == run_uuid.hex
    assert context.metadata["root_runtime_task_id"] == str(root_runtime_task_id)
    assert context.metadata["root_session_id"] == str(root_session_id)
    assert context.metadata["base_transcript_sequence"] == 91
