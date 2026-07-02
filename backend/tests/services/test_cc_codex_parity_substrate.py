from __future__ import annotations

from uuid import uuid4


def test_command_registry_exposes_index_without_full_schema():
    from app.services.command_registry import CommandRegistry, build_default_command_registry

    registry = build_default_command_registry()
    index = registry.visible_index(surface="agent_prompt")

    names = {entry["name"] for entry in index}
    assert {
        "resume",
        "rewind",
        "compact",
        "team_create",
        "task_create",
        "task_output",
        "goal_start",
        "load_skill",
        "start_workflow",
        "mcp",
    } <= names
    assert "advanced_plan" not in names
    assert "verify_plan" not in names

    assert all("input_schema" not in entry for entry in index)
    assert all("Claude" not in str(entry) and "Anthropic" not in str(entry) for entry in index)

    goal_command = registry.get("goal_start")
    assert goal_command.input_schema["properties"]["objective"]["type"] == "string"
    assert goal_command.execution_mode == "runtime"
    team_command = registry.get("team_create")
    assert "members" not in team_command.input_schema["properties"]
    assert "spawn_subagent" in team_command.description
    assert "team_name + name" in team_command.description
    advanced_plan = registry.get("advanced_plan")
    verify_plan = registry.get("verify_plan")
    assert advanced_plan.execution_mode == "runtime"
    assert advanced_plan.visible_to_model is False
    assert verify_plan.execution_mode == "runtime"
    assert verify_plan.visible_to_model is False

    # Registry must reject ambiguous names across sources unless the later
    # command is an explicit alias to the same canonical handler.
    duplicate = goal_command.model_copy(update={"source": "plugin"})
    fresh = CommandRegistry()
    fresh.register(goal_command)
    try:
        fresh.register(duplicate)
    except ValueError as exc:
        assert "duplicate command" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("duplicate command registration must fail")


def test_rewind_command_registry_describes_active_projection_not_branch():
    from app.services.command_registry import build_default_command_registry

    registry = build_default_command_registry()

    rewind = registry.get("rewind")
    rollback = registry.get("rollback")

    assert "active projection" in rewind.description
    assert "branch" not in rewind.description.lower()
    assert "active projection" in rollback.description
    assert "branch" not in rollback.description.lower()


def test_session_goal_continuation_rules_are_event_driven_and_budgeted():
    from app.services.session_goal_runtime import GoalStatus, SessionGoal, should_continue_goal

    goal = SessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Finish the parity implementation.",
        status=GoalStatus.ACTIVE,
        token_budget=100,
        tokens_used=10,
        max_continuation_turns=3,
        continuation_count=0,
    )

    decision = should_continue_goal(goal, plan_mode=False, pending_user_input=False, active_run_exists=False)
    assert decision.continue_goal is True
    assert decision.trigger == "turn_complete"

    assert (
        should_continue_goal(goal, plan_mode=True, pending_user_input=False, active_run_exists=False).continue_goal
        is False
    )
    assert (
        should_continue_goal(goal, plan_mode=False, pending_user_input=True, active_run_exists=False).continue_goal
        is False
    )
    assert (
        should_continue_goal(goal, plan_mode=False, pending_user_input=False, active_run_exists=True).continue_goal
        is False
    )

    exhausted = goal.model_copy(update={"tokens_used": 100})
    budget_decision = should_continue_goal(
        exhausted,
        plan_mode=False,
        pending_user_input=False,
        active_run_exists=False,
    )
    assert budget_decision.continue_goal is False
    assert budget_decision.next_status == GoalStatus.BUDGET_LIMITED


