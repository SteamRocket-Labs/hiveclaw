from __future__ import annotations

from uuid import uuid4


def test_session_context_metadata_records_session_key():
    from app.runtime.session_key import build_session_key, ensure_session_key
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    ctx = SessionContext(session_id=None, source="trigger", channel="default")
    key = build_session_key(
        agent_id=agent_id,
        source="trigger",
        channel="default",
    )
    ensure_session_key(ctx, key)

    expected = f"trigger:default:agent:{agent_id.hex}"
    assert key.stable_id == expected
    assert ctx.session_id == expected
    assert ctx.metadata["session_key"]["stable_id"] == expected
    assert "objective_id" not in ctx.metadata["session_key"]


def test_runtime_task_session_key_uses_runtime_trace_when_no_objective():
    from app.runtime.session_key import build_session_key

    agent_id = uuid4()
    runtime_task_id = uuid4()
    key = build_session_key(
        agent_id=agent_id,
        source="task",
        channel="delegation",
        runtime_task_id=runtime_task_id.hex,
    )

    assert key.stable_id == f"task:delegation:runtime:{runtime_task_id.hex}"
    assert key.runtime_task_id == runtime_task_id.hex
