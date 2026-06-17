from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_non_noop_heartbeat_reflection_emits_learning_brain_event(monkeypatch):
    from app.memory import metrics
    from app.runtime.hooks import HookEvent
    from app.services.heartbeat import _route_heartbeat_reflection_learning

    metrics.reset_all()
    emitted: list[dict] = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append({"event": event, **kwargs})

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    runtime_task_id = uuid.uuid4()
    session_id = uuid.uuid4()
    chat_message_id = uuid.uuid4()
    late_lesson = "NEVER_LOSE_THIS_HEARTBEAT_LESSON: pytest auth regression needs token refresh first."
    reply = ("A" * 200) + "\n" + late_lesson + "\n[OUTCOME:action_taken] [SCORE:7]"

    result = await _route_heartbeat_reflection_learning(
        agent_id=agent_id,
        tenant_id=tenant_id,
        agent_name="Web3研究员",
        session_id=session_id,
        runtime_task_id=str(runtime_task_id),
        assistant_message_id=str(chat_message_id),
        runtime_messages=[{"role": "user", "content": "heartbeat instruction"}],
        reply=reply,
        outcome_type="action_taken",
        outcome_lane="agent_action",
        score=7,
        await_hook=True,
    )

    assert result["status"] == "emitted"
    assert len(emitted) == 1
    assert emitted[0]["event"] == HookEvent.RESPONSE_COMPLETE
    assert emitted[0]["source"] == "heartbeat_reflection"
    assert late_lesson in str(emitted[0]["messages"])
    assert emitted[0]["metadata"]["heartbeat_outcome"] == "action_taken"
    assert emitted[0]["metadata"]["source_refs"] == [
        f"heartbeat_session:{session_id}",
        f"runtime_task:{runtime_task_id}",
        f"chat_message:{chat_message_id}",
    ]
    assert metrics.snapshot()["heartbeat_reflection_total"]["processed"] == 1


@pytest.mark.asyncio
async def test_noop_heartbeat_reflection_does_not_emit_learning_event(monkeypatch):
    from app.memory import metrics
    from app.services.heartbeat import _route_heartbeat_reflection_learning

    metrics.reset_all()
    emitted: list[dict] = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append({"event": event, **kwargs})

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    result = await _route_heartbeat_reflection_learning(
        agent_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_name="Agent",
        session_id=uuid.uuid4(),
        runtime_task_id="runtime-1",
        assistant_message_id="message-1",
        runtime_messages=[],
        reply="HEARTBEAT_OK [OUTCOME:noop] [SCORE:0]",
        outcome_type="noop",
        outcome_lane="idle",
        score=0,
        await_hook=True,
    )

    assert result == {"status": "skipped", "reason": "low_signal_noop"}
    assert emitted == []
    assert metrics.snapshot()["heartbeat_reflection_total"]["skipped_low_signal"] == 1


def test_heartbeat_reflection_messages_preserve_full_reply_not_lineage_summary():
    from app.services.heartbeat import _build_heartbeat_reflection_messages

    late_lesson = "LATE_HEARTBEAT_LESSON: retrying direct writes to evolution is a dead end."
    reply = ("x" * 200) + late_lesson + "\n[OUTCOME:failure] [SCORE:2]"

    messages = _build_heartbeat_reflection_messages(
        runtime_messages=[{"role": "user", "content": "heartbeat"}],
        reply=reply,
        metadata={"lineage_summary": "Summary: first 80 chars only"},
    )

    serialized = str(messages)
    assert late_lesson in serialized
    assert "lineage_summary" not in serialized
