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


def test_lease_release_requires_matching_owner_and_lease_identity() -> None:
    runtime = CoordinationRuntime(now=lambda: datetime(2026, 5, 22, tzinfo=UTC))
    acquired = runtime.acquire_lease(task_key="task-1", agent_id="runtime-task:1", ttl_seconds=60)

    assert acquired.lease is not None
    assert runtime.release_lease(
        task_key="task-1",
        agent_id="runtime-task:other",
        lease_id=acquired.lease.id,
    ) is False
    assert runtime.release_lease(
        task_key="task-1",
        agent_id="runtime-task:1",
        lease_id="stale-lease-id",
    ) is False
    assert runtime.release_lease(
        task_key="task-1",
        agent_id="runtime-task:1",
        lease_id=acquired.lease.id,
    ) is True
    assert runtime.acquire_lease(task_key="task-1", agent_id="runtime-task:2", ttl_seconds=60).acquired is True


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


def test_sentinel_can_emit_signal_for_full_authority_followup() -> None:
    current = datetime(2026, 5, 22, tzinfo=UTC)
    runtime = CoordinationRuntime(now=lambda: current)
    sentinel = runtime.register_sentinel(
        sentinel_id="sentinel-open-loop",
        owner_agent_id="agent-owner",
        target_agent_id="agent-worker",
        condition="open_loop_due",
        runtime_path="signal",
    )

    emission = runtime.fire_sentinel(
        sentinel.id,
        content="Prepare the follow-up draft for the owner.",
        thread_id="loop-42",
    )

    assert emission.kind == "signal"
    assert emission.signal is not None
    assert emission.checkpoint is None
    assert runtime.read_signals("agent-worker", thread_id="loop-42") == [emission.signal]


def test_sentinel_can_emit_checkpoint_for_confirm_first_action() -> None:
    current = datetime(2026, 5, 22, tzinfo=UTC)
    runtime = CoordinationRuntime(now=lambda: current)
    sentinel = runtime.register_sentinel(
        sentinel_id="sentinel-external-send",
        owner_agent_id="agent-owner",
        target_agent_id="agent-worker",
        condition="external_send_ready",
        runtime_path="checkpoint",
        checkpoint_approver_id="owner-user",
        escalation_chain=["company-admin"],
        deadline_seconds=900,
    )

    emission = runtime.fire_sentinel(
        sentinel.id,
        content="Approve sending the prepared vendor reply.",
        metadata={"tool_name": "send_feishu_message"},
    )

    assert emission.kind == "checkpoint"
    assert emission.signal is None
    assert emission.checkpoint is not None
    assert emission.checkpoint.action == "external_send_ready"
    assert emission.checkpoint.current_approver_id == "owner-user"
    assert emission.checkpoint.escalation_chain == ["company-admin"]
    assert emission.checkpoint.metadata["tool_name"] == "send_feishu_message"
