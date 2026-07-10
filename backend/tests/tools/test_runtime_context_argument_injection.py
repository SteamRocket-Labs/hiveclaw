from __future__ import annotations

import uuid
from pathlib import Path

from app.tools.runtime import ToolExecutionContext
from app.tools.service import _inject_runtime_context_arguments


def test_a2a_runtime_context_injects_stable_turn_and_task_idempotency_anchors() -> None:
    runtime_task_id = uuid.uuid4()
    context = ToolExecutionContext(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=str(uuid.uuid4()),
        workspace=Path("/tmp/hive-runtime-context-test"),
        session_id="session-1",
        turn_id="turn-7",
        runtime_task_id=str(runtime_task_id),
    )

    enriched = _inject_runtime_context_arguments(
        "delegate_to_agent",
        {"agent_name": "Local Mac", "message": "Inspect the repository."},
        context,
    )

    assert enriched["parent_session_id"] == "session-1"
    assert enriched["_turn_id"] == "turn-7"
    assert enriched["_runtime_task_id"] == str(runtime_task_id)
