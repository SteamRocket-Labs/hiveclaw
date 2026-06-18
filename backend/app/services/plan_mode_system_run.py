"""System launcher for explicit Plan Mode authoring.

The explicit "no active agent run" entries — REST ``create``/``regenerate`` /
``revise`` and accepted channel Plan Mode recommendations — have no existing
live user message stream to drive the agent main loop. This launcher lets the
agent author the draft plan in Plan Mode without executing the planned work.

Given a freshly created **draft** plan, it pre-arms the Plan Mode runtime
(read-only policy + scheduler reminder + ``exit_plan_mode``), seeds the loop
with a guiding user prompt, and runs the agent. The agent explores read-only,
then calls ``exit_plan_mode``, which fills THAT draft and lands it
``awaiting_confirmation``. The id the entry point already returned to the
frontend never changes.

Fail-closed: any launcher / agent / ``exit_plan_mode`` failure leaves the plan
in a non-confirmable state (``planning_failed`` if the agent never submitted, or
the existing draft/planning row), and this launcher NEVER executes the planned
work — it only authors a confirmable plan. The caller re-loads the row to
observe the outcome.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.kernel import ExecutionIdentityRef
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.plan_request import AgentPlanRequest
from app.runtime.session import PlanModeState, SessionContext

logger = logging.getLogger(__name__)

SYSTEM_PLAN_RUN_SOURCE = "system_plan_run"
#: Generous-but-bounded exploration budget for a system-initiated plan run. The
#: agent needs room to inspect read-only context before authoring, but Plan
#: Mode's read-only policy already prevents side effects, so this is purely a
#: cost cap (path-unification §12.6).
SYSTEM_PLAN_RUN_MAX_ROUNDS = 20


def _build_launcher_user_prompt(plan: AgentPlanRequest, *, seed_context: dict[str, Any] | None) -> str:
    """Compose the single guiding user turn that drives the plan-mode loop.

    The agent is already constrained by the per-round Plan Mode reminder
    (read-only policy + ``exit_plan_mode`` contract injected by the kernel); this
    prompt only states the request and any structured seed the entry point
    carried (intercepted tool args, intent), and tells the agent to finish by
    calling ``exit_plan_mode``.
    """
    lines = [
        "You are in Plan Mode. Do NOT execute the requested work — plan it only.",
        "Inspect read-only context as needed and design a concrete approach. If a missing decision "
        "materially changes scope, risk, cost, deliverable, recipient, or cadence, ask the user with "
        "ask_user_question before finalizing — do not assume a default. Then submit the final plan by "
        "calling the exit_plan_mode tool. The exit_plan_mode card is the approval mechanism; do not "
        "ask in prose whether the plan is OK.",
        "",
        f"Request to plan (intent_type={plan.intent_type}):",
        (plan.original_request or "").strip() or "(no explicit request text; infer from the seed context below)",
    ]
    if seed_context:
        import json as _json

        lines.extend(
            [
                "",
                "Seed context (a starting hypothesis from the runtime — treat as clues, not the "
                "final answer; rewrite user-visible plan fields in your own words):",
                _json.dumps(seed_context, ensure_ascii=False, sort_keys=True, default=str, indent=2),
            ]
        )
    return "\n".join(lines)


async def _resolve_agent_models(agent_id: UUID) -> tuple[LLMModel | None, LLMModel | None, Agent | None]:
    """Resolve (primary, fallback, agent) tenant-scoped for the plan-mode run."""
    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            return None, None, None

        model = None
        fallback_model = None
        model_id = agent.primary_model_id or agent.fallback_model_id
        if model_id:
            model_result = await db.execute(
                select(LLMModel).where(LLMModel.id == model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            model = model_result.scalar_one_or_none()
        if agent.fallback_model_id and agent.fallback_model_id != model_id:
            fallback_result = await db.execute(
                select(LLMModel).where(LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            fallback_model = fallback_result.scalar_one_or_none()
        return model, fallback_model, agent


async def launch_system_plan_run(
    plan: AgentPlanRequest,
    *,
    seed_context: dict[str, Any] | None = None,
) -> AgentPlanRequest:
    """Run a system-initiated Plan Mode pass that fills ``plan`` (a draft).

    Pre-arms Plan Mode with ``plan.id`` and runs the agent main loop; the agent's
    ``exit_plan_mode`` fills the draft and lands it ``awaiting_confirmation``. The
    caller passes a freshly created draft (``create_plan_request``) and re-loads
    the row afterwards to read the authored result.

    Fail-closed: if the agent cannot author a plan the row is left non-confirmable
    (this launcher never executes the work). The plan id is stable throughout.
    """
    # Lazy import: invoke_agent pulls in a large slice of the runtime; importing
    # it at module load would risk an import cycle through the service layer.
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.services.plan_mode_runtime_context import (
        reset_interactive_plan_mode,
        set_interactive_plan_mode,
    )

    model, fallback_model, agent = await _resolve_agent_models(plan.agent_id)
    if agent is None:
        logger.warning("system_plan_run_agent_not_found", extra={"plan_id": str(plan.id)})
        return plan
    if model is None:
        logger.warning("system_plan_run_model_missing", extra={"plan_id": str(plan.id)})
        return plan

    user_id = plan.requested_by_user_id or getattr(agent, "owner_user_id", None) or agent.creator_id

    state = PlanModeState(
        active=True,
        plan_id=str(plan.id),
        intent_type=plan.intent_type,
        original_request=plan.original_request,
        source=SYSTEM_PLAN_RUN_SOURCE,
        reason="system_plan_run",
    )
    session_context = SessionContext(
        source=SYSTEM_PLAN_RUN_SOURCE,
        channel="internal",
        session_id=plan.session_id,
        plan_mode=state,
        metadata={
            "plan_id": str(plan.id),
            "runtime_task_id": str(plan.runtime_task_id or ""),
            "intent_type": plan.intent_type,
            "tenant_id": str(plan.tenant_id) if plan.tenant_id else None,
        },
    )
    # The metadata mirror + ContextVar must both carry the armed state (the
    # scheduler reads typed state for reminder eligibility/content; exit_plan_mode
    # + the read-only gate read the ContextVar) — same dual-source invariant as
    # explicit Plan Mode runtime.
    session_context.metadata["plan_mode"] = state.to_metadata()
    token = set_interactive_plan_mode(state.to_metadata())
    try:
        await invoke_agent(
            AgentInvocationRequest(
                model=model,
                fallback_model=fallback_model,
                messages=[{"role": "user", "content": _build_launcher_user_prompt(plan, seed_context=seed_context)}],
                memory_messages=[{"role": "user", "content": plan.original_request or ""}],
                agent_name=agent.name,
                role_description=agent.role_description or "",
                agent_id=plan.agent_id,
                user_id=user_id,
                execution_identity=ExecutionIdentityRef(
                    identity_type="agent_bot",
                    identity_id=plan.agent_id,
                    label=f"Agent: {agent.name} (system plan run)",
                ),
                session_context=session_context,
                core_tools_only=False,
                expand_tools=True,
                max_tool_rounds=SYSTEM_PLAN_RUN_MAX_ROUNDS,
            )
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed: never execute, just leave non-confirmable.
        logger.warning(
            "system_plan_run_failed",
            extra={"plan_id": str(plan.id), "agent_id": str(plan.agent_id), "error": str(exc)},
        )
    finally:
        reset_interactive_plan_mode(token)
    return plan


__all__ = [
    "SYSTEM_PLAN_RUN_SOURCE",
    "SYSTEM_PLAN_RUN_MAX_ROUNDS",
    "launch_system_plan_run",
]
