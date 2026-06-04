"""§9 P4 red tests: risk classification + Plan Mode wiring + real-spawn leaf.

Covers the launch half of P4:
* ``start_workflow`` joins ACTION_KINDS so PlanModeGate can arbitrate it;
* risk classification decides WHICH launches need a confirmed plan
  (§10 decision 3: graded by risk, not by ephemeral/registered);
* the production leaf executor binds agent_step to the REAL axis-1
  ``spawn_subagent`` entry (asserted via an injected double) and inherits
  tenant / budget / governance context from the spawn ctx.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.agents.subagent import SubagentBudget, SubagentHandle, SubagentResult, SubagentSpawnContext
from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_engine import LeafRequest
from app.services.workflow_launch import (
    build_subagent_leaf_executor,
    classify_workflow_risk,
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


# ── ACTION_KINDS wiring ───────────────────────────────────────────


def test_start_workflow_is_a_plan_action_kind():
    from app.services.plan_mode_core import ACTION_KINDS, intent_type_for_action

    assert "start_workflow" in ACTION_KINDS
    assert intent_type_for_action("start_workflow")  # mapped, does not raise


# ── risk classification (§10 decision 3) ──────────────────────────


def test_read_only_small_workflow_is_low_risk():
    compiled = compile_workflow(_definition())
    assessment = classify_workflow_risk(compiled, args={})
    assert assessment.level == "low"
    assert assessment.reasons == []


def test_external_effects_are_high_risk():
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
    assessment = classify_workflow_risk(compiled, args={})
    assert assessment.level == "high"
    assert any("external" in reason for reason in assessment.reasons)


def test_high_budget_is_high_risk():
    compiled = compile_workflow(_definition(default_budget={"max_total_tokens": 1_500_000}))
    assessment = classify_workflow_risk(compiled, args={})
    assert assessment.level == "high"
    assert any("budget" in reason for reason in assessment.reasons)


def test_wide_fanout_is_high_risk():
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
    assessment = classify_workflow_risk(compiled, args={"targets": [f"t{i}" for i in range(12)]})
    assert assessment.level == "high"
    assert any("fanout" in reason for reason in assessment.reasons)


def test_long_wait_is_high_risk():
    data = _definition()
    data["steps"].append({"id": "wait", "type": "wait_until_step", "delay_seconds": 24 * 3600})
    compiled = compile_workflow(data)
    assessment = classify_workflow_risk(compiled, args={})
    assert assessment.level == "high"
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
