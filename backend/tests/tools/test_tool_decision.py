from __future__ import annotations

from uuid import uuid4


def test_tool_decision_is_deterministic_typed_and_serializable() -> None:
    from app.tools.decision import ToolDecisionOutcome, build_tool_decision

    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    first = build_tool_decision(
        decision_id="decision-boundary-1",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor_user_id=user_id,
        tool_name="write_file",
        arguments={"content": "x", "path": "a"},
        policy_snapshot={"permission_mode": "default", "capability": "workspace.write"},
        capability_snapshot={"allowed": True, "capability": "workspace.write"},
        outcome=ToolDecisionOutcome.ALLOW,
        reason_codes=("capability_allow", "preflight_allow"),
        runtime_task_id="task-1",
        session_id="session-1",
    )
    second = build_tool_decision(
        decision_id="decision-boundary-2",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor_user_id=user_id,
        tool_name="write_file",
        arguments={"path": "a", "content": "x"},
        policy_snapshot={"capability": "workspace.write", "permission_mode": "default"},
        capability_snapshot={"capability": "workspace.write", "allowed": True},
        outcome=ToolDecisionOutcome.ALLOW,
        reason_codes=("capability_allow", "preflight_allow"),
        runtime_task_id="task-1",
        session_id="session-1",
    )

    assert first.input_hash == second.input_hash
    assert first.decision_id == "decision-boundary-1"
    assert first.policy_snapshot_hash == second.policy_snapshot_hash
    assert first.capability_snapshot_hash == second.capability_snapshot_hash
    payload = first.to_dict()
    assert payload["schema"] == "hive.tool_decision.v1"
    assert payload["outcome"] == "allow"
    assert payload["tool_name"] == "write_file"
    assert payload["runtime_task_id"] == "task-1"
