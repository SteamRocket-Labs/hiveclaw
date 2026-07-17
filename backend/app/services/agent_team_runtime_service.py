"""Unified Agent Team runtime operations.

Agent Team is the CC AgentTool teammate branch expressed in Hive session
runtime terms: ``team_create`` creates a session-local team container and
``spawn_subagent(team_name + name)`` creates/starts addressable teammate child
sessions. It is deliberately separate from A2A employee delegation.
"""

from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.user import User
from app.runtime.decision_ledger import build_agent_cycle_decision_entry
from app.runtime.hooks import HookEvent, emit_hook
from app.services.agent_team_contract import teammate_creation_discovery
from app.services.agent_session_continuation import continue_agent_session_from_mailbox
from app.services.chat_message_parts import build_session_native_event
from app.services.chat_transcript import append_session_event
from app.services.execution_admission import ExecutionAdmission
from app.services.runtime_budget_service import RuntimeBudgetDenied, RuntimeBudgetReservation, RuntimeBudgetService
from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification
from app.services.runtime_root_ledger import (
    RuntimeRootIntentSpec,
    read_runtime_root_coverage,
    register_runtime_root_item,
    transition_runtime_root_item,
)
from app.services.tenant_resolver import resolve_tenant_for_agent

ACTIVE_AGENT_TEAM_MEMBER_STATUSES = frozenset(
    {"created", "pending", "queued", "running", "started", "in_progress", "resuming"}
)


@dataclass(frozen=True)
class TeamMemberCreateSpec:
    name: str
    role: str = ""
    model_id: uuid.UUID | str | None = None
    tool_policy: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    prompt: str | None = None
    display_content: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentTeamRuntimeCreateResult:
    team: AgentTeam
    members: list[AgentTeamMember]
    member_sessions: list[ChatSession]
    payload: dict[str, Any]


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


async def _resolve_team_member_model_id(
    db: Any,
    *,
    tenant_id: uuid.UUID | None,
    spec: TeamMemberCreateSpec,
) -> tuple[uuid.UUID | None, str | None]:
    selector_value = spec.model_id
    if selector_value is None and isinstance(spec.metadata, dict):
        selector_value = spec.metadata.get("model")
    selector = str(selector_value or "").strip()
    if not selector or selector.lower() in {"inherit", "default"}:
        return None, None
    if tenant_id is None:
        raise ValueError("Agent tenant is required for Team member model selection")

    selector_uuid = _uuid_or_none(selector)
    identity_filter = (
        LLMModel.id == selector_uuid
        if selector_uuid is not None
        else or_(LLMModel.label == selector, LLMModel.model == selector)
    )
    result = await db.execute(
        select(LLMModel)
        .where(
            LLMModel.tenant_id == tenant_id,
            LLMModel.enabled.is_(True),
            identity_filter,
        )
        .order_by(LLMModel.id)
    )
    matches = list(result.scalars().all())
    if len(matches) != 1:
        raise ValueError("Team member model is unavailable or ambiguous in this tenant")
    return matches[0].id, selector


def _team_member_payload(member: AgentTeamMember) -> dict[str, Any]:
    metadata = member.metadata_json or {}
    return {
        "id": str(member.id),
        "member_name": member.member_name,
        "member_role": member.member_role,
        "chat_session_id": str(member.chat_session_id),
        "runtime_task_id": str(member.runtime_task_id) if member.runtime_task_id else None,
        "runtime_task_type": member.runtime_task_type,
        "status": member.status,
        "last_turn_status": metadata.get("last_turn_status"),
        "last_runtime_status": metadata.get("last_runtime_status"),
        "summary": metadata.get("summary") or "",
    }


def _unique_member_name(requested: str, existing: list[AgentTeamMember]) -> str:
    base = str(requested or "").strip()
    if not base:
        raise ValueError("Team member name is required")
    used = {str(member.member_name or "").strip().lower() for member in existing}
    if base.lower() not in used:
        return base
    idx = 2
    while True:
        candidate = f"{base}-{idx}"
        if candidate.lower() not in used:
            return candidate
        idx += 1


def _team_task_list_payload(team_name: str) -> dict[str, Any]:
    return {
        "id": team_name,
        "owner_field": "member_name",
        "create_tool": "track_todo",
        "claim_tool": "track_todo",
        "complete_tool": "track_todo",
        "list_tool": "read_ledger",
        "coordination_contract": "cc_team_shared_task_list",
    }


def _teammate_lifecycle_payload() -> dict[str, Any]:
    return {
        "idle_after_each_turn": True,
        "address_by": "member_name",
        "message_tool": "send_agent_session_message",
        "terminal_turn_status_field": "last_turn_status",
    }


