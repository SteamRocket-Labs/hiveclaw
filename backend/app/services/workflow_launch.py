"""Workflow launch surface: confirmation inspection + real-spawn leaf + launcher.

Three responsibilities, one cohesive "how a run gets started" module:

* :func:`inspect_workflow_confirmation_needs` — deterministic preview notes for
  definitions that should be shown to the user before launch. These notes never
  force Plan Mode and do not assign low/high risk levels.
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
from typing import Any

from app.agents.subagent import (
    SubagentBudget,
    SubagentSpawnContext,
    SubagentSpec,
    spawn_subagent,
)
from app.config import get_settings
from app.runtime.workflow_admission import _resolve_args_path, estimate_wait_seconds
from app.runtime.workflow_compiler import CompiledWorkflow
from app.runtime.workflow_definition import FanoutStep, WaitUntilStep
from app.runtime.workflow_engine import LeafExecutor, LeafOutcome, LeafRequest, WorkflowRunOutcome
from app.services.workflow_runtime_service import WorkflowRunHandle, WorkflowRuntimeService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkflowConfirmationAssessment:
    requires_confirmation: bool
    reasons: list[str] = field(default_factory=list)


def inspect_workflow_confirmation_needs(compiled: CompiledWorkflow, *, args: dict) -> WorkflowConfirmationAssessment:
    """Return deterministic user-confirmation notes for a compiled workflow."""
    settings = get_settings()
    reasons: list[str] = []

    for step in compiled.definition.steps:
        if step.effects in ("external", "irreversible"):
            reasons.append(f"step {step.id!r} has {step.effects} effects")

    budget = compiled.definition.default_budget.max_total_tokens
    if budget > settings.WORKFLOW_HIGH_RISK_BUDGET_TOKENS:
        reasons.append(f"budget {budget} exceeds confirmation threshold {settings.WORKFLOW_HIGH_RISK_BUDGET_TOKENS}")

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
        elif isinstance(step, WaitUntilStep):
            total_wait += estimate_wait_seconds(step, args=args)
    if total_wait > settings.WORKFLOW_HIGH_RISK_WAIT_SECONDS:
        reasons.append(
            f"total wait {total_wait}s exceeds confirmation threshold {settings.WORKFLOW_HIGH_RISK_WAIT_SECONDS}s"
        )

    return WorkflowConfirmationAssessment(requires_confirmation=bool(reasons), reasons=reasons)


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
        from app.services.workflow_leaf_presets import resolve_leaf_preset

        preset = resolve_leaf_preset(request.leaf.name)
        spec = SubagentSpec(
            name=request.leaf.name,
            type=request.leaf.type,
            max_tool_rounds=request.leaf.max_tool_rounds,
            allowed_tools=preset.allowed_tools if preset else (),
            excluded_tools=preset.excluded_tools if preset else (),
            system_prompt=preset.system_prompt if preset else "",
            disable_tools=preset.disable_tools if preset else False,
        )
        task = request.task
        if preset is not None and preset.pre_process is not None:
            rewritten = await preset.pre_process(request, ctx)
            if rewritten is not None:
                task = rewritten
        options = preset.options if preset is not None else {}
        budget = SubagentBudget(
            max_tool_rounds=request.leaf.max_tool_rounds or SubagentBudget().max_tool_rounds,
            # Source-capture caps (arms the spawn's on_tool_call capture) —
            # the preset's post_process refines what the spawn captured.
            max_sources=options.get("max_sources"),
            max_source_chars=options.get("max_source_chars"),
        )
        handle = await spawn(ctx, spec, task, budget=budget)
        result = handle.result
        if result is None:
            outcome = LeafOutcome(ok=False, error="spawn returned no result (background spawn not valid for leaves)")
        elif not result.ok:
            outcome = LeafOutcome(
                ok=False,
                error=result.error or f"leaf {request.leaf.name!r} finished with status {result.status}",
                tokens_used=result.tokens_used,
            )
        else:
            outcome = LeafOutcome(
                ok=True,
                output={"text": result.content, "sources": result.sources},
                tokens_used=result.tokens_used,
            )
        if preset is not None and preset.post_process is not None:
            # Deterministic domain logic (ledger bookkeeping, citation gates,
            # artifact writes) — system side, never delegated to the LLM.
            outcome = await preset.post_process(request, ctx, result, outcome)
        return outcome

    return leaf


async def resolve_agent_runtime(
    agent_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | str | None = None,
    session_factory=None,
) -> tuple[Any, Any]:
    """Load the acting agent + a usable model (same resolution rules as the
    orchestrator's resumable-target lookup). Raises LookupError when the
    agent or model cannot be resolved."""
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    async with tenant_scoped_session(str(tenant_id) if tenant_id else None, session_factory=session_factory) as db:
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
    definition_source: str = "ephemeral",
    session_factory=None,
    spawn=spawn_subagent,
    run_id: uuid.UUID | None = None,
    parent_session_id: uuid.UUID | str | None = None,
    root_session_id: uuid.UUID | str | None = None,
    run_metadata: dict[str, Any] | None = None,
    enqueue_only: bool = False,
    budget_run_id: uuid.UUID | str | None = None,
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
        budget_run_id=str(budget_run_id) if budget_run_id else None,
    )
    service = WorkflowRuntimeService(session_factory=session_factory)
    executor = build_subagent_leaf_executor(ctx, spawn=spawn)
    delivery_target = None
    try:
        from app.services.channel_delivery_service import channel_delivery_target

        delivery_target = channel_delivery_target.get(None)
    except Exception:
        delivery_target = None

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=definition,
        args=args,
        leaf_executor=executor,
        definition_source=definition_source,
        agent_id=agent.id,
        user_id=user_id,
        confirmed_plan_id=confirmed_plan_id,
        run_id=run_id,
        delivery_target=delivery_target,
        parent_session_id=parent_session_id,
        root_session_id=root_session_id,
        run_metadata=run_metadata,
        enqueue_only=enqueue_only,
        budget_run_id=budget_run_id,
    )
    if enqueue_only:
        try:
            from app.services.runtime_task_worker import notify_runtime_task_worker

            await notify_runtime_task_worker(reason="workflow_run_created", runtime_task_id=handle.run_id)
        except Exception as exc:
            logger.warning("[Workflow] runtime task worker wakeup failed for %s: %s", handle.run_id, exc)

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


async def execute_claimed_workflow_run(
    run_id: uuid.UUID | str, *, session_factory=None, spawn=spawn_subagent
) -> WorkflowRunOutcome:
    service = WorkflowRuntimeService(session_factory=session_factory)
    from sqlalchemy import select

    from app.database import async_session, tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.services.tenant_resolver import resolve_tenant_for_runtime_task

    run_uuid = uuid.UUID(str(run_id))
    factory = session_factory or async_session
    tenant_value = await resolve_tenant_for_runtime_task(
        run_uuid,
        session_factory=factory,
    )
    if tenant_value is None:
        raise LookupError(f"workflow run {run_id} not found")
    async with tenant_scoped_session(
        tenant_value,
        session_factory=factory,
        require_tenant=True,
        source="workflow_worker_claim_runtime_load",
    ) as db:
        task = (
            await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.id == run_uuid,
                    RuntimeTask.tenant_id == tenant_value,
                )
            )
        ).scalar_one_or_none()
    if task is None or task.task_type != "workflow":
        raise LookupError(f"workflow run {run_id} not found")
    executor = build_resumable_workflow_leaf_executor(session_factory=session_factory, spawn=spawn)
    return await service.resume_run(run_uuid, tenant_id=uuid.UUID(str(tenant_value)), leaf_executor=executor)


def build_resumable_workflow_leaf_executor(
    *,
    session_factory=None,
    spawn=spawn_subagent,
) -> LeafExecutor:
    """Leaf executor for startup/signal resumes.

    A daemon resume receives only a ``LeafRequest``. The original run metadata
    carries ``parent_agent_id`` + tenant, so this executor resolves that agent
    on each leaf and then delegates to the normal real-spawn executor.
    """

    async def leaf(request: LeafRequest) -> LeafOutcome:
        service = WorkflowRuntimeService(session_factory=session_factory)
        tenant_value = request.tenant_id
        run_id = uuid.UUID(str(request.run_id))
        loaded = await service.load_run(run_id, tenant_id=tenant_value)
        if loaded is None:
            return LeafOutcome(ok=False, error=f"workflow run {run_id} not found for resume")
        agent_id = loaded.task.parent_agent_id
        if agent_id is None:
            return LeafOutcome(ok=False, error=f"workflow run {run_id} has no parent agent for real leaf resume")

        agent, model = await resolve_agent_runtime(agent_id, tenant_id=tenant_value, session_factory=session_factory)
        tenant_id = agent.tenant_id
        if tenant_id is None:
            return LeafOutcome(ok=False, error=f"agent {agent_id} has no tenant for workflow resume")

        ctx = SubagentSpawnContext(
            parent_agent_id=agent.id,
            parent_user_id=agent.id,
            model=model,
            parent_agent_name=getattr(agent, "name", "Agent"),
            role_description=getattr(agent, "role_description", "") or "",
            tenant_id=tenant_id,
        )
        executor = build_subagent_leaf_executor(ctx, spawn=spawn)
        return await executor(request)

    return leaf
