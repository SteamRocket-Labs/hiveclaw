from __future__ import annotations


def test_agent_cycle_decision_entry_has_required_matrix_fields() -> None:
    from app.runtime.decision_ledger import build_agent_cycle_decision_entry

    entry = build_agent_cycle_decision_entry(
        subsystem="compaction",
        trigger="context_threshold",
        judge="SessionContextController",
        decision="compact",
        outcome="completed",
        next_action="continue",
        model_interaction="rewrite_next_context",
        user_visible=False,
        permission_result="unchanged",
        budget_result="under_limit",
    )

    assert entry["schema"] == "hive.ccplus.agent_cycle_decision.v1"
    for field in (
        "subsystem",
        "trigger",
        "judge",
        "decision",
        "outcome",
        "next_action",
        "model_interaction",
        "user_visible",
        "permission_result",
        "budget_result",
    ):
        assert field in entry


def test_subsystem_decision_builders_embed_agent_cycle_decision_entry() -> None:
    from uuid import uuid4

    from app.runtime.dynamic_workflow import build_workflow_decision_entry
    from app.runtime.schedule_decision_ledger import build_schedule_decision_entry
    from app.runtime.subagent_decision_entry import build_subagent_decision_entry
    from app.services.agent_team_runtime_service import build_agent_team_decision_entry
    from app.services.session_goal_runtime import GoalContinuationDecision, GoalStatus, SessionGoal, build_goal_decision_entry

    goal = SessionGoal(id=uuid4(), agent_id=uuid4(), chat_session_id=uuid4(), objective="Ship")
    goal_entry = build_goal_decision_entry(goal, GoalContinuationDecision(continue_goal=True, reason="active"))
    schedule_entry = build_schedule_decision_entry(command_origin="tool:set_trigger", natural_vs_structured="structured")
    workflow_entry = build_workflow_decision_entry(
        dynamic_workflow={"proposal_id": "p1", "candidate_id": "c1", "definition_hash": "h1"},
        run_id="run-1",
        outcome={"status": "completed"},
    )
    subagent_entry = build_subagent_decision_entry(run_id="run-2", status="completed")
    team_entry = build_agent_team_decision_entry(
        {"id": "team-1", "status": "active", "metadata_json": {}},
        [{"id": "member-1", "member_name": "critic", "status": "completed", "metadata_json": {}}],
    )

    entries = [
        goal_entry.model_dump(by_alias=True),
        schedule_entry,
        workflow_entry,
        subagent_entry,
        team_entry,
    ]
    assert goal_entry.status_transition == {"from": GoalStatus.ACTIVE.value, "to": GoalStatus.ACTIVE.value}
    for entry in entries:
        agent_cycle = entry["agent_cycle_decision_entry"]
        assert agent_cycle["schema"] == "hive.ccplus.agent_cycle_decision.v1"
        assert agent_cycle["trigger"]
        assert agent_cycle["judge"]
        assert agent_cycle["decision"]
        assert agent_cycle["outcome"]
        assert agent_cycle["next_action"]
        assert "permission_result" in agent_cycle
        assert "budget_result" in agent_cycle


def test_agent_cycle_decision_matrix_lists_all_runtime_subsystems() -> None:
    from app.runtime.decision_ledger import build_agent_cycle_decision_matrix

    matrix = build_agent_cycle_decision_matrix()

    assert matrix["schema"] == "hive.ccplus.agent_cycle_decision_matrix.v1"
    assert matrix["required_fields"] == [
        "trigger",
        "judge",
        "decision",
        "outcome",
        "next_action",
        "model_interaction",
        "user_visible",
        "permission_result",
        "budget_result",
    ]
    subsystems = {item["subsystem"] for item in matrix["subsystems"]}
    assert {
        "compaction",
        "loop_guard",
        "goal",
        "schedule",
        "trigger",
        "workflow",
        "agent_team",
        "subagent",
    } <= subsystems