def _object_field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _metadata_dict(value: Any) -> dict[str, Any]:
    metadata = _object_field(value, "metadata_json", {})
    return metadata if isinstance(metadata, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def team_close_projection(team: Any) -> dict[str, str | None]:
    metadata = _metadata_dict(team)
    return {
        "close_status": str(metadata.get("close_synthesis_status") or "").strip() or None,
        "close_failure": str(metadata.get("close_failure") or "").strip() or None,
    }


def _effective_member_turn_status(member: Any) -> str:
    metadata = _metadata_dict(member)
    return str(
        metadata.get("last_turn_status") or metadata.get("status") or _object_field(member, "status", "")
    ).lower()


def build_agent_team_decision_entry(
    team: Any,
    members: list[Any],
    *,
    close_summary_ref: str | None = None,
) -> dict[str, Any]:
    team_metadata = _metadata_dict(team)
    member_statuses: list[dict[str, Any]] = []
    open_tasks = list(_list_value(team_metadata.get("open_tasks")))
    has_running = False
    has_failed = False
    all_idle_or_done = bool(members)
    for member in members:
        metadata = _metadata_dict(member)
        runtime_status = str(_object_field(member, "status", "") or "").lower()
        last_turn_status = _effective_member_turn_status(member)
        effective_status = last_turn_status or runtime_status
        if runtime_status in {"running", "queued", "started"} or effective_status in {"running", "queued", "started"}:
            has_running = True
        if effective_status in {"failed", "killed", "cancelled", "canceled"}:
            has_failed = True
        if runtime_status not in {"idle", "completed", "closed"} and effective_status not in {"completed", "closed"}:
            all_idle_or_done = False
        open_tasks.extend(_list_value(metadata.get("open_tasks")))
        member_statuses.append(
            {
                "member_id": str(_object_field(member, "id", "")),
                "member_name": str(_object_field(member, "member_name", "")),
                "runtime_task_id": str(_object_field(member, "runtime_task_id", "") or "") or None,
                "runtime_status": runtime_status or None,
                "last_turn_status": last_turn_status or None,
                "summary": str(metadata.get("summary") or ""),
            }
        )

    team_status = str(_object_field(team, "status", "") or "active").lower()
    if team_status == "closed":
        team_outcome = "closed"
    elif has_failed:
        team_outcome = "failed"
    elif has_running:
        team_outcome = "running"
    elif all_idle_or_done:
        team_outcome = "idle"
    else:
        team_outcome = team_status or "active"

    lead_required_actions: list[str] = []
    if team_outcome == "running":
        lead_required_actions.append("wait_for_members")
    if team_outcome == "failed":
        lead_required_actions.append("review_failed_members")
    if open_tasks:
        lead_required_actions.append("resolve_open_tasks")
    if team_outcome == "idle" and not lead_required_actions:
        lead_required_actions.append("close_or_continue_team")
    next_action = lead_required_actions[0] if lead_required_actions else "observe_team"

    return {
        "schema": "hive.ccplus.agent_team_decision.v1",
        "team_id": str(_object_field(team, "id", "")),
        "team_name": str(_object_field(team, "name", "")),
        "member_statuses": member_statuses,
        "open_tasks": open_tasks,
        "lead_required_actions": lead_required_actions,
        "team_outcome": team_outcome,
        "close_summary_ref": close_summary_ref or team_metadata.get("close_summary_ref"),
        "agent_cycle_decision_entry": build_agent_cycle_decision_entry(
            subsystem="agent_team",
            trigger="team_state_projection",
            judge="agent_team_runtime_service.build_agent_team_decision_entry",
            decision=next_action,
            outcome=team_outcome,
            next_action=next_action,
            model_interaction="team_completion_wake" if team_outcome in {"idle", "failed", "closed"} else "none",
            user_visible=True,
            permission_result="member_runtime_inherited",
            budget_result="team_member_budget",
            details={
                "team_id": str(_object_field(team, "id", "")),
                "member_count": len(member_statuses),
                "open_task_count": len(open_tasks),
            },
        ),
    }


def team_payload(
    team: AgentTeam, members: list[AgentTeamMember], *, requires_api_persist: bool = False
) -> dict[str, Any]:
    decision_entry = build_agent_team_decision_entry(team, list(members))
    return {
        "requires_api_persist": requires_api_persist,
        "id": str(team.id),
        "name": team.name,
        "status": team.status,
        "transcript_truth": team.transcript_truth,
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "members": [_team_member_payload(member) for member in members],
        "agent_team_decision_entry": decision_entry,
        "team_outcome": decision_entry["team_outcome"],
        "lead_required_actions": decision_entry["lead_required_actions"],
        **team_close_projection(team),
        "team_task_list": _team_task_list_payload(team.name),
        "teammate_lifecycle": _teammate_lifecycle_payload(),
        **teammate_creation_discovery(team.name),
    }


async def _append_team_member_parent_event(
    *,
    db: Any,
    agent: Any,
    user: Any,
    parent_session: Any,
    team: AgentTeam,
    member: AgentTeamMember,
    source: str,
    command: str | None,
) -> None:
    payload = {
        "type": "team_member",
        "status": "created",
        "message": f"Team member created: {member.member_name}",
        "team_id": str(team.id),
        "team_name": team.name,
        "child_session_id": str(member.chat_session_id),
        "member_id": str(member.id),
        "member_name": member.member_name,
        "command": command,
        "source": source,
    }
    event = build_session_native_event(payload)
    await append_session_event(
        db=db,
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", None),
        session_id=str(parent_session.id),
        actor_type="system",
        event_type="team_member",
        role="system",
        user_id=getattr(user, "id", None),
        root_session_id=str(getattr(parent_session, "root_session_id", None) or parent_session.id),
        parent_session_id=str(parent_session.id),
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        source="agent_team",
        parts=[event["part"]] if isinstance(event.get("part"), dict) else None,
        metadata={"source": source, **{key: value for key, value in payload.items() if value is not None}},
    )


def _build_team_member_records(
    *,
    agent: Any,
    user: Any,
    parent_session: Any,
    team: AgentTeam,
    spec: TeamMemberCreateSpec,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[AgentTeamMember, ChatSession]:
    member_name = str(spec.name or "").strip()
    if not member_name:
        raise ValueError("Team member name is required")
    parent_session_id = _uuid_or_none(getattr(parent_session, "id", None))
    if parent_session_id is None:
        raise ValueError("A valid parent session is required")

    root_session_id = _uuid_or_none(getattr(parent_session, "root_session_id", None)) or parent_session_id
    member_session_id = uuid.uuid4()
    member_role = str(spec.role or "").strip()
    member_metadata = {
        "runtime_policy": "enterable_chat_session",
        "direct_chat_supported": True,
        "source": source,
        **(metadata or {}),
        **(spec.metadata or {}),
    }
    member_session = ChatSession(
        id=member_session_id,
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", None),
        user_id=getattr(user, "id", None),
        title=f"{team.name} / {member_name}"[:200],
        source_channel="agent_team",
        session_kind="team_member",
        actor_type="agent",
        runtime_source="team_member",
        visibility_scope="team",
        listed_surface="parent",
        parent_session_id=parent_session_id,
        root_session_id=root_session_id,
        transcript_metadata_json={
            "team_id": str(team.id),
            "member_name": member_name,
            "member_role": member_role,
            "source": source,
            **(metadata or {}),
            **(spec.metadata or {}),
        },
    )
    member = AgentTeamMember(
        id=uuid.uuid4(),
        team_id=team.id,
        member_name=member_name[:160],
        member_role=member_role or None,
        model_id=_uuid_or_none(spec.model_id),
        chat_session_id=member_session_id,
        tool_policy_json=spec.tool_policy if isinstance(spec.tool_policy, dict) else None,
        budget_json=spec.budget if isinstance(spec.budget, dict) else None,
        metadata_json=member_metadata,
    )
    return member, member_session


async def create_agent_team_runtime_result(
    *,
    db: Any,
    agent: Any,
    user: Any,
    parent_session: Any,
    name: str,
    members: list[TeamMemberCreateSpec],
    source: str,
    command: str | None = None,
    metadata: dict[str, Any] | None = None,
    append_parent_events: bool = True,
    emit_created_hook: bool = True,
) -> AgentTeamRuntimeCreateResult:
    team_name = str(name or "").strip()
    if not team_name:
        raise ValueError("Team name is required")

    parent_session_id = _uuid_or_none(getattr(parent_session, "id", None))
    if parent_session_id is None:
        raise ValueError("A valid parent session is required")
    if members:
        raise ValueError(
            "TeamCreate creates the Team container only; spawn teammates with spawn_subagent team_name + name"
        )

    team = AgentTeam(
        id=uuid.uuid4(),
        tenant_id=getattr(agent, "tenant_id", None),
        lead_agent_id=agent.id,
        parent_session_id=parent_session_id,
        name=team_name[:160],
        created_by_user_id=getattr(user, "id", None),
        metadata_json={
            "source": source,
            "command": command,
            "session_id": str(parent_session_id),
            "member_runtime_policy": "enterable_chat_session",
            "team_task_list": _team_task_list_payload(team_name[:160]),
            "teammate_lifecycle": _teammate_lifecycle_payload(),
            **(metadata or {}),
        },
    )
    db.add(team)

    created_members: list[AgentTeamMember] = []
    member_sessions: list[ChatSession] = []

    db.add(
        AgentTeamEvent(
            id=uuid.uuid4(),
            team_id=team.id,
            event_type="team_created",
            payload_json={"name": team.name, "member_count": len(created_members), "source": source},
        )
    )
    await db.flush()

    if emit_created_hook:
        await emit_hook(
            HookEvent.TEAM_CREATED,
            evidence_db=db,
            agent_id=agent.id,
            session_id=str(parent_session_id),
            source="agent_team",
            metadata={
                "tenant_id": str(getattr(agent, "tenant_id", "") or ""),
                "team_id": str(team.id),
                "name": team.name,
                "runtime_path": "agent_team_runtime_service",
            },
        )
    if append_parent_events:
        for member in created_members:
            await _append_team_member_parent_event(
                db=db,
                agent=agent,
                user=user,
                parent_session=parent_session,
                team=team,
                member=member,
                source=source,
                command=command,
            )

    return AgentTeamRuntimeCreateResult(
        team=team,
        members=created_members,
        member_sessions=member_sessions,
        payload=team_payload(team, created_members, requires_api_persist=False),
    )


async def create_agent_team_runtime(**kwargs: Any) -> dict[str, Any]:
    result = await create_agent_team_runtime_result(**kwargs)
    return result.payload


_TEAM_FANOUT_NAMESPACE = uuid.UUID("1da6b22f-32f0-4d93-ad42-e829a61b43a9")
_TEAM_FANOUT_PRODUCER_LEASE_SECONDS = 60


def _team_fanout_operation_identity(
    *,
    team_id: uuid.UUID,
    operation_id: str | None,
    root_runtime_task_id: uuid.UUID | str | None,
) -> tuple[str, uuid.UUID]:
    operation = str(operation_id or "").strip() or uuid.uuid4().hex
    root_id = _uuid_or_none(root_runtime_task_id)
    if root_id is None:
        root_id = uuid.uuid5(_TEAM_FANOUT_NAMESPACE, f"team:{team_id}:operation:{operation}")
    return operation, root_id


def _team_fanout_work_items(
    *,
    team: AgentTeam,
    members: list[AgentTeamMember],
    operation_id: str,
    root_runtime_task_id: uuid.UUID,
    message: str,
    ordinal_overrides: dict[uuid.UUID, int] | None = None,
) -> list[dict[str, Any]]:
    message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
    items: list[dict[str, Any]] = []
    for ordinal, member in enumerate(members):
        effective_ordinal = int((ordinal_overrides or {}).get(member.id, ordinal))
        stable_material = f"{root_runtime_task_id}:{operation_id}:{member.id}:{message_sha256}"
        run_id = uuid.uuid5(_TEAM_FANOUT_NAMESPACE, stable_material)
        operation_hash = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:20]
        intent_key = f"team:{team.id}:{operation_hash}:member:{member.id}"
        items.append(
            {
                "member": member,
                "ordinal": effective_ordinal,
                "run_id": run_id,
                "intent_key": intent_key,
                "target_ref": f"team-member:{member.id}",
                "reservation_key": f"agent-team:{team.id}:{operation_hash}:{member.id}",
                "message_sha256": message_sha256,
            }
        )
    return items


async def _register_team_fanout_requested_set(
    *,
    db: Any,
    agent: Any,
    user: Any,
    team: AgentTeam,
    operation_id: str,
    root_runtime_task_id: uuid.UUID,
    message: str,
    work_items: list[dict[str, Any]],
    source: str,
    display_content: str,
    interrupt_requested: bool,
    budget_run_id: uuid.UUID | None,
    reserve_new_team_sessions: bool,
) -> None:
    """Commit every requested Team member before the first child can start."""

    lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=_TEAM_FANOUT_PRODUCER_LEASE_SECONDS)
    for item in work_items:
        member = item["member"]
        root_item = await register_runtime_root_item(
            db,
            tenant_id=agent.tenant_id,
            root_runtime_task_id=root_runtime_task_id,
            source_agent_id=agent.id,
            root_user_id=user.id,
            root_session_id=str(team.parent_session_id),
            intent_key=item["intent_key"],
            work_type="team_member",
            target_ref=item["target_ref"],
            path=(f"agent:{agent.id}", f"team:{team.id}"),
            state="requested",
            admission_disposition="requested",
            budget_reservation_key=item["reservation_key"],
            child_session_id=str(member.chat_session_id),
            metadata={
                "schema": "hive.runtime_root_team_intent.v1",
                "operation_id": operation_id,
                "ordinal": item["ordinal"],
                "team_id": str(team.id),
                "member_id": str(member.id),
                "member_name": member.member_name,
                "message": message,
                "display_content": display_content,
                "message_sha256": item["message_sha256"],
                "source": source,
                "budget_run_id": str(budget_run_id) if budget_run_id is not None else None,
                "reserve_new_team_sessions": bool(reserve_new_team_sessions),
                "interrupt_requested": bool(interrupt_requested),
            },
        )
        if root_item.state == "requested" and root_item.runtime_task_id is None:
            root_item.recovery_claimed_by = f"fanout-producer:{operation_id}"[:200]
            root_item.recovery_claim_expires_at = lease_expires_at
    # This is the deliberate durability fence: a crash after this commit leaves
    # the not-yet-admitted members as recoverable deferred rows, never invisible.
    if hasattr(db, "commit"):
        await db.commit()
    else:
        await db.flush()


