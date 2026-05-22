from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.coordination import CoordinationRuntime


def test_lease_serializes_same_task() -> None:
    runtime = CoordinationRuntime(now=lambda: datetime(2026, 5, 22, tzinfo=UTC))

    first = runtime.acquire_lease(task_key="task-1", agent_id="agent-a", ttl_seconds=60)
    second = runtime.acquire_lease(task_key="task-1", agent_id="agent-b", ttl_seconds=60)

    assert first.acquired
    assert not second.acquired
    assert second.existing_lease_id == first.lease.id


def test_signal_threads_messages_between_agents() -> None:
    runtime = CoordinationRuntime(now=lambda: datetime(2026, 5, 22, tzinfo=UTC))

    signal = runtime.send_signal(
        from_agent_id="agent-a",
        to_agent_id="agent-b",
        content="Need research output schema.",
        signal_type="request",
        thread_id="thread-1",
    )

    assert runtime.read_signals("agent-b", thread_id="thread-1") == [signal]


def test_checkpoint_deadline_escalates_to_company_admin() -> None:
    current = datetime(2026, 5, 22, tzinfo=UTC)
    runtime = CoordinationRuntime(now=lambda: current)
    checkpoint = runtime.create_checkpoint(
        action="external_reply",
        approver_id="owner",
        escalation_chain=["company_admin"],
        deadline_at=current - timedelta(seconds=1),
    )

    escalated = runtime.escalate_expired_checkpoints()

    assert escalated == [checkpoint.id]
    assert runtime.get_checkpoint(checkpoint.id).current_approver_id == "company_admin"

