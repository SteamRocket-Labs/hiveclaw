from __future__ import annotations


def test_system_prompt_includes_vendor_neutral_behavior_contract() -> None:
    from app.runtime.prompt_sections.executing_actions import build_executing_actions_section
    from app.runtime.prompt_sections.system import build_system_section
    from app.runtime.prompts.behavior import BEHAVIOR_CONTRACT

    rendered = "\n".join([build_system_section(), build_executing_actions_section()])

    for phrase in [
        "No hidden assumptions",
        "When you have enough information to act, act",
        "Do the simplest thing that works",
        "Surgical changes only",
        "Define success criteria before work begins",
        "Progress claims require evidence from this run",
        "Pause only for destructive or irreversible actions",
        "Do not ask the model to reveal or reproduce hidden reasoning",
    ]:
        assert phrase in BEHAVIOR_CONTRACT
        assert phrase in rendered

    assert "Claude Fable" not in rendered
    assert "Karpathy" not in rendered


def test_delegation_tool_descriptions_require_structured_briefs() -> None:
    from app.runtime.prompts.delegation import DELEGATION_BRIEF_CONTRACT
    from app.tools.handlers.communication import delegate_to_agent, send_message_to_agent

    assert "Goal / Context / Known facts / Constraints / Evidence needed / Output / Stop condition" in (
        DELEGATION_BRIEF_CONTRACT
    )
    assert "Do not ask the worker to infer missing scope silently" in DELEGATION_BRIEF_CONTRACT

    sync_description = send_message_to_agent.meta.description
    async_description = delegate_to_agent.meta.description

    assert "short consults" in sync_description
    assert "Do NOT use this for long-running delegated work" in sync_description
    assert "Goal / Context / Known facts / Constraints / Evidence needed / Output / Stop condition" in (
        async_description
    )
    assert "session_id/child_session_id" in async_description
    assert "use `check_async_task` only as a fallback status inspection" in async_description
    assert "Do not ask the worker to infer missing scope silently" in async_description


def test_command_parity_tools_explain_command_layer_semantics() -> None:
    from app.tools.handlers.command_parity import advanced_plan, goal_start, task_create, team_create, verify_plan

    assert "cognitive task only" in task_create.meta.description
    assert "does NOT start execution" in task_create.meta.description
    assert "current session goal" in goal_start.meta.description
    assert "resume/continuation" in goal_start.meta.description
    assert "does not complete the goal automatically" in goal_start.meta.description
    assert "Team container" in team_create.meta.description
    assert "does not spawn teammates" in team_create.meta.description
    assert "spawn_subagent" in team_create.meta.description
    assert "team_name" in team_create.meta.description
    assert "planning-only" in advanced_plan.meta.description
    assert "does not execute" in advanced_plan.meta.description
    assert "evidence check" in verify_plan.meta.description
    assert "does not execute the plan" in verify_plan.meta.description


def test_mcp_descriptions_have_routing_contracts() -> None:
    from app.tools.handlers.mcp import (
        call_mcp_tool,
        import_mcp_server,
        inspect_mcp_tool,
        list_mcp_tools,
        mcp_list_resources,
        mcp_read_resource,
    )

    assert "imported MCP tools" in list_mcp_tools.meta.description
    assert "tool schemas, not protocol resources" in list_mcp_tools.meta.description
    assert "parameters schema before calling" in inspect_mcp_tool.meta.description
    assert "explicit platform-extension workflow" in import_mcp_server.meta.description
    assert "not a normal task-execution step" in import_mcp_server.meta.description
    assert "call only after list_mcp_tools/inspect_mcp_tool" in call_mcp_tool.meta.description
    assert "resources/list" in mcp_list_resources.meta.description
    assert "distinct from tools" in mcp_list_resources.meta.description
    assert "resources/read" in mcp_read_resource.meta.description
    assert "Large binary blobs spill to workspace artifacts" in mcp_read_resource.meta.description


def test_compaction_prompt_preserves_long_run_and_behavior_state() -> None:
    from app.runtime.prompts.compaction import COMPACTION_LONG_RUN_STATE_CONTRACT
    from app.services.conversation_summarizer import _SUMMARIZE_SYSTEM_PROMPT

    normalized = " ".join(_SUMMARIZE_SYSTEM_PROMPT.split())
    for phrase in [
        "progress claims require evidence",
        "do not convert tactical state into durable memory",
        "preserve assumptions, tradeoffs, and user-approved scope",
        "do not ask the next turn to reveal hidden reasoning",
        "resume from the latest explicit request",
    ]:
        assert phrase in COMPACTION_LONG_RUN_STATE_CONTRACT
        assert phrase in normalized


def test_runtime_reminders_and_loop_guard_use_canonical_prompt_fragments() -> None:
    from app.kernel import loop_guard, reminder_scheduler
    from app.runtime.prompts.runtime_reminders import (
        LOOP_GUARD_WARN_GUIDANCE,
        PLAN_MODE_REMINDER_FULL,
        PLAN_MODE_REMINDER_SPARSE,
        PROGRESS_REPLAN_POLICY,
        WORK_LEDGER_REMINDER,
        build_round_pressure_warning,
    )

    assert reminder_scheduler._PLAN_MODE_REMINDER_FULL == PLAN_MODE_REMINDER_FULL
    assert reminder_scheduler._PLAN_MODE_REMINDER_SPARSE == PLAN_MODE_REMINDER_SPARSE
    assert reminder_scheduler._WORK_LEDGER_REMINDER == WORK_LEDGER_REMINDER
    assert reminder_scheduler._PROGRESS_REPLAN_POLICY == PROGRESS_REPLAN_POLICY
    assert reminder_scheduler._build_round_pressure_warning(
        round_i=2,
        max_rounds=3,
        total_tool_calls=4,
        failed_tool_calls=1,
        context_tokens=12345,
        final=True,
    ) == build_round_pressure_warning(
        round_i=2,
        max_rounds=3,
        total_tool_calls=4,
        failed_tool_calls=1,
        context_tokens=12345,
        final=True,
    )
    assert loop_guard._WARN_GUIDANCE == LOOP_GUARD_WARN_GUIDANCE
