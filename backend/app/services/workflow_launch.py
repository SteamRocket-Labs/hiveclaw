"""Workflow launch surface (§9 P4): risk grading + real-spawn leaf + launcher.

Three responsibilities, one cohesive "how a run gets started" module:

* :func:`classify_workflow_risk` — §10 decision 3: confirmation is graded by
  RISK, not by ephemeral/registered. Low risk (read-only/workspace, small
  budget/fanout/wait) may start on in-conversation confirmation; high risk
  (external/irreversible effects, big budget, wide fanout, long waits) must
  carry a confirmed plan through PlanModeGate.
* :func:`build_subagent_leaf_executor` — binds ``agent_step`` to the REAL
  axis-1 ``spawn_subagent`` entry (§6.3): governance, tenant isolation,
  delegation token and SubagentBudget are inherited from the spawn ctx, so
  the engine never grows a second execution path.
* :func:`start_ephemeral_workflow_for_agent` — resolves the acting agent's
  runtime (model/tenant), builds ctx + executor, and drives
  :class:`WorkflowRuntimeService`. Single entry for the tool handler and the
  REST surface.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agents.subagent import (
    SubagentBudget,
    SubagentSpawnContext,
    SubagentSpec,
    spawn_subagent,
)
from app.config import get_settings
from app.runtime.workflow_admission import _resolve_args_path
from app.runtime.workflow_compiler import CompiledWorkflow
from app.runtime.workflow_definition import FanoutStep, WaitUntilStep
from app.runtime.workflow_engine import LeafExecutor, LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import WorkflowRunHandle, WorkflowRuntimeService

logger = logging.getLogger(__name__)

RiskLevel = Literal["low", "high"]


@dataclass(slots=True)
class WorkflowRiskAssessment:
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)


def classify_workflow_risk(compiled: CompiledWorkflow, *, args: dict) -> WorkflowRiskAssessment:
    """Grade a compiled definition + args. Thresholds live in Settings."""
    settings = get_settings()
    reasons: list[str] = []

    for step in compiled.definition.steps:
        if step.effects in ("external", "irreversible"):
            reasons.append(f"step {step.id!r} has {step.effects} effects")

    budget = compiled.definition.default_budget.max_total_tokens
    if budget > settings.WORKFLOW_HIGH_RISK_BUDGET_TOKENS:
        reasons.append(f"budget {budget} exceeds high-risk threshold {settings.WORKFLOW_HIGH_RISK_BUDGET_TOKENS}")

    total_wait = 0
    for step in compiled.definition.steps:
        if isinstance(step, FanoutStep):
            try:
                items = _resolve_args_path(step.items_from, args)
            except Exception:
                items = None
            if isinstance(items, list) and len(items) > settings.WORKFLOW_HIGH_RISK_FANOUT_ITEMS:
                reasons.append(
                    f"fanout step {step.id!r} spans {len(items)} items (> {settings.WORKFLOW_HIGH_RISK_FANOUT_ITEMS})"
                )
        elif isinstance(step, WaitUntilStep) and step.delay_seconds is not None:
            total_wait += step.delay_seconds
    if total_wait > settings.WORKFLOW_HIGH_RISK_WAIT_SECONDS:
        reasons.append(
            f"total wait {total_wait}s exceeds high-risk threshold {settings.WORKFLOW_HIGH_RISK_WAIT_SECONDS}s"
        )

    return WorkflowRiskAssessment(level="high" if reasons else "low", reasons=reasons)


def build_subagent_leaf_executor(
    ctx: SubagentSpawnContext,
    *,
    spawn=spawn_subagent,
) -> LeafExecutor:
    """LeafRequest → REAL ``spawn_subagent`` call (§6.3 leaf executor).

    Governance / capability gate / tenant / delegation token all ride on
    ``ctx`` exactly as they do for every axis-1 worker — the workflow engine
    never opens a second execution path. ``spawn`` is injectable for tests.
    """

    async def leaf(request: LeafRequest) -> LeafOutcome:
        spec = SubagentSpec(
            name=request.leaf.name,
            type=request.leaf.type,
            max_tool_rounds=request.leaf.max_tool_rounds,
        )
        budget = SubagentBudget(max_tool_rounds=request.leaf.max_tool_rounds or SubagentBudget().max_tool_rounds)
        handle = await spawn(ctx, spec, request.task, budget=budget)
        result = handle.result
        if result is None:
            return LeafOutcome(ok=False, error="spawn returned no result (background spawn not valid for leaves)")
        if not result.ok:
            return LeafOutcome(
                ok=False,
                error=result.error or f"leaf {request.leaf.name!r} finished with status {result.status}",
                tokens_used=result.tokens_used,
            )
        return LeafOutcome(
            ok=True,
            output={"text": result.content, "sources": result.sources},
            tokens_used=result.tokens_used,
        )

    return leaf


async def resolve_agent_runtime(agent_id: uuid.UUID, *, session_factory=None) -> tuple[Any, Any]:
    """Load the acting agent + a usable model (same resolution rules as the
    orchestrator's resumable-target lookup). Raises LookupError when the
    agent or model cannot be resolved."""
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    async with tenant_scoped_session(session_factory=session_factory) as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if agent is None:
            raise LookupError(f"agent {agent_id} not found")
        model = None
        if agent.primary_model_id:
            model = (
                await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
                )
            ).scalar_one_or_none()
        if model is None and agent.fallback_model_id:
            model = (
                await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id
                    )
                )
            ).scalar_one_or_none()
        if model is None:
            raise LookupError(f"agent {agent_id} has no resolvable model")
    return agent, model


async def start_ephemeral_workflow_for_agent(
    *,
    agent_id: uuid.UUID,
    definition: dict,
    args: dict,
    user_id: uuid.UUID | None = None,
    confirmed_plan_id: uuid.UUID | str | None = None,
    ledger_todo_id: str | None = None,
    session_factory=None,
    spawn=spawn_subagent,
) -> WorkflowRunHandle:
    """Resolve agent runtime → build governed leaf executor → start the run.

    Risk gating happens BEFORE this function (tool intercept / REST gate);
    this is the post-confirmation launch path shared by both surfaces.
    """
    agent, model = await resolve_agent_runtime(agent_id, session_factory=session_factory)
    tenant_id = agent.tenant_id
    if tenant_id is None:
        raise LookupError(f"agent {agent_id} has no tenant — refusing tenant-less workflow run")

    ctx = SubagentSpawnContext(
        parent_agent_id=agent.id,
        parent_user_id=user_id or agent.id,
        model=model,
        parent_agent_name=getattr(agent, "name", "Agent"),
        role_description=getattr(agent, "role_description", "") or "",
        tenant_id=tenant_id,
    )
    service = WorkflowRuntimeService(session_factory=session_factory)
    executor = build_subagent_leaf_executor(ctx, spawn=spawn)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=definition,
        args=args,
        leaf_executor=executor,
        definition_source="ephemeral",
        agent_id=agent.id,
        confirmed_plan_id=confirmed_plan_id,
    )

    if ledger_todo_id:
        # Observation surface only — mirrors the run onto the agent's ledger
        # todo; failures log, never block (切口③ contract).
        try:
            from app.services.agent_work_ledger import upsert_agent_work_ledger_todo

            upsert_agent_work_ledger_todo(
                agent_id=agent.id,
                item_id=ledger_todo_id,
                status="completed" if handle.outcome.status == "completed" else None,
                evidence_refs=[f"workflow_run:{handle.run_id}"],
            )
        except Exception as exc:
            logger.warning("[Workflow] ledger todo %s mirror failed (non-fatal): %s", ledger_todo_id, exc)

    return handle
