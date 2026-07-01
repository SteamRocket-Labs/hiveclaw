"""Plan Mode handoff target that starts a real Agent Team runtime."""

from __future__ import annotations

import inspect
from typing import Any

from app.services.agent_team_runtime_service import (
    TeamMemberCreateSpec,
    create_agent_team_runtime_result,
    spawn_agent_team_member_runtime,
)
from app.services.plan_mode_core import build_plan_execution_instruction

AGENT_TEAM_TARGET = "agent_team"


class AgentTeamHandoffError(Exception):
    """Agent Team handoff could not start and must be recorded as failed."""


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _load_agent(db: Any, agent_id: Any) -> Any | None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.agent import Agent

    return (await db.execute(select(Agent).options(selectinload(Agent.sponsor)).where(Agent.id == agent_id))).scalar_one_or_none()


async def _load_user(db: Any, user_id: Any) -> Any | None:
    from sqlalchemy import select

    from app.models.user import User

    return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def _load_session(db: Any, session_id: Any) -> Any | None:
    from sqlalchemy import select

    from app.models.chat_session import ChatSession

    return (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()


def _execution_contract(plan: Any) -> dict[str, Any]:
    plan_json = getattr(plan, "plan_json", None)
    if not isinstance(plan_json, dict):
        return {}
    contract = plan_json.get("execution_contract")
    return dict(contract) if isinstance(contract, dict) else {}


def _member_specs(contract: dict[str, Any]) -> list[dict[str, Any]]:
    members = contract.get("members")
    if not isinstance(members, list):
        return []
    return [dict(item) for item in members if isinstance(item, dict) and str(item.get("name") or "").strip()]


def _plan_prompt(plan: Any, *, member: dict[str, Any]) -> str:
    explicit = str(member.get("prompt") or "").strip()
    if explicit:
        return explicit
    plan_json = getattr(plan, "plan_json", None) if isinstance(getattr(plan, "plan_json", None), dict) else {}
    return build_plan_execution_instruction(
        plan_id=getattr(plan, "id", ""),
        plan_version=getattr(plan, "plan_version", ""),
        plan_markdown=str(plan_json.get("plan_markdown") or ""),
        objective=str(plan_json.get("objective") or ""),
        original_request=str(getattr(plan, "original_request", "") or ""),
        source="live",
    )


async def agent_team_handoff(db: Any, plan: Any) -> dict[str, Any]:
    if getattr(plan, "status", None) != "confirmed":
        raise AgentTeamHandoffError(
            f"agent_team handoff requires confirmed plan (status={getattr(plan, 'status', None)!r})"
        )

    contract = _execution_contract(plan)
    if str(contract.get("type") or "").strip() not in {"agent_team", "team"}:
        raise AgentTeamHandoffError("agent_team handoff requires execution_contract.type='agent_team'")
    members = _member_specs(contract)
    if not members:
        raise AgentTeamHandoffError("agent_team handoff requires at least one member spec")

    session_id = getattr(plan, "session_id", None)
    user_id = getattr(plan, "requested_by_user_id", None)
    if not session_id or not user_id:
        raise AgentTeamHandoffError("agent_team handoff requires a live session and requesting user")

    from app.core.permissions import is_agent_expired

    agent = await _maybe_await(_load_agent(db, plan.agent_id))
    user = await _maybe_await(_load_user(db, user_id))
    parent_session = await _maybe_await(_load_session(db, session_id))
    if agent is None:
        raise AgentTeamHandoffError(f"agent {plan.agent_id} not found")
    if user is None:
        raise AgentTeamHandoffError(f"requesting user {user_id} not found")
    if parent_session is None:
        raise AgentTeamHandoffError(f"session {session_id} not found")
    if is_agent_expired(agent):
        raise AgentTeamHandoffError(f"agent {plan.agent_id} is expired")

    create_result = await create_agent_team_runtime_result(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        name=str(contract.get("name") or "Plan Agent Team").strip(),
        members=[],
        source="plan_mode_handoff",
        metadata={
            "approved_plan_id": str(plan.id),
            "approved_plan_version": getattr(plan, "plan_version", None),
            "approved_plan_hash": getattr(plan, "plan_hash", None),
            "execution_contract_type": "agent_team",
            "team_create_semantics": "container_only",
        },
    )
    team = create_result.team

    member_runs: list[dict[str, Any]] = []
    for raw_member in members:
        spawn_result = await spawn_agent_team_member_runtime(
            db=db,
            agent=agent,
            user=user,
            parent_session=parent_session,
            team=team,
            spec=TeamMemberCreateSpec(
                name=str(raw_member.get("name") or "").strip(),
                role=str(raw_member.get("role") or raw_member.get("member_role") or "").strip(),
                model_id=raw_member.get("model_id"),
                tool_policy=raw_member.get("tool_policy") if isinstance(raw_member.get("tool_policy"), dict) else None,
                budget=raw_member.get("budget") if isinstance(raw_member.get("budget"), dict) else None,
                prompt=str(raw_member.get("prompt") or "").strip() or None,
                display_content=str(raw_member.get("display_content") or "").strip() or None,
                metadata={
                    "approved_plan_id": str(plan.id),
                    "approved_plan_version": getattr(plan, "plan_version", None),
                    "approved_plan_hash": getattr(plan, "plan_hash", None),
                    "execution_contract_type": "agent_team",
                },
            ),
            prompt=_plan_prompt(plan, member=raw_member),
            source="plan_mode_agent_team_handoff",
            mode="plan_confirmed",
        )
        run_payload = spawn_result.get("run") if isinstance(spawn_result.get("run"), dict) else {}
        run_results = run_payload.get("results") if isinstance(run_payload.get("results"), list) else []
        first_run = run_results[0] if run_results and isinstance(run_results[0], dict) else {}
        member_payload = spawn_result.get("member") if isinstance(spawn_result.get("member"), dict) else {}
        member_runs.append(
            {
                "member_id": str(member_payload.get("id") or ""),
                "member_name": str(spawn_result.get("member_name") or member_payload.get("member_name") or ""),
                "chat_session_id": str(spawn_result.get("child_session_id") or ""),
                "run_id": first_run.get("run_id"),
                "status": str(first_run.get("status") or spawn_result.get("status") or "queued"),
            }
        )

    await db.flush()
    return {
        "team_id": str(team.id),
        "team_name": team.name,
        "member_runs": member_runs,
        "execution": "agent_team",
    }


def register_agent_team_handoff(service: Any) -> None:
    service.register_handoff_handler(AGENT_TEAM_TARGET, agent_team_handoff)