async def spawn_agent_team_member_runtime(
    *,
    db: Any,
    agent: Any,
    user: Any,
    parent_session: Any,
    team: AgentTeam,
    spec: TeamMemberCreateSpec,
    prompt: str,
    source: str,
    mode: str = "",
    budget_run_id: uuid.UUID | str | None = None,
    budget_service: RuntimeBudgetService | None = None,
    root_runtime_task_id: uuid.UUID | str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Create and start one teammate from the AgentTool ``team_name + name`` branch."""

    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise ValueError("prompt is required for teammate spawn")

    # Name allocation is a read-only prerequisite of admission. It must happen
    # before the reservation key is built so repeated requested names do not
    # collapse distinct teammate sessions onto one idempotency key.
    existing_members: list[AgentTeamMember] = []
    if hasattr(db, "execute"):
        existing_result = await db.execute(
            select(AgentTeamMember).where(AgentTeamMember.team_id == team.id).order_by(AgentTeamMember.created_at.asc())
        )
        existing_members = list(existing_result.scalars().all())
    unique_name = _unique_member_name(spec.name, existing_members)
    resolved_model_id, requested_model = await _resolve_team_member_model_id(
        db,
        tenant_id=getattr(agent, "tenant_id", None),
        spec=spec,
    )

    member_spec = TeamMemberCreateSpec(
        name=unique_name,
        role=spec.role,
        model_id=resolved_model_id,
        tool_policy=spec.tool_policy,
        budget=spec.budget,
        prompt=spec.prompt,
        display_content=spec.display_content,
        metadata={
            **(spec.metadata or {}),
            "agent_tool_branch": "teammate_spawn",
            "mode": mode,
            **({"requested_model": requested_model} if requested_model else {}),
        },
    )
    member, member_session = _build_team_member_records(
        agent=agent,
        user=user,
        parent_session=parent_session,
        team=team,
        spec=member_spec,
        source=source,
    )
    db.add(member_session)
    db.add(member)
    db.add(
        AgentTeamEvent(
            id=uuid.uuid4(),
            team_id=team.id,
            receiver_member_id=member.id,
            event_type="member_spawned",
            payload_json={
                "member_name": member.member_name,
                "member_role": member.member_role,
                "source": source,
                "mode": mode,
            },
        )
    )
    await db.flush()
    await _append_team_member_parent_event(
        db=db,
        agent=agent,
        user=user,
        parent_session=parent_session,
        team=team,
        member=member,
        source=source,
        command="spawn_subagent",
    )
    run_payload = await message_agent_team_members_runtime(
        db=db,
        agent=agent,
        user=user,
        team=team,
        members=[member],
        member_sessions=[member_session],
        message=prompt_text,
        display_content=spec.display_content or prompt_text,
        interrupt_requested=False,
        source=source,
        budget_run_id=_uuid_or_none(budget_run_id),
        budget_service=budget_service,
        root_runtime_task_id=root_runtime_task_id,
        operation_id=operation_id or f"spawn:{member.id}",
        reserve_new_team_sessions=True,
    )
    result_status = str((run_payload.get("results") or [{}])[0].get("status") or "")
    return {
        "ok": bool(run_payload.get("ok")),
        "status": "waiting_budget_approval" if result_status == "waiting_budget_approval" else "teammate_spawned",
        "team_id": str(team.id),
        "team_name": team.name,
        "member": _team_member_payload(member),
        "member_name": member.member_name,
        "child_session_id": str(member.chat_session_id),
        "prompt": prompt_text,
        "run": run_payload,
    }


def _stamp_member_runtime(member: AgentTeamMember, run: dict[str, Any], *, status: str) -> None:
    run_id = run.get("run_id") or run.get("id")
    run_uuid = _uuid_or_none(run_id)
    if run_uuid is not None:
        member.runtime_task_id = run_uuid
    member.status = "running" if status in {"queued", "started", "running"} else status
    metadata = dict(member.metadata_json or {})
    metadata["last_runtime_status"] = status
    metadata["last_runtime_payload"] = run
    if run_id:
        metadata["last_runtime_task_id"] = str(run_id)
    member.metadata_json = metadata


def _member_terminal_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value == "completed":
        return "completed"
    if value in {"failed", "killed", "skipped", "cancelled", "canceled"}:
        return "failed"
    return value or "completed"


async def _wake_parent_session_from_team_member_completion(
    *,
    db: Any,
    member: AgentTeamMember,
    task: Any,
    status: str,
    result_summary: str | None,
    metadata: dict[str, Any],
) -> None:
    run_id = _uuid_or_none(getattr(task, "id", None))
    task_id = str(run_id or getattr(task, "id", "") or member.runtime_task_id or member.id)
    team = (await db.execute(select(AgentTeam).where(AgentTeam.id == member.team_id).limit(1))).scalar_one_or_none()
    if team is None:
        return
    parent_session = (
        await db.execute(
            select(ChatSession)
            .where(
                ChatSession.id == team.parent_session_id,
                ChatSession.agent_id == team.lead_agent_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    parent_agent = (await db.execute(select(Agent).where(Agent.id == team.lead_agent_id).limit(1))).scalar_one_or_none()
    owner_id = (
        getattr(parent_session, "user_id", None)
        or getattr(team, "created_by_user_id", None)
        or getattr(parent_agent, "creator_id", None)
    )
    owner = (
        (await db.execute(select(User).where(User.id == owner_id).limit(1))).scalar_one_or_none() if owner_id else None
    )
    if parent_session is None or parent_agent is None or owner is None:
        return

    summary = result_summary or f"Team member {member.member_name} finished with status {status}."
    await enqueue_completion_notification(
        db,
        CompletionNotification(
            tenant_id=team.tenant_id,
            source_kind="agent_team",
            source_run_id=task_id,
            parent_session_id=parent_session.id,
            parent_agent_id=parent_agent.id,
            parent_user_id=owner.id,
            child_session_id=member.chat_session_id,
            child_agent_name=member.member_name,
            terminal_status=status,
            task_type="team_member",
            summary=summary,
            delivery_mode="parent_continuation",
            metadata={
                "team_id": str(team.id),
                "team_name": team.name,
                "member_id": str(member.id),
                "member_name": member.member_name,
                "runtime_task_id": task_id,
                "runtime_task_type": "team_member",
                "agent_team_decision_entry": metadata.get("agent_team_decision_entry"),
                **({"budget_run_id": str(metadata["budget_run_id"])} if metadata.get("budget_run_id") else {}),
            },
        ),
    )


async def project_agent_team_member_completion(
    *,
    db: Any,
    task: Any,
    status: str,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Project a terminal team-member RuntimeTask back into the team read model."""
    if str(getattr(task, "task_type", "") or "") != "team_member":
        return None

    filters = []
    run_id = _uuid_or_none(getattr(task, "id", None))
    if run_id is not None:
        filters.append(AgentTeamMember.runtime_task_id == run_id)
    child_session_id = _uuid_or_none(getattr(task, "child_session_id", None))
    if child_session_id is not None:
        filters.append(AgentTeamMember.chat_session_id == child_session_id)
    if not filters:
        return None

    member = (
        await db.execute(
            select(AgentTeamMember).where(or_(*filters) if len(filters) > 1 else filters[0]).limit(1).with_for_update()
        )
    ).scalar_one_or_none()
    if member is None:
        return None

    terminal_status = _member_terminal_status(status)
    metadata = dict(member.metadata_json or {})
    source_metadata = dict(metadata_json or {})
    artifacts = source_metadata.get("artifacts") or metadata.get("artifacts") or []
    artifact_paths = source_metadata.get("artifact_paths") or metadata.get("artifact_paths") or []
    t0_refs = source_metadata.get("t0_refs") or source_metadata.get("transcript_refs") or metadata.get("t0_refs") or []
    if result_summary is not None:
        metadata["summary"] = result_summary
    metadata["last_turn_status"] = terminal_status
    metadata["idle_after_turn"] = True
    metadata["status"] = "idle"
    metadata["runtime_task_id"] = str(run_id) if run_id is not None else str(getattr(task, "id", "") or "")
    metadata["artifact_paths"] = artifact_paths
    metadata["artifacts"] = artifacts
    metadata["t0_refs"] = t0_refs
    if source_metadata.get("budget_run_id"):
        metadata["budget_run_id"] = source_metadata["budget_run_id"]
    member.status = "idle"
    member.metadata_json = metadata
    decision_entry = build_agent_team_decision_entry(
        {"id": member.team_id, "name": "", "status": "active", "metadata_json": {}},
        [member],
    )
    metadata["agent_team_decision_entry"] = decision_entry
    member.metadata_json = metadata

    payload = {
        "status": terminal_status,
        "runtime_task_type": "team_member",
        "run_id": metadata["runtime_task_id"],
        "summary": result_summary or "",
        "artifact_paths": artifact_paths,
        "artifacts": artifacts,
        "t0_refs": t0_refs,
        "agent_team_decision_entry": decision_entry,
    }
    db.add(
        AgentTeamEvent(
            id=uuid.uuid4(),
            team_id=member.team_id,
            receiver_member_id=member.id,
            event_type=f"member_{terminal_status}",
            payload_json=payload,
        )
    )
    db.add(
        AgentTeamEvent(
            id=uuid.uuid4(),
            team_id=member.team_id,
            receiver_member_id=member.id,
            event_type="member_idle",
            payload_json={
                **payload,
                "status": "idle",
                "last_turn_status": terminal_status,
            },
        )
    )
    await _wake_parent_session_from_team_member_completion(
        db=db,
        member=member,
        task=task,
        status=terminal_status,
        result_summary=result_summary,
        metadata=metadata,
    )
    return payload


async def project_agent_team_close_completion(
    *,
    db: Any,
    task: Any,
    status: str,
    result_summary: str | None,
) -> dict[str, str] | None:
    """Finalize Team close only after the lead model's synthesis turn terminates."""

    task_metadata = dict(getattr(task, "metadata_json", None) or {})
    team_id = _uuid_or_none(task_metadata.get("agent_team_close_id"))
    if team_id is None:
        return None
    team = (
        await db.execute(select(AgentTeam).where(AgentTeam.id == team_id).limit(1).with_for_update())
    ).scalar_one_or_none()
    if team is None:
        return None
    if team.status == "closed":
        return {"team_id": str(team.id), "status": "closed"}
    if team.status != "closing":
        return None

    members = list(
        (
            await db.execute(
                select(AgentTeamMember)
                .where(AgentTeamMember.team_id == team.id)
                .order_by(AgentTeamMember.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    terminal_status = str(status or "failed").lower()
    succeeded = terminal_status == "completed"
    now = datetime.now(timezone.utc)
    metadata = dict(team.metadata_json or {})
    metadata["close_synthesis_run_id"] = str(getattr(task, "id", "") or "")
    metadata["close_synthesis_status"] = terminal_status
    metadata["close_synthesis_summary"] = str(result_summary or "")
    if succeeded:
        team.status = "closed"
        team.closed_at = now
        metadata.pop("close_failure", None)
        metadata["closed_at"] = now.isoformat()
        for member in members:
            member.status = "closed"
            member.closed_at = now
        event_type = "team_closed"
        event_content = "Agent Team lead synthesis completed; the Team is closed."
    else:
        team.status = "active"
        team.closed_at = None
        metadata["close_failure"] = str(result_summary or terminal_status)
        metadata["close_failed_at"] = now.isoformat()
        event_type = "team_close_failed"
        event_content = "Agent Team lead synthesis failed; the Team is available for retry."
    team.metadata_json = metadata
    event_payload = {
        "status": team.status,
        "terminal_status": terminal_status,
        "synthesis_run_id": str(getattr(task, "id", "") or ""),
        "result_summary": str(result_summary or ""),
    }
    db.add(AgentTeamEvent(team_id=team.id, event_type=event_type, payload_json=event_payload))
    await append_session_event(
        db=db,
        agent_id=team.lead_agent_id,
        tenant_id=team.tenant_id,
        session_id=team.parent_session_id,
        run_id=getattr(task, "id", None),
        actor_type="system",
        event_type=event_type,
        role="system",
        user_id=_uuid_or_none(task_metadata.get("user_id")),
        content=event_content,
        source="agent_team_close",
        materialize_chat_message=False,
        metadata={
            "source": "agent_team_close",
            "team_id": str(team.id),
            **event_payload,
        },
    )
    if succeeded:
        await emit_hook(
            HookEvent.TEAM_CLOSED,
            evidence_db=db,
            agent_id=team.lead_agent_id,
            session_id=str(team.parent_session_id),
            source="agent_team",
            metadata={
                "tenant_id": str(team.tenant_id) if team.tenant_id else None,
                "team_id": str(team.id),
                "synthesis_run_id": str(getattr(task, "id", "") or ""),
            },
        )
    await db.flush()
    return {"team_id": str(team.id), "status": team.status}


async def reopen_agent_team_close_after_delivery_failure(
    *,
    db: Any,
    team_id: uuid.UUID,
    notification_id: uuid.UUID,
    error: str,
) -> bool:
    """Release a Team stuck in closing when its lead-synthesis wake dead-letters."""

    team = (
        await db.execute(select(AgentTeam).where(AgentTeam.id == team_id).limit(1).with_for_update())
    ).scalar_one_or_none()
    if team is None or team.status != "closing":
        return False
    metadata = dict(team.metadata_json or {})
    if str(metadata.get("close_notification_id") or "") != str(notification_id):
        return False
    now = datetime.now(timezone.utc)
    team.status = "active"
    team.closed_at = None
    metadata.update(
        {
            "close_synthesis_status": "delivery_failed",
            "close_failure": str(error or "Lead synthesis delivery failed."),
            "close_failed_at": now.isoformat(),
        }
    )
    team.metadata_json = metadata
    db.add(
        AgentTeamEvent(
            team_id=team.id,
            event_type="team_close_delivery_failed",
            payload_json={
                "notification_id": str(notification_id),
                "error": metadata["close_failure"],
                "retryable": True,
            },
        )
    )
    await db.flush()
    return True


async def message_agent_team_members_runtime(
    *,
    db: Any,
    agent: Any,
    user: Any,
    team: AgentTeam,
    members: list[AgentTeamMember],
    member_sessions: list[ChatSession],
    message: str,
    display_content: str = "",
    interrupt_requested: bool = False,
    source: str = "agent_team",
    budget_run_id: uuid.UUID | str | None = None,
    budget_service: RuntimeBudgetService | None = None,
    root_runtime_task_id: uuid.UUID | str | None = None,
    operation_id: str | None = None,
    reserve_new_team_sessions: bool = False,
    fanout_ordinal_overrides: dict[uuid.UUID, int] | None = None,
) -> dict[str, Any]:
    message_text = str(message or "").strip()
    if not message_text:
        raise ValueError("message is required")
    if not members:
        raise ValueError("No matching team members")

    operation, root_id = _team_fanout_operation_identity(
        team_id=team.id,
        operation_id=operation_id,
        root_runtime_task_id=root_runtime_task_id,
    )
    budget_uuid = _uuid_or_none(budget_run_id)
    work_items = _team_fanout_work_items(
        team=team,
        members=members,
        operation_id=operation,
        root_runtime_task_id=root_id,
        message=message_text,
        ordinal_overrides=fanout_ordinal_overrides,
    )
    await _register_team_fanout_requested_set(
        db=db,
        agent=agent,
        user=user,
        team=team,
        operation_id=operation,
        root_runtime_task_id=root_id,
        message=message_text,
        work_items=work_items,
        source=source,
        display_content=display_content,
        interrupt_requested=interrupt_requested,
        budget_run_id=budget_uuid,
        reserve_new_team_sessions=reserve_new_team_sessions,
    )

    sessions_by_id = {str(session.id): session for session in member_sessions}
    admission = ExecutionAdmission(budget_service) if budget_uuid is not None else None
    results: list[dict[str, Any]] = []
    for item in work_items:
        member = item["member"]
        session = sessions_by_id.get(str(member.chat_session_id))
        if session is None:
            await transition_runtime_root_item(
                db,
                root_runtime_task_id=root_id,
                intent_key=item["intent_key"],
                requested_state="not_admitted",
                reason_code="team_member_session_not_found",
            )
            results.append(
                {
                    "member_id": str(member.id),
                    "member_name": member.member_name,
                    "child_session_id": str(member.chat_session_id),
                    "status": "rejected",
                    "reason": "team member session not found",
                }
            )
            continue

        # A live input joins an already-admitted turn and is not a new child
        # execution. Avoid charging/pausing work-amplification budget for it.
        active_run = None
        if budget_uuid is not None:
            from app.services.web_chat_runtime import _find_active_run

            active_run = await _find_active_run(db, agent_id=agent.id, session_id=session.id)

        admission_decision = None
        if admission is not None and budget_uuid is not None and active_run is None:
            try:
                admission_decision = await admission.admit(
                    RuntimeBudgetReservation(
                        budget_run_id=budget_uuid,
                        reservation_key=item["reservation_key"],
                        team_sessions=1 if reserve_new_team_sessions else 0,
                        background_tasks=1,
                        continuation_wakes=1,
                        reason="agent_team_member_spawn" if reserve_new_team_sessions else "agent_team_member_turn",
                        runtime_task_id=item["run_id"],
                        metadata={
                            "work_type": "agent_team_member",
                            "root_runtime_task_id": str(root_id),
                            "root_item_intent_key": item["intent_key"],
                            "operation_id": operation,
                            "ordinal": item["ordinal"],
                            "team_id": str(team.id),
                            "team_name": team.name,
                            "member_id": str(member.id),
                            "member_name": member.member_name,
                            "child_session_id": str(member.chat_session_id),
                            "parent_session_id": str(team.parent_session_id),
                            "message_sha256": item["message_sha256"],
                        },
                    )
                )
            except RuntimeBudgetDenied as exc:
                await transition_runtime_root_item(
                    db,
                    root_runtime_task_id=root_id,
                    intent_key=item["intent_key"],
                    requested_state="not_admitted",
                    reason_code="runtime_budget_denied",
                    metadata={"budget_error": str(exc)},
                )
                results.append(
                    {
                        "member_id": str(member.id),
                        "member_name": member.member_name,
                        "child_session_id": str(member.chat_session_id),
                        "status": "rejected",
                        "reason": str(exc),
                        "retryable": False,
                    }
                )
                continue

        waiting = bool(admission_decision is not None and admission_decision.waiting)
        root_intent = RuntimeRootIntentSpec(
            intent_key=item["intent_key"],
            work_type="team_member",
            target_ref=item["target_ref"],
            path=(f"agent:{agent.id}", f"team:{team.id}"),
            state="waiting_approval" if waiting else "queued",
            admission_disposition="deferred" if waiting else "admitted",
            reason_code="runtime_budget_approval_required" if waiting else None,
            approval_ref=(
                f"runtime-budget://{budget_uuid}/reservation/{item['reservation_key']}"
                if waiting and budget_uuid is not None
                else None
            ),
            budget_reservation_key=item["reservation_key"] if budget_uuid is not None else None,
            metadata={
                "operation_id": operation,
                "ordinal": item["ordinal"],
                "message_sha256": item["message_sha256"],
            },
        )
        try:
            run = await continue_agent_session_from_mailbox(
                db=db,
                agent=agent,
                user=user,
                session=session,
                message=message_text,
                display_content=display_content,
                interrupt_requested=interrupt_requested,
                parent_session_id=team.parent_session_id,
                runtime_task_type="team_member",
                extra_metadata={
                    "budget_run_id": str(budget_uuid) if budget_uuid else None,
                    "root_runtime_task_id": str(root_id),
                    "team_operation_id": operation,
                    "team_root_item_intent_key": item["intent_key"],
                    **(
                        {
                            "runtime_model_id": str(member.model_id),
                            "runtime_model_source": "agent_team_member",
                        }
                        if getattr(member, "model_id", None)
                        else {}
                    ),
                },
                run_id=None if active_run is not None else item["run_id"],
                root_item_intent=None if active_run is not None else root_intent,
                budget_admission_status_override=(
                    "waiting_budget_approval"
                    if waiting
                    else "approved"
                    if budget_uuid is not None and active_run is None
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - other members remain independently recoverable.
            root_item, _ = await transition_runtime_root_item(
                db,
                root_runtime_task_id=root_id,
                intent_key=item["intent_key"],
                requested_state="requested",
                reason_code="team_member_admission_interrupted",
                metadata={
                    "retryable": True,
                    "error_class": type(exc).__name__,
                    "error": str(exc),
                },
            )
            if root_item is not None:
                root_item.recovery_claimed_by = None
                root_item.recovery_claim_expires_at = None
                root_item.next_recovery_at = datetime.now(timezone.utc)
            if admission is not None and admission_decision is not None:
                await admission.settle(
                    admission_decision,
                    reason="agent_team_member_admission_interrupted",
                    runtime_task_id=item["run_id"],
                )
            results.append(
                {
                    "member_id": str(member.id),
                    "member_name": member.member_name,
                    "child_session_id": str(member.chat_session_id),
                    "status": "deferred",
                    "reason": "team_member_admission_interrupted",
                    "retryable": True,
                    "error_class": type(exc).__name__,
                }
            )
            continue

        status = str(run.get("status") or "queued")
        if status in {"queued", "started", "running"}:
            _stamp_member_runtime(member, run, status=status)
            event_type = "member_message_queued"
        elif status == "waiting_budget_approval":
            _stamp_member_runtime(member, run, status=status)
            member.status = "waiting"
            event_type = "member_message_waiting_budget_approval"
        else:
            member.status = "blocked" if status == "rejected" else member.status
            event_type = "member_message_rejected"
            if status == "rejected":
                await transition_runtime_root_item(
                    db,
                    root_runtime_task_id=root_id,
                    intent_key=item["intent_key"],
                    requested_state="not_admitted",
                    reason_code=str(run.get("reason") or "team_member_runtime_rejected"),
                )

        if str(run.get("consumer") or "") == "session_v2_round_input":
            receipt = dict(run.get("session_input_receipt") or {})
            input_id = str(receipt.get("input_id") or "").strip()
            await transition_runtime_root_item(
                db,
                root_runtime_task_id=root_id,
                intent_key=item["intent_key"],
                requested_state="completed",
                reason_code="delivered_to_active_team_turn",
                result_refs=((f"session-input://{input_id}",) if input_id else ()),
            )

        if admission is not None and admission_decision is not None:
            run_started = status in {"queued", "started", "running"}
            await admission.settle(
                admission_decision,
                actual_team_sessions=1 if reserve_new_team_sessions else 0,
                actual_background_tasks=1 if run_started else 0,
                actual_continuation_wakes=1 if run_started else 0,
                reason="agent_team_member_admitted" if run_started else "agent_team_member_waiting",
                runtime_task_id=item["run_id"],
                metadata={
                    "root_runtime_task_id": str(root_id),
                    "root_item_intent_key": item["intent_key"],
                    "team_id": str(team.id),
                    "member_id": str(member.id),
                },
            )
        db.add(
            AgentTeamEvent(
                id=uuid.uuid4(),
                team_id=team.id,
                receiver_member_id=member.id,
                event_type=event_type,
                payload_json={
                    "status": status,
                    "runtime_task_type": "team_member",
                    "run_id": run.get("run_id"),
                    "consumer": run.get("consumer"),
                    "reason": run.get("reason"),
                    "message_preview": message_text[:240],
                    "source": source,
                    "root_runtime_task_id": str(root_id),
                    "root_item_intent_key": item["intent_key"],
                    "operation_id": operation,
                },
            )
        )
        results.append(
            {
                "member_id": str(member.id),
                "member_name": member.member_name,
                "child_session_id": str(member.chat_session_id),
                "status": status,
                "consumer": run.get("consumer"),
                "run_id": run.get("run_id"),
                "reason": run.get("reason"),
                "approval_ref": root_intent.approval_ref,
                "root_item_intent_key": item["intent_key"],
            }
        )

    await db.flush()
    if hasattr(db, "commit"):
        await db.commit()
    coverage = await read_runtime_root_coverage(db, root_runtime_task_id=root_id)
    return {
        "ok": all(result.get("status") != "rejected" for result in results),
        "team_id": str(team.id),
        "team_name": team.name,
        "operation_id": operation,
        "root_runtime_task_id": str(root_id),
        "coverage": coverage.to_dict(),
        "message_count": len(results),
        "interrupt_requested": interrupt_requested,
        "results": results,
    }


async def create_agent_team_from_tool_request(
    request: Any,
    *,
    name: str,
    members: list[dict[str, Any]] | None = None,
    description: str = "",
    agent_type: str = "",
) -> dict[str, Any]:
    session_id = _uuid_or_none(getattr(request.context, "session_id", None))
    if session_id is None:
        raise ValueError("team_create requires the current session_id")

    tenant_id = _uuid_or_none(getattr(request.context, "tenant_id", None))
    if tenant_id is None:
        tenant_id = await resolve_tenant_for_agent(request.context.agent_id)
    if tenant_id is None:
        raise ValueError("Agent tenant not found")

    async with tenant_scoped_session(
        tenant_id,
        require_tenant=True,
        source="agent_team_create_tool_runtime",
    ) as db:
        agent = (
            await db.execute(
                select(Agent).where(
                    Agent.id == request.context.agent_id,
                    Agent.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise ValueError("Agent not found")
        user = (await db.execute(select(User).where(User.id == request.context.user_id))).scalar_one_or_none()
        if user is None:
            raise ValueError("User not found")
        parent_session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.agent_id == request.context.agent_id,
                )
            )
        ).scalar_one_or_none()
        if parent_session is None:
            raise ValueError("Parent session not found")
        if members:
            raise ValueError(
                "team_create creates the Team container only; spawn teammates with spawn_subagent team_name + name"
            )
        return await create_agent_team_runtime(
            db=db,
            agent=agent,
            user=user,
            parent_session=parent_session,
            name=name,
            members=[],
            source="team_create_tool",
            command="team_create",
            metadata={
                "description": str(description or "").strip(),
                "lead_agent_type": str(agent_type or "").strip(),
                "team_create_semantics": "container_only",
            },
        )


async def active_agent_team_contract_from_tool_request(request: Any) -> dict[str, Any] | None:
    """Return the active session Team contract that forbids silent one-shot downgrades.

    ``team_create`` creates a durable session-local container. Once it exists,
    plain ``spawn_subagent`` would bypass the requested Agent Team unless the
    caller supplies ``team_name + name`` and enters the teammate branch.
    """
    session_id = _uuid_or_none(getattr(request.context, "session_id", None))
    if session_id is None:
        return None
    tenant_id = _uuid_or_none(getattr(request.context, "tenant_id", None)) or await resolve_tenant_for_agent(
        request.context.agent_id
    )
    async with tenant_scoped_session(tenant_id) as db:
        team = (
            await db.execute(
                select(AgentTeam)
                .where(
                    AgentTeam.lead_agent_id == request.context.agent_id,
                    AgentTeam.parent_session_id == session_id,
                    AgentTeam.status == "active",
                )
                .order_by(AgentTeam.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if team is None:
            return None
        return {
            "type": "agent_team",
            "source": "active_session_team",
            "team_id": str(team.id),
            "name": team.name,
            "team_name": team.name,
            **teammate_creation_discovery(team.name),
        }


async def spawn_agent_team_member_from_tool_request(
    request: Any,
    *,
    team_name: str,
    member_name: str,
    prompt: str,
    description: str = "",
    subagent_type: str = "",
    model: str = "",
    mode: str = "",
) -> dict[str, Any]:
    session_id = _uuid_or_none(getattr(request.context, "session_id", None))
    if session_id is None:
        raise ValueError("AgentTool teammate spawn requires the current session_id")
    team_name = str(team_name or "").strip()
    member_name = str(member_name or "").strip()
    if not team_name:
        raise ValueError("team_name is required for teammate spawn")
    if not member_name:
        raise ValueError("name is required for teammate spawn")

    tenant_id = _uuid_or_none(getattr(request.context, "tenant_id", None))
    if tenant_id is None:
        tenant_id = await resolve_tenant_for_agent(request.context.agent_id)
    if tenant_id is None:
        raise ValueError("Agent tenant not found")

    async with tenant_scoped_session(
        tenant_id,
        require_tenant=True,
        source="agent_team_member_spawn_tool_runtime",
    ) as db:
        agent = (
            await db.execute(
                select(Agent).where(
                    Agent.id == request.context.agent_id,
                    Agent.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise ValueError("Agent not found")
        user = (await db.execute(select(User).where(User.id == request.context.user_id))).scalar_one_or_none()
        parent_session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.agent_id == request.context.agent_id,
                )
            )
        ).scalar_one_or_none()
        team = (
            await db.execute(
                select(AgentTeam).where(
                    AgentTeam.lead_agent_id == request.context.agent_id,
                    AgentTeam.parent_session_id == session_id,
                    AgentTeam.name == team_name,
                    AgentTeam.status == "active",
                )
            )
        ).scalar_one_or_none()
        if user is None:
            raise ValueError("User not found")
        if parent_session is None:
            raise ValueError("Parent session not found")
        if team is None:
            raise ValueError(f"Team {team_name!r} not found in the current session; call team_create first")
        return await spawn_agent_team_member_runtime(
            db=db,
            agent=agent,
            user=user,
            parent_session=parent_session,
            team=team,
            spec=TeamMemberCreateSpec(
                name=member_name,
                role=str(description or "").strip(),
                metadata={
                    "agent_type": str(subagent_type or "").strip(),
                    "model": str(model or "").strip(),
                },
            ),
            prompt=prompt,
            source="agent_tool_teammate_spawn",
            mode=mode,
            budget_run_id=getattr(request.context, "budget_run_id", None),
            root_runtime_task_id=(
                getattr(request.context, "authority_root_runtime_task_id", None)
                or getattr(request.context, "runtime_task_id", None)
            ),
            operation_id=str(
                (
                    (getattr(request.context, "tool_execution_frames", None) or [{}])[-1].get("tool_call_id")
                    if isinstance((getattr(request.context, "tool_execution_frames", None) or [{}])[-1], dict)
                    else ""
                )
                or ""
            ).strip()
            or None,
        )


async def send_agent_team_message_from_tool_request(request: Any) -> dict[str, Any]:
    team_id = _uuid_or_none(getattr(request, "arguments", {}).get("team_id"))
    team_name = str(getattr(request, "arguments", {}).get("team_name") or "").strip()
    member_name = str(
        getattr(request, "arguments", {}).get("member_name") or getattr(request, "arguments", {}).get("to") or ""
    ).strip()
    message = str(getattr(request, "arguments", {}).get("message") or "").strip()
    if not member_name:
        raise ValueError("member_name/to is required; use '*' to broadcast")
    if not message:
        raise ValueError("message is required")

    tenant_id = _uuid_or_none(getattr(request.context, "tenant_id", None)) or await resolve_tenant_for_agent(
        request.context.agent_id
    )
    async with tenant_scoped_session(tenant_id) as db:
        agent = (await db.execute(select(Agent).where(Agent.id == request.context.agent_id))).scalar_one_or_none()
        user = (await db.execute(select(User).where(User.id == request.context.user_id))).scalar_one_or_none()
        team_stmt = select(AgentTeam).where(AgentTeam.lead_agent_id == request.context.agent_id)
        if team_id is not None:
            team_stmt = team_stmt.where(AgentTeam.id == team_id)
        else:
            session_id = _uuid_or_none(getattr(request.context, "session_id", None))
            team_stmt = team_stmt.where(AgentTeam.status == "active")
            if session_id is not None:
                team_stmt = team_stmt.where(AgentTeam.parent_session_id == session_id)
            if team_name:
                team_stmt = team_stmt.where(AgentTeam.name == team_name)
            team_stmt = team_stmt.order_by(AgentTeam.created_at.desc()).limit(1)
        team = (await db.execute(team_stmt)).scalar_one_or_none()
        if agent is None or user is None:
            raise ValueError("Agent Team continuation principal could not be loaded")
        if team is None:
            raise ValueError("Agent Team not found for this agent")
        if team.status == "closed":
            raise ValueError("Agent Team is closed")

        member_result = await db.execute(
            select(AgentTeamMember).where(AgentTeamMember.team_id == team.id).order_by(AgentTeamMember.created_at.asc())
        )
        all_members = list(member_result.scalars().all())
        if member_name == "*":
            members = [member for member in all_members if member.status != "closed"]
        else:
            members = [
                member
                for member in all_members
                if member.status != "closed" and member.member_name.lower() == member_name.lower()
            ]
        if not members:
            raise ValueError("No matching active team members")

        session_ids = [member.chat_session_id for member in members]
        session_result = await db.execute(
            select(ChatSession).where(
                ChatSession.id.in_(session_ids),
                ChatSession.agent_id == request.context.agent_id,
            )
        )
        member_sessions = list(session_result.scalars().all())
        payload = await message_agent_team_members_runtime(
            db=db,
            agent=agent,
            user=user,
            team=team,
            members=members,
            member_sessions=member_sessions,
            message=message,
            display_content=str(getattr(request, "arguments", {}).get("display_content") or ""),
            interrupt_requested=bool(getattr(request, "arguments", {}).get("interrupt")),
            source="send_agent_session_message",
            budget_run_id=getattr(request.context, "budget_run_id", None),
            root_runtime_task_id=(
                getattr(request.context, "authority_root_runtime_task_id", None)
                or getattr(request.context, "runtime_task_id", None)
            ),
            operation_id=str(
                (
                    (getattr(request.context, "tool_execution_frames", None) or [{}])[-1].get("tool_call_id")
                    if isinstance((getattr(request.context, "tool_execution_frames", None) or [{}])[-1], dict)
                    else ""
                )
                or ""
            ).strip()
            or None,
        )
        payload["member_name"] = member_name
        return payload