def test_team_runtime_is_control_index_not_transcript_truth():
    from app.services.team_runtime import TeamMemberSpec, create_team_index, plan_team_close_consolidation

    lead_agent_id = uuid4()
    parent_session_id = uuid4()
    team = create_team_index(
        tenant_id=uuid4(),
        lead_agent_id=lead_agent_id,
        parent_session_id=parent_session_id,
        name="research-sprint",
        members=[
            TeamMemberSpec(name="researcher", role="Collect evidence", model_id="model-a"),
            TeamMemberSpec(name="critic", role="Find gaps", model_id="model-b"),
        ],
    )

    assert team.lead_agent_id == lead_agent_id
    assert team.parent_session_id == parent_session_id
    assert {member.member_name for member in team.members} == {"researcher", "critic"}
    assert all(member.chat_session_id is not None for member in team.members)
    assert all(member.runtime_task_type == "team_member" for member in team.members)
    assert team.transcript_truth == "chat_session_t0"

    close_plan = plan_team_close_consolidation(
        team,
        member_outputs=[
            {
                "member_id": str(team.members[0].id),
                "summary": "Found FreeCode Team semantics.",
                "t0_refs": ["t0://researcher/1"],
                "full_transcript": "must not be copied into lead context",
            }
        ],
    )

    assert close_plan["team_id"] == str(team.id)
    assert close_plan["merge_mode"] == "summary_with_t0_refs"
    assert "full_transcript" not in str(close_plan)
    assert close_plan["member_summaries"][0]["t0_refs"] == ["t0://researcher/1"]


def test_task_command_adapter_keeps_work_ledger_cognitive_only():
    from app.services.task_command_adapter import TaskCommandKind, adapt_task_command

    create = adapt_task_command(
        "TaskCreate",
        {"subject": "Inspect hooks", "owner": "researcher"},
        current_session_id="session-1",
    )
    assert create.kind == TaskCommandKind.WORK_LEDGER_TODO
    assert create.starts_execution is False
    assert create.work_ledger_payload["title"] == "Inspect hooks"

    output = adapt_task_command(
        "TaskOutput",
        {"runtime_task_id": "rt-1"},
        current_session_id="session-1",
    )
    assert output.kind == TaskCommandKind.RUNTIME_TASK_IO
    assert output.runtime_task_id == "rt-1"

    blocked = adapt_task_command("TaskStop", {}, current_session_id="session-1")
    assert blocked.kind == TaskCommandKind.INVALID
    assert "runtime_task_id" in blocked.error


def test_task_command_adapter_keeps_cc_task_surface_unified_with_internal_flavors():
    from app.services.task_command_adapter import TaskCommandKind, adapt_task_command

    delegated = adapt_task_command(
        "TaskCreate",
        {"kind": "delegation", "agent_name": "Researcher", "message": "Collect source evidence."},
        current_session_id="session-1",
    )
    assert delegated.kind == TaskCommandKind.DELEGATION_TASK
    assert delegated.starts_execution is True
    assert delegated.delegate_action == "create"
    assert delegated.delegate_payload == {
        "agent_name": "Researcher",
        "message": "Collect source evidence.",
    }

    shorthand = adapt_task_command(
        "TaskCreate",
        {"agent_name": "Critic", "prompt": "Review the plan."},
        current_session_id="session-1",
    )
    assert shorthand.kind == TaskCommandKind.DELEGATION_TASK
    assert shorthand.delegate_payload == {
        "agent_name": "Critic",
        "message": "Review the plan.",
    }

    delegated_get = adapt_task_command(
        "TaskGet",
        {"kind": "delegation", "task_id": "async-1"},
        current_session_id="session-1",
    )
    assert delegated_get.kind == TaskCommandKind.DELEGATION_TASK
    assert delegated_get.starts_execution is False
    assert delegated_get.delegate_action == "get"
    assert delegated_get.delegate_payload == {"task_id": "async-1"}


def test_remaining_freecode_hook_events_exist():
    from app.runtime.hooks import HookEvent

    for event_name in (
        "PERMISSION_REQUEST",
        "TASK_CREATED",
        "TASK_COMPLETED",
        "ELICITATION",
        "CONFIG_CHANGE",
        "INSTRUCTIONS_LOADED",
        "WORKSPACE_CONTEXT_CHANGED",
        "ARTIFACT_CHANGED",
        "TEAM_CREATED",
        "TEAM_CLOSED",
        "TEAMMATE_IDLE",
    ):
        assert hasattr(HookEvent, event_name)
