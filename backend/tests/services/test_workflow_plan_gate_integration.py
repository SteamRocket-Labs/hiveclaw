"""Workflow confirmation inspection + real-spawn leaf tests.

Covers the launch half of P4:
* workflow preview can surface confirmation notes without assigning low/high
  risk levels or entering Plan Mode;
* the production leaf executor binds agent_step to the REAL axis-1
  ``spawn_subagent`` entry (asserted via an injected double) and inherits
  tenant / budget / governance context from the spawn ctx.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.agents.subagent import SubagentBudget, SubagentHandle, SubagentResult, SubagentSpawnContext
from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_engine import LeafRequest
from app.services.workflow_launch import (
    build_subagent_leaf_executor,
    inspect_workflow_confirmation_needs,
)


def _definition(**overrides) -> dict:
    data = {
        "name": "risk-probe",
        "args_schema": {"targets": {"type": "array", "required": False}},
        "steps": [
            {
                "id": "scan",
                "type": "agent_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "task": "Scan everything",
            },
        ],
    }
    data.update(overrides)
    return data


def _ctx(**overrides) -> SubagentSpawnContext:
    ctx = SubagentSpawnContext(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
        tenant_id=uuid.uuid4(),
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


# ── PlanModeGate decoupling ───────────────────────────────────────


def test_start_workflow_is_not_a_plan_action_kind():
    from app.services.plan_mode_core import ACTION_KINDS

    assert "start_workflow" not in ACTION_KINDS


# ── confirmation inspection ───────────────────────────────────────


def test_read_only_small_workflow_needs_no_extra_confirmation_note():
    compiled = compile_workflow(_definition())
    assessment = inspect_workflow_confirmation_needs(compiled, args={})
    assert assessment.requires_confirmation is False
    assert assessment.reasons == []


def test_external_effects_are_confirmation_notes():
    data = _definition()
    data["steps"].insert(0, {"id": "gate", "type": "gate_step", "reason": "external send"})
    data["steps"].append(
        {
            "id": "send",
            "type": "agent_step",
            "leaf": {"name": "sender", "type": "worker"},
            "task": "Send the report",
            "effects": "external",
        }
    )
    compiled = compile_workflow(data)
    assessment = inspect_workflow_confirmation_needs(compiled, args={})
    assert assessment.requires_confirmation is True
    assert any("external" in reason for reason in assessment.reasons)


def test_large_budget_is_confirmation_note():
    compiled = compile_workflow(_definition(default_budget={"max_total_tokens": 1_500_000}))
    assessment = inspect_workflow_confirmation_needs(compiled, args={})
    assert assessment.requires_confirmation is True
    assert any("budget" in reason for reason in assessment.reasons)


def test_wide_fanout_is_confirmation_note():
    data = _definition()
    data["steps"].append(
        {
            "id": "fan",
            "type": "fanout_step",
            "leaf": {"name": "scanner", "type": "explorer"},
            "items_from": "args.targets",
            "per_item_task": "Scan {{item}}",
        }
    )
    compiled = compile_workflow(data)
    assessment = inspect_workflow_confirmation_needs(compiled, args={"targets": [f"t{i}" for i in range(12)]})
    assert assessment.requires_confirmation is True
    assert any("fanout" in reason for reason in assessment.reasons)


def test_long_wait_is_confirmation_note():
    data = _definition()
    data["steps"].append({"id": "wait", "type": "wait_until_step", "delay_seconds": 24 * 3600})
    compiled = compile_workflow(data)
    assessment = inspect_workflow_confirmation_needs(compiled, args={})
    assert assessment.requires_confirmation is True
    assert any("wait" in reason or "wall" in reason for reason in assessment.reasons)


def test_absolute_wait_until_is_confirmation_note_when_far_in_future():
    data = _definition()
    data["steps"].append(
        {
            "id": "wait",
            "type": "wait_until_step",
            "until": (datetime.now(UTC) + timedelta(hours=12)).isoformat(),
        }
    )
    compiled = compile_workflow(data)
    assessment = inspect_workflow_confirmation_needs(compiled, args={})
    assert assessment.requires_confirmation is True
    assert any("wait" in reason or "wall" in reason for reason in assessment.reasons)


def test_wait_until_args_reference_is_confirmation_note_when_far_in_future():
    data = _definition()
    data["args_schema"]["resume_at"] = {"type": "string", "required": True}
    data["steps"].append({"id": "wait", "type": "wait_until_step", "until": "args.resume_at"})
    compiled = compile_workflow(data)
    assessment = inspect_workflow_confirmation_needs(
        compiled,
        args={"resume_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat()},
    )
    assert assessment.requires_confirmation is True
    assert any("wait" in reason or "wall" in reason for reason in assessment.reasons)


# ── real-spawn leaf executor ──────────────────────────────────────


async def test_leaf_executor_calls_real_spawn_subagent_entry():
    """The double stands in for axis-1 spawn_subagent; the assertion is the
    CONTRACT: spec/task/budget/ctx all flow through the real entry, which is
    what inherits governance + tenant + SubagentBudget (§6.3)."""
    ctx = _ctx()
    seen: dict = {}

    async def spawn_double(spawn_ctx, spec, task, *, fork="none", budget=None, context_brief=None, **kwargs):
        seen["ctx"] = spawn_ctx
        seen["spec"] = spec
        seen["task"] = task
        seen["budget"] = budget
        return SubagentHandle(
            name=spec.name,
            trace_id="t",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="done", tokens_used=5),
        )

    executor = build_subagent_leaf_executor(ctx, spawn=spawn_double)
    request = LeafRequest(
        run_id=str(uuid.uuid4()),
        step_id="scan",
        leaf=compile_workflow(_definition()).definition.steps[0].leaf,
        task="Scan everything",
        tenant_id=str(ctx.tenant_id),
    )
    outcome = await executor(request)

    assert outcome.ok is True
    assert outcome.output["text"] == "done"
    assert outcome.tokens_used == 5
    assert seen["ctx"] is ctx, "spawn ctx (tenant/governance/token) must pass through unchanged"
    assert seen["spec"].name == "scanner"
    assert seen["spec"].type == "explorer"
    assert seen["task"] == "Scan everything"
    assert isinstance(seen["budget"], SubagentBudget)


async def test_leaf_executor_maps_failure_to_not_ok():
    ctx = _ctx()

    async def failing_spawn(spawn_ctx, spec, task, **kwargs):
        return SubagentHandle(
            name=spec.name,
            trace_id="t",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="failed", error="worker exploded"),
        )

    executor = build_subagent_leaf_executor(ctx, spawn=failing_spawn)
    request = LeafRequest(
        run_id=str(uuid.uuid4()),
        step_id="scan",
        leaf=compile_workflow(_definition()).definition.steps[0].leaf,
        task="Scan",
        tenant_id=str(ctx.tenant_id),
    )
    outcome = await executor(request)

    assert outcome.ok is False
    assert "worker exploded" in (outcome.error or "")


async def test_leaf_executor_respects_leaf_max_tool_rounds():
    ctx = _ctx()
    seen: dict = {}

    async def spawn_double(spawn_ctx, spec, task, *, budget=None, **kwargs):
        seen["spec"] = spec
        seen["budget"] = budget
        return SubagentHandle(
            name=spec.name,
            trace_id="t",
            depth=2,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ok"),
        )

    definition = _definition()
    definition["steps"][0]["leaf"]["max_tool_rounds"] = 3
    compiled = compile_workflow(definition)
    executor = build_subagent_leaf_executor(ctx, spawn=spawn_double)
    await executor(
        LeafRequest(
            run_id=str(uuid.uuid4()),
            step_id="scan",
            leaf=compiled.definition.steps[0].leaf,
            task="Scan",
            tenant_id=str(ctx.tenant_id),
        )
    )

    assert seen["budget"].max_tool_rounds == 3
