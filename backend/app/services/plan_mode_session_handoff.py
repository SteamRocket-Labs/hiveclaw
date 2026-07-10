"""Plan Mode current-session continuation handoff — confirmed plan -> live run.

CC/Codex parity (``docs/plan-mode-cc-alignment.md`` §4.2): Plan Mode is the
current session's runtime mode. After the user confirms a plan, the DEFAULT is to
keep executing in the SAME chat session and stream the result — not to fan the
work out to a detached background task. The previous default target ``long_task``
had no registered handler, so a confirmed live-chat plan resolved to
``handoff_status="skipped"``, no ``RuntimeTask`` was created, and the agent then
wrongly reported "not confirmed" (the production "用不了" chain).

This handler closes that chain: it loads the plan's agent / requesting user /
chat session and starts a durable ``web_chat_turn`` run (reusing
:func:`start_web_chat_run`) whose prompt carries the approved plan as the agent's
marching orders. The run streams to the same session over the existing broadcast
path; its id lands on ``handoff_payload.runtime_task_id`` so the UI and the agent
can truthfully answer "started?".

Registered for BOTH ``continue_current_session`` (the new live-chat default) and
``long_task`` (legacy/compat): a long_task plan with a live session continues in
that session; without one it fails closed with a visible reason instead of the
old silent ``skipped``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.services.plan_mode_core import build_plan_execution_instruction

logger = logging.getLogger(__name__)

CONTINUE_TARGET = "continue_current_session"
#: Deprecated handoff *target* word. Since G/H.2 the skeleton no longer seeds it
#: (``long_task`` *intent* now seeds ``continue_current_session``); this stays
#: registered only so already-persisted plans whose ``handoff.target == "in_session_execution"``
#: still resolve to the continuation handler instead of degrading to ``skipped``.
LEGACY_LONG_TASK_TARGET = "long_task"


class SessionHandoffError(Exception):
    """A continuation that could not start; :class:`PlanModeService` records it as
    ``handoff_status="failed"`` with the reason — never a silent success."""


def _runtime_run_id(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("run_id") or payload.get("id")
    return str(value) if value else None


# Split-out loaders so tests can stub them without faking a DB engine (mirrors the
# ``_load_agent`` seam in ``plan_mode_handoff.py``).
async def _load_agent(db: Any, agent_id: Any) -> Any | None:
    from app.models.agent import Agent

    return (
        await db.execute(select(Agent).options(selectinload(Agent.sponsor)).where(Agent.id == agent_id))
    ).scalar_one_or_none()


async def _load_user(db: Any, user_id: Any) -> Any | None:
    from app.models.user import User

    return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def _load_session(db: Any, session_id: Any) -> Any | None:
    from app.models.chat_session import ChatSession

    return (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()


def _plan_execution_prompt(plan: Any) -> str:
    """The continuation run's prompt — the agent's marching orders.

    Amendment ④ / E: delegates to the shared
    :func:`build_plan_execution_instruction` so the live-continuation and
    scheduled-trigger paths render from one source (no wording drift). The
    plan_markdown body is the substance; objective / original_request are the
    fallbacks for machine-intercepted plans with no authored article.
    """
    plan_json = plan.plan_json or {}
    return build_plan_execution_instruction(
        plan_id=plan.id,
        plan_version=plan.plan_version,
        plan_markdown=str(plan_json.get("plan_markdown") or ""),
        objective=str(plan_json.get("objective") or ""),
        original_request=str(plan.original_request or ""),
        source="live",
    )


async def continue_current_session_handoff(db: Any, plan: Any) -> dict[str, Any]:
    """Start a same-session continuation run for a confirmed plan.

    Returns the audit payload (``runtime_task_id`` / ``session_id``). When a run is
    already active in the session, returns ``handoff_status="queued"`` so the UI
    shows "confirmed, waiting for the current run" rather than an error.

    Raises:
        SessionHandoffError: not confirmed, no live session/user, missing entity,
        or expired agent — recorded as ``handoff_status="failed"`` (fail closed,
        visible reason), never the old silent ``skipped``.
    """
    if getattr(plan, "status", None) != "confirmed":
        raise SessionHandoffError(f"continuation requires a confirmed plan (status={getattr(plan, 'status', None)!r})")

    session_id = getattr(plan, "session_id", None)
    user_id = getattr(plan, "requested_by_user_id", None)
    if not session_id or not user_id:
        raise SessionHandoffError(
            "continue_current_session requires a live chat session and requesting user; this plan "
            "has none — use a detached/objective target for unattended execution"
        )

    from app.core.permissions import is_agent_expired
    from app.services.web_chat_runtime import ActiveWebChatRunExists, start_web_chat_run

    agent = await _load_agent(db, plan.agent_id)
    if agent is None:
        raise SessionHandoffError(f"agent {plan.agent_id} not found for plan {plan.id}")
    if is_agent_expired(agent):
        raise SessionHandoffError(f"agent {plan.agent_id} is expired")

    user = await _load_user(db, user_id)
    if user is None:
        raise SessionHandoffError(f"requesting user {user_id} not found for plan {plan.id}")

    session = await _load_session(db, session_id)
    if session is None:
        raise SessionHandoffError(f"chat session {session_id} not found for plan {plan.id}")

    extra_metadata = {
        "approved_plan_id": str(plan.id),
        "approved_plan_version": plan.plan_version,
        "approved_plan_hash": plan.plan_hash,
        "source": "plan_mode_handoff",
    }
    plan_json = plan.plan_json if isinstance(plan.plan_json, dict) else {}
    execution_contract = plan_json.get("execution_contract")
    if isinstance(execution_contract, dict) and execution_contract:
        extra_metadata["execution_contract"] = dict(execution_contract)

    try:
        run = await start_web_chat_run(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content=_plan_execution_prompt(plan),
            display_content="✅ 计划已确认，开始执行",
            extra_metadata=extra_metadata,
        )
    except ActiveWebChatRunExists as exc:
        active_run = getattr(exc, "run", None) or {}
        active_id = _runtime_run_id(active_run)
        logger.info(
            "plan_continue_session_queued",
            extra={"plan_id": str(plan.id), "session_id": str(session_id), "active_run_id": active_id},
        )
        return {
            "handoff_status": "queued",
            "reason": "active_run_exists",
            "session_id": str(session_id),
            "active_run_id": active_id,
        }

    run_id = _runtime_run_id(run)
    logger.info(
        "plan_continue_session_started",
        extra={"plan_id": str(plan.id), "session_id": str(session_id), "runtime_task_id": run_id},
    )
    return {"runtime_task_id": run_id, "session_id": str(session_id), "execution": "current_session"}


def register_continue_current_session_handoff(service: Any) -> None:
    """Register the continuation handler for the new + legacy targets.

    ``continue_current_session`` is the new live-chat default; ``long_task`` is the
    legacy target that previously had NO handler (the skipped bug) — routing it here
    means a legacy plan with a live session continues, and one without fails closed.
    """
    service.register_handoff_handler(CONTINUE_TARGET, continue_current_session_handoff)
    service.register_handoff_handler(LEGACY_LONG_TASK_TARGET, continue_current_session_handoff)
