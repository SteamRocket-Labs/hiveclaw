"""Unit tests for the agent-authored Plan Mode planner prompt contract."""

from __future__ import annotations

from uuid import uuid4


def test_planner_system_prompt_defines_analysis_workflow_and_quality_bar() -> None:
    from app.services.agent_plan_planner import PLANNER_PROMPT_VERSION, _planner_system_prompt

    prompt = _planner_system_prompt()

    assert PLANNER_PROMPT_VERSION == "agent_plan_v4"
    assert "Claude Code-style" in prompt
    assert "Plan Mode is active" in prompt
    assert "Initial understanding" in prompt
    assert "Explore read-only context" in prompt
    assert "Design" in prompt
    assert "Review" in prompt
    assert "Final plan" in prompt
    assert "Approval boundary" in prompt
    assert "Concrete planning workflow" in prompt
    assert "inspect current state" in prompt
    assert "Do not invent file paths" in prompt
    assert "do not expose internal tool invocation scripts" in prompt
    assert "runtime_artifacts" in prompt
    assert "Clarification policy" in prompt
    assert "Quality bar" in prompt
    assert "required_capabilities is machine contract metadata" in prompt
    assert "Never emit raw tool names" in prompt
    assert "Do not emit placeholder external_side_effects" in prompt
    assert "Agent Work Ledger" in prompt
    assert "current phase" in prompt
    assert "assumptions" in prompt
    assert "open_questions" in prompt
    assert "verification" in prompt
    assert "plan_json" in prompt
    assert "plan_markdown" in prompt


def test_planner_user_prompt_marks_seed_plan_as_context_not_final_answer() -> None:
    from app.services.agent_plan_planner import AgentPlanPlannerInput, _build_planner_user_prompt

    planning_input = AgentPlanPlannerInput(
        plan_id=uuid4(),
        agent_id=uuid4(),
        requested_by_user_id=uuid4(),
        tenant_id=uuid4(),
        session_id="sess-1",
        runtime_task_id=uuid4(),
        source="tool_runtime",
        intent_type="autonomous_wake",
        original_request="每天 9 点整理 AI 新闻并发给我",
        seed_plan={
            "objective": "Seeded objective from intercepted tool args",
            "steps": [{"order": 1, "description": "seed step"}],
        },
        intercepted_tool={
            "tool_name": "set_trigger",
            "action_kind": "create_enabled_trigger",
            "arguments": {"schedule": "0 9 * * *"},
        },
        metadata_json={"intercept_signature": "abc123"},
    )

    prompt = _build_planner_user_prompt(planning_input)

    assert "Seed plan is context, not the final answer" in prompt
    assert "Claude Code-style Plan Mode contract" in prompt
    assert "preserve the machine handoff" in prompt
    assert "Do not copy intercepted tool arguments as the final plan" in prompt
    assert "load_skill" in prompt
    assert "deep_research_start" in prompt
    assert "runtime_artifacts/" in prompt
    assert "evidence_summary" in prompt
    assert '"original_request": "每天 9 点整理 AI 新闻并发给我"' in prompt
    assert '"tool_name": "set_trigger"' in prompt
