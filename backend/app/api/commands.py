"""Unified command surface API."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._plan_gate import enforce_plan_gate, get_plan_mode_gate
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent_session_goal import AgentSessionGoal
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.runtime.hooks import HookEvent, emit_hook
from app.services.agent_tools import execute_tool
from app.services.command_registry import build_default_command_registry
from app.services.diagnostic_command_runtime import DIAGNOSTIC_COMMAND_NAMES, execute_diagnostic_command
from app.services.pack_policy_service import get_agent_pack_policies
from app.services.plan_mode_core import (
    plan_mode_user_declined,
    stamp_confirmed_plan_provenance,
    stamp_user_declined_plan_exemption,
)
from app.services.plan_mode_recommendation_service import (
    PlanRecommendationError,
    require_declined_plan_recommendation,
)
from app.services.session_command_runtime import SESSION_COMMAND_NAMES, execute_session_command
from app.services.task_command_adapter import TaskCommandKind, adapt_task_command
from app.services.team_runtime import TeamIndex, TeamMemberIndex, plan_team_close_consolidation

router = APIRouter(prefix="/agents/{agent_id}/commands", tags=["commands"])


class ExecuteCommandIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    origin: str = Field(default="web", pattern="^(web|local|remote|bridge|agent)$")


_TOOL_BACKED_COMMANDS = frozenset(
    {
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "task_output",
        "task_stop",
        "advanced_plan",
        "verify_plan",
        "load_skill",
        "preview_workflow",
        "start_workflow",
    }
)

_GOAL_COMMANDS = frozenset({"goal_start", "goal_update", "goal_stop"})
_TEAM_COMMANDS = frozenset({"team_create", "team_delete"})
_SCHEDULE_COMMANDS = frozenset({"schedule_create", "schedule_once"})
_METADATA_COMMANDS = frozenset({"permissions", "config"})
_EXTERNAL_PACK_COMMANDS = frozenset(
    {
        "worktree_enter",
        "worktree_exit",
        "diff",
        "commit",
        "commit_push_pr",
        "pr_comments",
        "github_review",
        "review",
        "security_review",
        "lsp",
        "notebook",
        "shell_pack",
    }
)
_MCP_ACTION_TO_TOOL = {
    "list_tools": "list_mcp_tools",
    "inspect_tool": "inspect_mcp_tool",
    "import_server": "import_mcp_server",
    "call_tool": "call_mcp_tool",
    "list_resources": "mcp_list_resources",
    "read_resource": "mcp_read_resource",
}


async def _coding_pack_enabled(db: AsyncSession, agent: Any) -> bool:
    policies = await get_agent_pack_policies(
        db,
        getattr(agent, "tenant_id", None),
        getattr(agent, "id", None),
    )
    return bool(policies.get("coding_pack"))


def _parse_uuid(value: uuid.UUID | str | None, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a UUID") from exc


async def _load_chat_session(db: AsyncSession, *, agent_id: uuid.UUID, session_id: str | uuid.UUID | None) -> ChatSession:
    session_uuid = _parse_uuid(session_id, field="session_id")
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_uuid,
            ChatSession.agent_id == agent_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


def _goal_payload(goal: AgentSessionGoal, *, requires_api_persist: bool = False) -> dict[str, Any]:
    return {
        "requires_api_persist": requires_api_persist,
        "id": str(goal.id),
        "agent_id": str(goal.agent_id),
        "session_id": str(goal.chat_session_id),
        "objective": goal.objective,
        "status": goal.status,
        "token_budget": goal.token_budget,
        "tokens_used": goal.tokens_used,
        "time_budget_seconds": goal.time_budget_seconds,
        "max_continuation_turns": goal.max_continuation_turns,
        "continuation_count": goal.continuation_count,
        "blocked_count": goal.blocked_count,
        "completion_summary": goal.completion_summary,
    }


async def _load_session_goal(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID | None,
    arguments: dict[str, Any],
) -> AgentSessionGoal:
    goal_id = arguments.get("goal_id")
    stmt = select(AgentSessionGoal).where(AgentSessionGoal.agent_id == agent_id)
    if goal_id:
        stmt = stmt.where(AgentSessionGoal.id == _parse_uuid(goal_id, field="goal_id"))
    else:
        stmt = stmt.where(
            AgentSessionGoal.chat_session_id == _parse_uuid(session_id, field="session_id"),
            AgentSessionGoal.status == "active",
        )
    result = await db.execute(stmt)
    goal = result.scalar_one_or_none()
    if goal is None:
        raise HTTPException(status_code=404, detail="Session goal not found")
    return goal


async def _execute_goal_command(
    *,
    db: AsyncSession,
    agent: Any,
    user: User,
    command_name: str,
    session_id: str | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if command_name == "goal_start":
        session = await _load_chat_session(db, agent_id=agent.id, session_id=session_id)
        objective = str(arguments.get("objective") or arguments.get("prompt") or "").strip()
        if not objective:
            raise HTTPException(status_code=400, detail="objective is required")
        goal = AgentSessionGoal(
            tenant_id=getattr(agent, "tenant_id", None),
            agent_id=agent.id,
            chat_session_id=session.id,
            created_by_user_id=user.id,
            objective=objective,
            token_budget=arguments.get("token_budget"),
            max_continuation_turns=arguments.get("max_continuation_turns"),
            time_budget_seconds=arguments.get("time_budget_seconds"),
            metadata_json={
                "source": "command",
                "command": "goal_start",
                "session_id": str(session.id),
            },
        )
        db.add(goal)
        await db.flush()
        return {"ok": True, "command": command_name, **_goal_payload(goal)}

    goal = await _load_session_goal(db, agent_id=agent.id, session_id=session_id, arguments=arguments)
    metadata = dict(goal.metadata_json or {})
    metadata["last_command"] = command_name
    metadata["last_command_at"] = datetime.now(timezone.utc).isoformat()

    if command_name == "goal_update":
        if "objective" in arguments:
            objective = str(arguments.get("objective") or "").strip()
            if not objective:
                raise HTTPException(status_code=400, detail="objective must not be empty")
            goal.objective = objective
        for field_name in ("token_budget", "max_continuation_turns", "time_budget_seconds"):
            if field_name in arguments:
                setattr(goal, field_name, arguments.get(field_name))
        if "status" in arguments:
            status_value = str(arguments.get("status") or "").strip()
            if status_value not in {"active", "paused", "blocked", "usage_limited", "budget_limited"}:
                raise HTTPException(status_code=400, detail="Unsupported goal status for update")
            goal.status = status_value
        goal.metadata_json = metadata
        await db.flush()
        return {"ok": True, "command": command_name, **_goal_payload(goal)}

    if command_name == "goal_stop":
        status_value = str(arguments.get("status") or arguments.get("reason_status") or "paused").strip()
        if status_value not in {"paused", "complete", "cancelled", "blocked"}:
            raise HTTPException(status_code=400, detail="status must be paused, complete, cancelled, or blocked")
        goal.status = status_value
        if "completion_summary" in arguments:
            goal.completion_summary = str(arguments.get("completion_summary") or "").strip() or None
        if status_value == "complete":
            goal.completed_at = datetime.now(timezone.utc)
        goal.metadata_json = metadata
        await db.flush()
        return {"ok": True, "command": command_name, **_goal_payload(goal)}

    raise HTTPException(status_code=501, detail=f"Unsupported goal command {command_name!r}")


def _compute_next_schedule_run(cron_expr: str) -> datetime | None:
    try:
        return croniter(cron_expr, datetime.now(timezone.utc)).get_next(datetime).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_once_schedule_at(value: object) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="at is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid one-time timestamp: {raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _schedule_command_payload(trigger: AgentTrigger) -> dict[str, Any]:
    config = dict(trigger.config or {})
    cron_expr = str(config.get("expr") or "")
    once_at = str(config.get("at") or "")
    next_run_at = None
    if trigger.is_enabled and trigger.type == "cron" and cron_expr:
        next_run = _compute_next_schedule_run(cron_expr)
        next_run_at = next_run.isoformat() if next_run else None
    elif trigger.is_enabled and trigger.type == "once":
        next_run_at = once_at or None
    return {
        "id": str(trigger.id),
        "agent_id": str(trigger.agent_id),
        "name": trigger.name,
        "instruction": trigger.reason or "",
        "schedule_type": trigger.type,
        "cron_expr": cron_expr,
        "at": once_at or None,
        "is_enabled": bool(trigger.is_enabled),
        "next_run_at": next_run_at,
        "run_count": int(trigger.fire_count or 0),
        "created_by": config.get("created_by"),
        "delivery_target_json": trigger.reply_context or config.get("delivery_target_json"),
        "created_at": trigger.created_at.isoformat() if trigger.created_at else None,
        "requires_api_persist": False,
    }


def _stamp_declined_schedule_exemption(config: dict[str, Any], recommendation_id: object) -> dict[str, Any]:
    stamped = stamp_user_declined_plan_exemption(config)
    metadata = dict(stamped.get("metadata") or {})
    metadata["plan_recommendation_id"] = str(recommendation_id)
    stamped["metadata"] = metadata
    return stamped


def _plan_recommendation_http_error(exc: PlanRecommendationError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "ok": False,
            "status": "plan_recommendation_required",
            "error": exc.error_code,
            "summary": exc.message,
        },
    )


async def _execute_schedule_command(
    *,
    db: AsyncSession,
    agent: Any,
    user: User,
    command_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if command_name not in _SCHEDULE_COMMANDS:
        raise HTTPException(status_code=501, detail=f"Unsupported schedule command {command_name!r}")
    if not is_agent_creator(user, agent):
        raise HTTPException(status_code=403, detail="Only creator can manage schedules")

    name = str(arguments.get("name") or "").strip()
    instruction = str(arguments.get("instruction") or arguments.get("reason") or "").strip()
    is_enabled = bool(arguments.get("is_enabled", False))
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction is required")
    trigger_type = "cron"
    config: dict[str, Any] = {
        "trigger_class": "scheduled_job",
        "legacy_surface": "command_surface",
        "command": command_name,
        "created_by": str(user.id),
    }
    if command_name == "schedule_create":
        cron_expr = str(arguments.get("cron_expr") or arguments.get("cron") or "").strip()
        if not cron_expr:
            raise HTTPException(status_code=400, detail="cron_expr is required")
        if not _compute_next_schedule_run(cron_expr):
            raise HTTPException(status_code=400, detail=f"Invalid cron expression: {cron_expr}")
        config["expr"] = cron_expr
    else:
        trigger_type = "once"
        config["at"] = _parse_once_schedule_at(arguments.get("at") or arguments.get("once_at")).isoformat()

    user_declined_plan_mode = plan_mode_user_declined(arguments.get("plan_mode_decision"))
    recommendation = None
    if is_enabled and user_declined_plan_mode:
        try:
            recommendation = await require_declined_plan_recommendation(
                db,
                recommendation_id=arguments.get("plan_recommendation_id"),
                agent_id=agent.id,
                user_id=getattr(user, "id", None),
                action_kind="create_enabled_trigger",
            )
        except PlanRecommendationError as exc:
            raise _plan_recommendation_http_error(exc) from exc
    elif is_enabled:
        await enforce_plan_gate(
            db,
            agent_id=agent.id,
            action_kind="create_enabled_trigger",
            gate=get_plan_mode_gate(),
            confirmed_plan_id=arguments.get("confirmed_plan_id"),
            confirmed_plan_version=arguments.get("confirmed_plan_version"),
            confirmed_plan_hash=arguments.get("confirmed_plan_hash"),
        )

    delivery_target_json = arguments.get("delivery_target_json")
    if delivery_target_json is not None and not isinstance(delivery_target_json, dict):
        raise HTTPException(status_code=400, detail="delivery_target_json must be an object")

    if delivery_target_json is not None:
        config["delivery_target_json"] = delivery_target_json
    if is_enabled:
        if user_declined_plan_mode:
            config = _stamp_declined_schedule_exemption(config, recommendation.id)
        else:
            config = stamp_confirmed_plan_provenance(
                config,
                plan_id=arguments.get("confirmed_plan_id"),
                plan_version=arguments.get("confirmed_plan_version"),
                plan_hash=arguments.get("confirmed_plan_hash"),
            )

    trigger = AgentTrigger(
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", None),
        name=name,
        type=trigger_type,
        config=config,
        reason=instruction,
        reply_context=delivery_target_json,
        is_enabled=is_enabled,
        cooldown_seconds=60,
    )
    db.add(trigger)
    await db.flush()
    return {"ok": True, "command": command_name, **_schedule_command_payload(trigger)}


def _team_member_payload(member: AgentTeamMember) -> dict[str, Any]:
    return {
        "id": str(member.id),
        "member_name": member.member_name,
        "member_role": member.member_role,
        "chat_session_id": str(member.chat_session_id),
        "runtime_task_id": str(member.runtime_task_id) if member.runtime_task_id else None,
        "runtime_task_type": member.runtime_task_type,
        "status": member.status,
    }


def _team_payload(team: AgentTeam, members: list[AgentTeamMember], *, requires_api_persist: bool = False) -> dict[str, Any]:
    return {
        "requires_api_persist": requires_api_persist,
        "id": str(team.id),
        "name": team.name,
        "status": team.status,
        "transcript_truth": team.transcript_truth,
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "members": [_team_member_payload(member) for member in members],
    }


async def _load_team_members(db: AsyncSession, *, team_id: uuid.UUID) -> list[AgentTeamMember]:
    result = await db.execute(
        select(AgentTeamMember).where(AgentTeamMember.team_id == team_id).order_by(AgentTeamMember.created_at.asc())
    )
    return list(result.scalars().all())


async def _execute_team_command(
    *,
    db: AsyncSession,
    agent: Any,
    user: User,
    command_name: str,
    session_id: str | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if command_name == "team_create":
        session = await _load_chat_session(db, agent_id=agent.id, session_id=session_id)
        name = str(arguments.get("name") or "").strip()
        members_in = arguments.get("members")
        if not name or not isinstance(members_in, list) or not members_in:
            raise HTTPException(status_code=400, detail="name and non-empty members are required")
        team = AgentTeam(
            id=uuid.uuid4(),
            tenant_id=getattr(agent, "tenant_id", None),
            lead_agent_id=agent.id,
            parent_session_id=session.id,
            name=name[:160],
            created_by_user_id=user.id,
            metadata_json={
                "source": "command",
                "command": "team_create",
                "session_id": str(session.id),
                "member_runtime_policy": "enterable_chat_session",
            },
        )
        db.add(team)
        members: list[AgentTeamMember] = []
        for raw_member in members_in:
            if not isinstance(raw_member, dict):
                raise HTTPException(status_code=400, detail="members must be objects")
            member_name = str(raw_member.get("name") or raw_member.get("member_name") or "").strip()
            if not member_name:
                raise HTTPException(status_code=400, detail="member name is required")
            member_session_id = uuid.uuid4()
            member_session = ChatSession(
                id=member_session_id,
                agent_id=agent.id,
                tenant_id=getattr(agent, "tenant_id", None),
                user_id=user.id,
                title=f"{name} / {member_name}"[:200],
                source_channel="agent_team",
                session_kind="team_member",
                actor_type="agent",
                runtime_source="team_member",
                visibility_scope="team",
                listed_surface="chat",
                parent_session_id=session.id,
                root_session_id=getattr(session, "root_session_id", None) or session.id,
                transcript_metadata_json={
                    "team_id": str(team.id),
                    "member_name": member_name,
                    "member_role": str(raw_member.get("role") or "").strip(),
                    "source": "team_create_command",
                },
            )
            db.add(member_session)
            member = AgentTeamMember(
                id=uuid.uuid4(),
                team_id=team.id,
                member_name=member_name[:160],
                member_role=str(raw_member.get("role") or "").strip() or None,
                model_id=raw_member.get("model_id"),
                chat_session_id=member_session_id,
                tool_policy_json=raw_member.get("tool_policy") if isinstance(raw_member.get("tool_policy"), dict) else None,
                budget_json=raw_member.get("budget") if isinstance(raw_member.get("budget"), dict) else None,
                metadata_json={
                    "runtime_policy": "enterable_chat_session",
                    "direct_chat_supported": True,
                },
            )
            db.add(member)
            members.append(member)
        db.add(
            AgentTeamEvent(
                id=uuid.uuid4(),
                team_id=team.id,
                event_type="team_created",
                payload_json={"name": team.name, "member_count": len(members), "source": "command"},
            )
        )
        await emit_hook(
            HookEvent.TEAM_CREATED,
            agent_id=agent.id,
            session_id=str(session.id),
            source="agent_team",
            metadata={"tenant_id": str(getattr(agent, "tenant_id", "") or ""), "team_id": str(team.id), "name": team.name},
        )
        await db.flush()
        return {"ok": True, "command": command_name, **_team_payload(team, members)}

    if command_name == "team_delete":
        team_id = _parse_uuid(arguments.get("team_id"), field="team_id")
        result = await db.execute(select(AgentTeam).where(AgentTeam.id == team_id, AgentTeam.lead_agent_id == agent.id))
        team = result.scalar_one_or_none()
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        members = await _load_team_members(db, team_id=team.id)
        now = datetime.now(timezone.utc)
        team.status = "closed"
        team.closed_at = now
        for member in members:
            member.status = "closed"
            member.closed_at = now
        db.add(
            AgentTeamEvent(
                id=uuid.uuid4(),
                team_id=team.id,
                event_type="team_closed",
                payload_json={"closed_at": now.isoformat(), "source": "command"},
            )
        )
        await emit_hook(
            HookEvent.TEAM_CLOSED,
            agent_id=agent.id,
            session_id=str(team.parent_session_id),
            source="agent_team",
            metadata={"tenant_id": str(getattr(agent, "tenant_id", "") or ""), "team_id": str(team.id)},
        )
        member_outputs = [
            {
                "member_id": str(member.id),
                "summary": (member.metadata_json or {}).get("summary") or "",
                "artifacts": (member.metadata_json or {}).get("artifacts") or [],
                "work_ledger_deltas": (member.metadata_json or {}).get("work_ledger_deltas") or [],
                "t0_refs": (member.metadata_json or {}).get("t0_refs") or [],
            }
            for member in members
        ]
        team_index = TeamIndex(
            id=team.id,
            tenant_id=team.tenant_id,
            lead_agent_id=team.lead_agent_id,
            parent_session_id=team.parent_session_id,
            name=team.name,
            status="closed",
            transcript_truth=team.transcript_truth,
            members=[
                TeamMemberIndex(
                    id=member.id,
                    member_name=member.member_name,
                    member_role=member.member_role or "",
                    model_id=str(member.model_id) if member.model_id else None,
                    chat_session_id=member.chat_session_id,
                    runtime_task_id=member.runtime_task_id,
                    runtime_task_type=member.runtime_task_type,
                    status="closed",
                    tool_policy=member.tool_policy_json or {},
                    budget=member.budget_json or {},
                )
                for member in members
            ],
        )
        await db.flush()
        return {
            "ok": True,
            "command": command_name,
            **_team_payload(team, members),
            "consolidation_plan": plan_team_close_consolidation(team_index, member_outputs=member_outputs),
        }

    raise HTTPException(status_code=501, detail=f"Unsupported team command {command_name!r}")


def _normalize_tool_command(command_name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    args = dict(arguments or {})
    if command_name.startswith("task_"):
        plan = adapt_task_command(command_name, args, current_session_id=None)
        if plan.kind == TaskCommandKind.DELEGATION_TASK:
            if plan.delegate_action == "create":
                return "delegate_to_agent", plan.delegate_payload
            if plan.delegate_action == "list":
                return "list_async_tasks", plan.delegate_payload
            if plan.delegate_action == "get":
                return "check_async_task", plan.delegate_payload
            if plan.delegate_action == "stop":
                return "cancel_async_task", plan.delegate_payload
        return command_name, args
    if command_name == "load_skill":
        if "name" not in args and "skill_name" in args:
            args["name"] = args.pop("skill_name")
        return "load_skill", args
    if command_name == "mcp":
        action = str(args.pop("action", "list_tools") or "list_tools").strip()
        tool_name = _MCP_ACTION_TO_TOOL.get(action)
        if tool_name is None:
            raise HTTPException(status_code=400, detail=f"Unsupported mcp action {action!r}")
        return tool_name, args
    return command_name, args


def _metadata_command_payload(
    *,
    command_name: str,
    command: Any,
    agent: Any,
    user: User,
    access_level: str,
    session_id: str | None,
) -> dict[str, Any]:
    if command_name == "permissions":
        return {
            "ok": True,
            "command": "permissions",
            "mode": "read_only_runtime_view",
            "agent_id": str(agent.id),
            "user_id": str(user.id),
            "session_id": session_id,
            "access_level": access_level,
            "permission_mode": command.permission_mode,
            "mutation_path": "agent permissions API or approval hooks",
        }
    if command_name == "config":
        return {
            "ok": True,
            "command": "config",
            "mode": "read_only_runtime_view",
            "agent_id": str(agent.id),
            "tenant_id": str(getattr(agent, "tenant_id", "") or "") or None,
            "session_id": session_id,
            "command_origin": "command_surface",
            "mutation_path": "typed admin/config APIs",
        }
    raise HTTPException(status_code=501, detail=f"Unsupported metadata command {command_name!r}")


def _enforce_command_origin_safety(command: Any, origin: str) -> None:
    if origin == "bridge" and not command.bridge_safe:
        raise HTTPException(status_code=403, detail=f"Command {command.name!r} is not bridge-safe")
    if origin == "remote" and not command.remote_safe:
        raise HTTPException(status_code=403, detail=f"Command {command.name!r} is not remote-safe")


@router.get("")
async def list_agent_commands(
    agent_id: uuid.UUID,
    surface: str = Query(default="agent_prompt", pattern="^(agent_prompt|user)$"),
    include_optional_packs: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    coding_pack_enabled = await _coding_pack_enabled(db, agent)
    return build_default_command_registry(
        include_optional_coding_pack=bool(include_optional_packs and coding_pack_enabled) or coding_pack_enabled,
        optional_coding_pack_model_visible=coding_pack_enabled,
    ).visible_index(surface="user" if surface == "user" else "agent_prompt")


@router.get("/{command_name}")
async def get_agent_command(
    agent_id: uuid.UUID,
    command_name: str,
    include_optional_packs: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access = await check_agent_access(db, current_user, agent_id)
    coding_pack_enabled = await _coding_pack_enabled(db, agent)
    registry = build_default_command_registry(
        include_optional_coding_pack=bool(include_optional_packs and coding_pack_enabled) or coding_pack_enabled,
        optional_coding_pack_model_visible=coding_pack_enabled,
    )
    try:
        return registry.get(command_name).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Command not found") from exc


@router.post("/{command_name}/execute")
async def execute_agent_command(
    agent_id: uuid.UUID,
    command_name: str,
    body: ExecuteCommandIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    registry = build_default_command_registry()
    try:
        command = registry.get(command_name)
    except KeyError as exc:
        coding_pack_enabled = await _coding_pack_enabled(db, agent)
        registry = build_default_command_registry(
            include_optional_coding_pack=coding_pack_enabled,
            optional_coding_pack_model_visible=coding_pack_enabled,
        )
        try:
            command = registry.get(command_name)
        except KeyError:
            raise HTTPException(status_code=404, detail="Command not found") from exc
    _enforce_command_origin_safety(command, body.origin)

    if command.name in SESSION_COMMAND_NAMES:
        result = await execute_session_command(
            db=db,
            agent=agent,
            user=current_user,
            access_level=access_level,
            command_name=command.name,
            session_id=body.session_id,
            arguments=body.arguments,
        )
        await db.commit()
        return {"ok": True, "command": command.name, "result": result}
    if command.name in DIAGNOSTIC_COMMAND_NAMES:
        result = await execute_diagnostic_command(
            db=db,
            agent=agent,
            user=current_user,
            command_name=command.name,
            session_id=body.session_id,
            arguments=body.arguments,
        )
        return {"ok": True, "command": command.name, "result": result}
    if command.name in _GOAL_COMMANDS:
        result = await _execute_goal_command(
            db=db,
            agent=agent,
            user=current_user,
            command_name=command.name,
            session_id=body.session_id,
            arguments=body.arguments,
        )
        await db.commit()
        return {"ok": True, "command": command.name, "result": result}
    if command.name in _TEAM_COMMANDS:
        result = await _execute_team_command(
            db=db,
            agent=agent,
            user=current_user,
            command_name=command.name,
            session_id=body.session_id,
            arguments=body.arguments,
        )
        await db.commit()
        return {"ok": True, "command": command.name, "result": result}
    if command.name in _SCHEDULE_COMMANDS:
        result = await _execute_schedule_command(
            db=db,
            agent=agent,
            user=current_user,
            command_name=command.name,
            arguments=body.arguments,
        )
        await db.commit()
        return {"ok": True, "command": command.name, "result": result}
    if command.name in _METADATA_COMMANDS:
        return {
            "ok": True,
            "command": command.name,
            "result": _metadata_command_payload(
                command_name=command.name,
                command=command,
                agent=agent,
                user=current_user,
                access_level=access_level,
                session_id=body.session_id,
            ),
        }
    if command.name in _EXTERNAL_PACK_COMMANDS:
        return {
            "ok": False,
            "command": command.name,
            "result": {
                "ok": False,
                "command": command.name,
                "execution_mode": command.execution_mode,
                "requires_pack_runtime": command.handler_ref,
                "message": "This command is provided by the optional coding pack and must run through the installed pack runtime.",
            },
        }
    if command.name == "mcp":
        tool_name, tool_arguments = _normalize_tool_command(command.name, body.arguments)
    elif command.name in _TOOL_BACKED_COMMANDS:
        tool_name, tool_arguments = _normalize_tool_command(command.name, body.arguments)
    else:
        return {
            "ok": False,
            "command": command.name,
            "result": {
                "ok": False,
                "command": command.name,
                "execution_mode": command.execution_mode,
                "unsupported_reason": "listed_metadata_only_without_runtime_handler",
            },
        }

    result = await execute_tool(
        tool_name,
        tool_arguments,
        agent_id=agent_id,
        user_id=current_user.id,
        session_id=body.session_id,
    )
    parsed: Any
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            parsed = result
    else:
        parsed = result
    return {"ok": True, "command": command.name, "result": parsed}
