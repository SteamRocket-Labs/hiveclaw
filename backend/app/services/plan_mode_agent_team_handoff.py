"""Plan Mode handoff target that starts a real Agent Team runtime."""

from __future__ import annotations

import inspect
import uuid
from typing import Any

from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.chat_session import ChatSession
from app.services.plan_mode_core import build_plan_execution_instruction
from app.services.web_chat_runtime import ActiveWebChatRunExists, start_web_chat_run

AGENT_TEAM_TARGET = "agent_team"


class AgentTeamHandoffError(Exception):
    """Agent Team handoff could not start and must be recorded as failed."""


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _load_agent(db: Any, agent_id: Any) -> Any | None:
    from sqlalchemy import select

    from app.models.agent import Agent

    return (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()


async def _load_user(db: Any, user_id: Any) -> Any | None:
    from sqlalchemy import select

    from app.models.user import User

    return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def _load_session(db: Any, session_id: Any) -> Any | None:
    from sqlalchemy import select

    from app.models.chat_session import ChatSession

    return (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


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


def _stamp_member_runtime(member: AgentTeamMember, run: dict[str, Any], *, status: str) -> None:
    run_id = run.get("run_id") or run.get("id")
    run_uuid = _uuid_or_none(run_id)
    if run_uuid is not None:
        member.runtime_task_id = run_uuid
    member.status = "running" if status in {"running", "queued"} else status
    metadata = dict(member.metadata_json or {})
    metadata["last_runtime_status"] = status
    metadata["last_runtime_payload"] = run
    if run_id:
        metadata["last_runtime_task_id"] = str(run_id)
    member.metadata_json = metadata


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

    team = AgentTeam(
        id=uuid.uuid4(),
        tenant_id=getattr(agent, "tenant_id", None),
        lead_agent_id=agent.id,
        parent_session_id=_uuid_or_none(session_id) or session_id,
        name=str(contract.get("name") or "Plan Agent Team").strip()[:160],
        created_by_user_id=getattr(user, "id", None),
        metadata_json={
            "source": "plan_mode_handoff",
            "approved_plan_id": str(plan.id),
            "approved_plan_version": getattr(plan, "plan_version", None),
            "approved_plan_hash": getattr(plan, "plan_hash", None),
            "execution_contract_type": "agent_team",
        },
    )
    db.add(team)
    db.add(
        AgentTeamEvent(
            team_id=team.id,
            event_type="team_created",
            payload_json={"name": team.name, "member_count": len(members), "source": "plan_mode_handoff"},
        )
    )

    member_runs: list[dict[str, Any]] = []
    for raw_member in members:
        member_session = ChatSession(
            id=uuid.uuid4(),
            agent_id=agent.id,
            tenant_id=getattr(agent, "tenant_id", None),
            user_id=getattr(user, "id", None),
            title=f"{team.name} / {str(raw_member.get('name') or '').strip()}"[:200],
            source_channel="agent_team",
            session_kind="team_member",
            actor_type="agent",
            runtime_source="team_member",
            visibility_scope="team",
            listed_surface="chat",
            parent_session_id=_uuid_or_none(session_id),
            root_session_id=getattr(parent_session, "root_session_id", None) or _uuid_or_none(session_id),
            transcript_metadata_json={
                "team_id": str(team.id),
                "member_name": str(raw_member.get("name") or "").strip(),
                "member_role": str(raw_member.get("role") or "").strip(),
                "source": "plan_mode_agent_team_handoff",
                "approved_plan_id": str(plan.id),
            },
        )
        db.add(member_session)
        member = AgentTeamMember(
            id=uuid.uuid4(),
            team_id=team.id,
            member_name=str(raw_member.get("name") or "").strip()[:160],
            member_role=str(raw_member.get("role") or "").strip() or None,
            model_id=_uuid_or_none(raw_member.get("model_id")),
            chat_session_id=member_session.id,
            tool_policy_json=raw_member.get("tool_policy") if isinstance(raw_member.get("tool_policy"), dict) else None,
            budget_json=raw_member.get("budget") if isinstance(raw_member.get("budget"), dict) else None,
            metadata_json={
                "source": "plan_mode_handoff",
                "approved_plan_id": str(plan.id),
                "runtime_policy": "team_member_session",
            },
        )
        db.add(member)
        await db.flush()
        try:
            run = await start_web_chat_run(
                db=db,
                agent=agent,
                user=user,
                session=member_session,
                content=_plan_prompt(plan, member=raw_member),
                display_content=str(raw_member.get("display_content") or ""),
                runtime_task_type="team_member",
                extra_metadata={
                    "source": "plan_mode_agent_team_handoff",
                    "team_id": str(team.id),
                    "member_id": str(member.id),
                    "member_name": member.member_name,
                    "approved_plan_id": str(plan.id),
                    "approved_plan_version": getattr(plan, "plan_version", None),
                    "approved_plan_hash": getattr(plan, "plan_hash", None),
                    "execution_contract": contract,
                },
            )
            status = str(run.get("status") or "running")
            event_type = "member_run_started"
        except ActiveWebChatRunExists as exc:
            run = {"status": "queued", **exc.run}
            status = "queued"
            event_type = "member_message_queued"
        _stamp_member_runtime(member, run, status=status)
        db.add(
            AgentTeamEvent(
                team_id=team.id,
                sender_member_id=member.id,
                event_type=event_type,
                payload_json={
                    "status": status,
                    "run_id": run.get("run_id"),
                    "runtime_task_type": "team_member",
                    "source": "plan_mode_handoff",
                },
            )
        )
        member_runs.append(
            {
                "member_id": str(member.id),
                "member_name": member.member_name,
                "chat_session_id": str(member.chat_session_id),
                "run_id": run.get("run_id"),
                "status": status,
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
