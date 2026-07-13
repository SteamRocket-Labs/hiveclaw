from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_broker_clears_resolved_recovery_state_without_reintroducing_carrier() -> None:
    from app.services.web_chat_broker import WebChatBroker

    broker = WebChatBroker()
    context = await broker.get_or_create_runtime_session("agent-1", "session-1")
    context.metadata.update(
        {
            "runtime_task_id": "run-carrier",
            "recovery_reconciliation_blocked": True,
            "pending_tool_frame": {"tool_call_id": "call-1", "tool_name": "send_email"},
            "pending_tool_frames": [{"tool_call_id": "call-1", "tool_name": "send_email"}],
            "recovered_pending_tool_frames": [{"tool_call_id": "call-1", "tool_name": "send_email"}],
            "prior_run_recovery_reconciliations": [
                {"source_runtime_task_id": "run-old", "status": "needs_reconciliation"},
                {"source_runtime_task_id": "run-other", "status": "needs_reconciliation"},
            ],
        }
    )

    cleared = await broker.resolve_runtime_recovery_state(
        "agent-1",
        "session-1",
        runtime_task_ids={"run-old", "run-carrier"},
    )

    assert cleared is True
    assert context.metadata["prior_run_recovery_reconciliations"] == [
        {"source_runtime_task_id": "run-other", "status": "needs_reconciliation"}
    ]
    assert context.metadata["recovery_reconciliation_blocked"] is True
    assert "pending_tool_frame" not in context.metadata
    assert "pending_tool_frames" not in context.metadata
    assert "recovered_pending_tool_frames" not in context.metadata

    await broker.resolve_runtime_recovery_state(
        "agent-1",
        "session-1",
        runtime_task_ids={"run-other"},
    )
    assert "prior_run_recovery_reconciliations" not in context.metadata
    assert "recovery_reconciliation_blocked" not in context.metadata
