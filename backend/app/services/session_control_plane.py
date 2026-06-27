"""CCPlus session workbench and JSON export aggregation."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.agent_team import AgentTeam, AgentTeamMember
from app.models.audit import ApprovalRequest
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.runtime.ccplus_contracts import (
    AgentSessionV1,
    SessionEdgeV1,
    SessionGraphV1,
    SessionNodeV1,
    TurnStateV1,
    TurnStatus,
    build_context_policy,
    build_permission_profile,
)
from app.services.session_command_runtime import _checkpoint_payloads, _event_payload, _load_events
from app.services.enterprise_approval_visibility import is_visible_enterprise_approval
from app.services.session_index import read_session_index
from app.services.web_chat_runtime import get_active_web_chat_run


def _iso(value: Any) -> str | None:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value else None


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _session_metadata(session: ChatSession) -> dict[str, Any]:
    return _mapping(getattr(session, "transcript_metadata_json", None))


def _active_run_value(active_run: Any, key: str) -> Any:
    if isinstance(active_run, dict):
        return active_run.get(key)
    return getattr(active_run, key, None)


def _active_run_metadata(active_run: Any) -> dict[str, Any]:
    if active_run is None:
        return {}
    metadata = _active_run_value(active_run, "metadata")
    if metadata is None:
        metadata = _active_run_value(active_run, "metadata_json")
    return _mapping(metadata)


def _merged_runtime_policy(
    *,
    active_run: Any,
    session: ChatSession,
    key: str,
) -> dict[str, Any]:
    active_metadata = _active_run_metadata(active_run)
    session_metadata = _session_metadata(session)
    return _mapping(active_metadata.get(key)) or _mapping(session_metadata.get(key))


def _permission_profile_payload(*, active_run: Any, session: ChatSession) -> dict[str, Any]:
    raw = _merged_runtime_policy(active_run=active_run, session=session, key="permission_profile")
    profile = _jsonable(build_permission_profile(raw))
    profile["schema"] = "hive.ccplus.permission_profile.v1"
    return profile


def _context_policy_payload(*, active_run: Any, session: ChatSession) -> dict[str, Any]:
    raw = _merged_runtime_policy(active_run=active_run, session=session, key="context_policy")
    model_window = int(raw.get("model_window") or raw.get("context_window_tokens") or 0)
    # Derive through the canonical contract builder so the projection is governed
    # by ContextPolicyV1 (overrides are validated against the contract's fields),
    # not a raw dict .update() that could smuggle arbitrary keys past the contract.
    policy = _jsonable(build_context_policy(model_window, overrides=raw))
    policy["schema"] = "hive.ccplus.context_policy.v1"
    return policy


def _session_payload(session: ChatSession) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "agent_id": str(session.agent_id),
        "tenant_id": str(session.tenant_id) if getattr(session, "tenant_id", None) else None,
        "user_id": str(session.user_id) if getattr(session, "user_id", None) else None,
        "title": session.title,
        "source_channel": session.source_channel,
        "session_kind": getattr(session, "session_kind", None) or "human_chat",
        "actor_type": getattr(session, "actor_type", None) or "user",
        "runtime_source": getattr(session, "runtime_source", None) or "web_chat",
        "visibility_scope": getattr(session, "visibility_scope", None) or "direct_user",
        "listed_surface": getattr(session, "listed_surface", None) or "chat",
        "parent_session_id": str(session.parent_session_id) if getattr(session, "parent_session_id", None) else None,
        "root_session_id": str(session.root_session_id) if getattr(session, "root_session_id", None) else None,
        "runtime_task_id": str(session.runtime_task_id) if getattr(session, "runtime_task_id", None) else None,
        "created_at": _iso(getattr(session, "created_at", None)),
        "last_message_at": _iso(getattr(session, "last_message_at", None)),
    }


def _runtime_task_payload(task: RuntimeTask) -> dict[str, Any]:
    metadata = task.metadata_json or {}
    return {
        "id": str(task.id),
        "task_type": task.task_type,
        "status": task.status,
        "parent_agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else None,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "trace_id": task.trace_id,
        "created_at": _iso(task.created_at),
        "started_at": _iso(task.started_at),
        "completed_at": _iso(task.completed_at),
        "result_summary": task.result_summary,
        "token_usage": task.token_usage or {},
        "metadata": metadata,
        "terminal_reason": metadata.get("terminal_reason"),
    }


def _goal_payload(goal: AgentSessionGoal) -> dict[str, Any]:
    return {
        "id": str(goal.id),
        "agent_id": str(goal.agent_id),
        "session_id": str(goal.chat_session_id),
        "objective": goal.objective,
        "status": goal.status,
        "token_budget": goal.token_budget,
        "tokens_used": goal.tokens_used,
        "time_budget_seconds": goal.time_budget_seconds,
        "continuation_count": goal.continuation_count,
        "max_continuation_turns": goal.max_continuation_turns,
        "blocked_count": goal.blocked_count,
        "completion_summary": goal.completion_summary,
        "created_at": _iso(goal.created_at),
        "updated_at": _iso(goal.updated_at),
        "completed_at": _iso(goal.completed_at),
    }


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
        "summary": metadata.get("summary") or "",
        "t0_refs": metadata.get("t0_refs") or [],
        "artifacts": metadata.get("artifacts") or [],
    }


def _team_payload(team: AgentTeam, members: list[AgentTeamMember]) -> dict[str, Any]:
    return {
        "id": str(team.id),
        "name": team.name,
        "status": team.status,
        "transcript_truth": team.transcript_truth,
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "member_count": len(members),
        "members": [_team_member_payload(member) for member in members],
        "created_at": _iso(team.created_at),
        "closed_at": _iso(team.closed_at),
    }


# Run-lifecycle status strings that are not TurnStatus enum values map here so a
# terminal cancel surfaces as CANCELLED instead of silently collapsing to RUNNING.
_RUN_STATUS_ALIASES: dict[str, TurnStatus] = {
    "killed": TurnStatus.CANCELLED,
    "cancelled": TurnStatus.CANCELLED,
    "canceled": TurnStatus.CANCELLED,
    "skipped": TurnStatus.CANCELLED,
    "queued": TurnStatus.RUNNING,
    "pending": TurnStatus.RUNNING,
}

_PERMISSION_PENDING_EVENT_TYPES = {"permission", "permission_request", "session_permission_required"}
_PERMISSION_RESOLVED_EVENT_TYPES = {"permission_resolved", "session_permission_decision"}
_CHILD_WAIT_EVENT_TYPES = {"child_session", "subagent"}
_WORKFLOW_WAIT_EVENT_TYPES = {"workflow_run", "workflow_step"}
_ACTIVE_PROGRESS_EVENT_TYPES = {"assistant_message", "tool_result", "user_message"}
_CHILD_TERMINAL_STATUSES = {"completed", "failed", "done", "cancelled", "canceled", "killed", "skipped", "error"}


def _coerce_turn_status(raw: Any) -> TurnStatus:
    """Map the live run's raw status string onto the TurnStateV1 status enum.

    Unknown/absent values fall back to RUNNING because the projection only
    surfaces an active turn when there is a live web-chat run in flight. A
    ``killed``/``skipped`` run is a terminal cancel and maps to CANCELLED.
    """
    value = str(raw or "").strip().lower()
    if value in _RUN_STATUS_ALIASES:
        return _RUN_STATUS_ALIASES[value]
    try:
        return TurnStatus(value)
    except ValueError:
        return TurnStatus.RUNNING


def _derive_active_turn_status(events: list[Any], base: TurnStatus) -> TurnStatus:
    """Derive the specific wait-state the active turn is blocked on.

    The run's raw lifecycle status is only ``running``/``pending``/``killed``/etc.,
    so a turn paused on a permission prompt, blocked by a hook, or waiting on a
    child session / workflow would otherwise project as a generic RUNNING. This
    scans recent session events backward for the most recent unresolved "wait
    concern" and surfaces the typed state, so the turn-state machine reflects
    reality instead of collapsing every wait to RUNNING.
    """
    if base in {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED, TurnStatus.INTERRUPTED}:
        return base
    for event in reversed(events[-50:]):
        event_type = str(getattr(event, "event_type", "") or "")
        if not event_type:
            continue
        metadata = getattr(event, "metadata_json", None)
        status = str(metadata.get("status") or "").strip().lower() if isinstance(metadata, dict) else ""
        if event_type in _PERMISSION_RESOLVED_EVENT_TYPES:
            return base
        if event_type in _PERMISSION_PENDING_EVENT_TYPES:
            return TurnStatus.WAITING_FOR_PERMISSION
        if event_type == "hook_blocked":
            return TurnStatus.BLOCKED_BY_HOOK
        if event_type in _CHILD_WAIT_EVENT_TYPES:
            return base if status in _CHILD_TERMINAL_STATUSES else TurnStatus.WAITING_FOR_CHILD
        if event_type in _WORKFLOW_WAIT_EVENT_TYPES:
            return base if status in _CHILD_TERMINAL_STATUSES else TurnStatus.WAITING_FOR_WORKFLOW
        if event_type in _ACTIVE_PROGRESS_EVENT_TYPES:
            return base
    return base


def _build_active_turn_state(
    *, session: ChatSession, active_run: Any, events: list[Any] | None = None
) -> TurnStateV1 | None:
    """Derive a TurnStateV1 from the REAL active run, or None when idle.

    Pulls live status, runtime_task_id, the active tool-call ids, and the
    persisted ``terminal_reason`` straight off the run + its metadata so the
    contract — not an ad-hoc dict — is the source of truth for the active turn.
    """
    if active_run is None:
        return None
    metadata = _active_run_metadata(active_run)
    runtime_task_id = str(_active_run_value(active_run, "id") or metadata.get("runtime_task_id") or "")
    turn_id = str(metadata.get("turn_id") or metadata.get("expected_turn_id") or runtime_task_id or "")
    active_tool_call_ids = tuple(
        str(call_id) for call_id in (metadata.get("active_tool_call_ids") or ()) if call_id is not None
    )
    return TurnStateV1(
        session_id=str(session.id),
        runtime_task_id=runtime_task_id or None,
        turn_id=turn_id or None,
        status=_derive_active_turn_status(
            events or [],
            _coerce_turn_status(_active_run_value(active_run, "status") or metadata.get("status")),
        ),
        terminal_reason=metadata.get("terminal_reason"),
        active_tool_call_ids=active_tool_call_ids,
        pending_steer_messages=tuple(metadata.get("pending_user_messages") or ()),
    )


def _active_turn_payload(*, turn_state: TurnStateV1 | None) -> dict[str, Any] | None:
    """The compatibility-shaped active_turn view (stable 6-key surface)."""
    if turn_state is None:
        return None
    return {
        "session_id": turn_state.session_id,
        "runtime_task_id": turn_state.runtime_task_id,
        "turn_id": turn_state.turn_id,
        "status": _jsonable(turn_state.status),
        "expected_turn_id": turn_state.turn_id,
        "pending_steer_count": len(turn_state.pending_steer_messages),
    }


def _active_turn_state_payload(turn_state: TurnStateV1 | None) -> dict[str, Any] | None:
    """The full TurnStateV1 view incl. terminal_reason + active tool-call ids."""
    if turn_state is None:
        return None
    return {
        "schema": "hive.ccplus.turn_state.v1",
        "session_id": turn_state.session_id,
        "runtime_task_id": turn_state.runtime_task_id,
        "turn_id": turn_state.turn_id,
        "status": _jsonable(turn_state.status),
        "terminal_reason": _jsonable(turn_state.terminal_reason),
        "active_tool_call_ids": list(turn_state.active_tool_call_ids),
        "pending_steer_count": len(turn_state.pending_steer_messages),
    }


def _agent_session_payload(
    *,
    session: ChatSession,
    runtime_tasks: list[RuntimeTask],
    turn_state: TurnStateV1 | None,
) -> dict[str, Any]:
    """Derive an AgentSessionV1 view from the REAL session row + loaded tasks."""
    agent_session = AgentSessionV1(
        session_id=str(session.id),
        root_session_id=str(session.root_session_id) if getattr(session, "root_session_id", None) else None,
        parent_session_id=str(session.parent_session_id) if getattr(session, "parent_session_id", None) else None,
        session_kind=getattr(session, "session_kind", None) or "human_chat",
        actor_type=getattr(session, "actor_type", None) or "user",
        source=getattr(session, "source_channel", None) or getattr(session, "runtime_source", None) or "web",
        active_turn=turn_state,
        runtime_task_refs=tuple(str(task.id) for task in runtime_tasks),
    )
    return {
        "schema": "hive.ccplus.agent_session.v1",
        "session_id": agent_session.session_id,
        "root_session_id": agent_session.root_session_id,
        "parent_session_id": agent_session.parent_session_id,
        "session_kind": agent_session.session_kind,
        "actor_type": agent_session.actor_type,
        "source": agent_session.source,
        "active_turn": _active_turn_state_payload(agent_session.active_turn),
        "runtime_task_refs": list(agent_session.runtime_task_refs),
    }


_TASK_TYPE_TO_RELATION = {
    "team_member": "team_member",
    "workflow": "workflow_leaf",
    "delegation": "delegated_to",
    "subagent": "delegated_to",
}


def _session_graph_payload(
    *,
    session: ChatSession,
    runtime_tasks: list[RuntimeTask],
    teams: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a SessionGraphV1 from the REAL parent/child/team/workflow rows.

    Nodes are the focus session plus every distinct child session reachable
    through a loaded RuntimeTask child or team member. Edges classify each
    real relationship by ``task_type`` (parent_child / delegated_to /
    team_member / workflow_leaf) so the workbench can render the live topology
    rather than an empty default.
    """
    root_session_id = str(session.root_session_id) if getattr(session, "root_session_id", None) else str(session.id)
    focus_session_id = str(session.id)

    nodes: dict[str, SessionNodeV1] = {
        focus_session_id: SessionNodeV1(
            session_id=focus_session_id,
            actor_type=getattr(session, "actor_type", None) or "user",
            session_kind=getattr(session, "session_kind", None) or "human_chat",
            source=getattr(session, "source_channel", None) or "web",
            parent_session_id=str(session.parent_session_id) if getattr(session, "parent_session_id", None) else None,
            root_session_id=root_session_id,
            agent_id=str(session.agent_id) if getattr(session, "agent_id", None) else None,
        )
    }
    edges: list[SessionEdgeV1] = []

    for task in runtime_tasks:
        parent_sid = str(task.parent_session_id) if task.parent_session_id else None
        child_sid = str(task.child_session_id) if task.child_session_id else None
        relation = _TASK_TYPE_TO_RELATION.get(task.task_type, "parent_child")
        from_id = parent_sid or focus_session_id
        # Same-session continuation tasks (web_chat_turn/goal_continuation) carry
        # no child session; they are still real parent_child edges on the focus node.
        to_id = child_sid or parent_sid or focus_session_id
        if child_sid and child_sid not in nodes:
            nodes[child_sid] = SessionNodeV1(
                session_id=child_sid,
                actor_type="agent",
                session_kind=task.task_type,
                source="agent",
                runtime_task_id=str(task.id),
                parent_session_id=parent_sid or focus_session_id,
                root_session_id=root_session_id,
                agent_id=str(task.child_agent_id) if task.child_agent_id else None,
                status=task.status,
            )
        edges.append(
            SessionEdgeV1(
                relation=relation,
                from_id=from_id,
                to_id=to_id,
                runtime_task_id=str(task.id),
                task_type=task.task_type,
            )
        )

    for team in teams:
        for member in team.get("members", []):
            member_sid = str(member.get("chat_session_id") or "")
            if not member_sid:
                continue
            if member_sid not in nodes:
                nodes[member_sid] = SessionNodeV1(
                    session_id=member_sid,
                    actor_type="agent",
                    session_kind="team_member",
                    source="agent",
                    runtime_task_id=member.get("runtime_task_id"),
                    parent_session_id=focus_session_id,
                    root_session_id=root_session_id,
                    status=member.get("status"),
                )
            edges.append(
                SessionEdgeV1(
                    relation="team_member",
                    from_id=focus_session_id,
                    to_id=member_sid,
                    runtime_task_id=member.get("runtime_task_id"),
                    task_type=member.get("runtime_task_type") or "team_member",
                )
            )

    graph = SessionGraphV1(
        root_session_id=root_session_id,
        nodes=tuple(nodes.values()),
        edges=tuple(edges),
        transcript_refs_by_node={
            node.session_id: node.transcript_refs for node in nodes.values() if node.transcript_refs
        },
    )
    payload = _jsonable(graph)
    payload["schema"] = "hive.ccplus.session_graph.v1"
    return payload


def _timeline_payload(
    *,
    events: list[Any],
    truth_source: str,
    limit: int,
) -> dict[str, Any]:
    event_payloads = [_event_payload(event) for event in events]
    return {
        "schema": "hive.ccplus.session_timeline.v1",
        "truth_source": truth_source,
        "event_count": len(event_payloads),
        "window_limit": limit,
        "truncated": len(event_payloads) >= limit,
        "events": event_payloads,
    }


def _event_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _mapping(payload.get("metadata"))


def _tool_call_payloads(events: list[Any]) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []
    for event in events:
        payload = _event_payload(event)
        metadata = _event_metadata_from_payload(payload)
        event_type = str(payload.get("event_type") or "")
        role = str(payload.get("role") or "")
        tool_name = metadata.get("tool_name") or metadata.get("name")
        if event_type in {"tool_call", "tool_result"} or role == "tool_call" or tool_name:
            tool_calls.append(
                {
                    "event_id": payload.get("id"),
                    "sequence": payload.get("sequence"),
                    "event_type": event_type,
                    "tool_name": tool_name,
                    "status": metadata.get("status") or metadata.get("tool_status"),
                    "invocation_span_id": metadata.get("invocation_span_id"),
                    "created_at": payload.get("created_at"),
                }
            )
    return tool_calls


def _hook_payloads(events: list[Any]) -> list[dict[str, Any]]:
    hooks: list[dict[str, Any]] = []
    for event in events:
        payload = _event_payload(event)
        metadata = _event_metadata_from_payload(payload)
        event_type = str(payload.get("event_type") or "")
        hook_event = metadata.get("hook_event") or metadata.get("hook")
        if event_type.startswith("hook") or hook_event:
            hooks.append(
                {
                    "event_id": payload.get("id"),
                    "sequence": payload.get("sequence"),
                    "event": hook_event or event_type,
                    "status": metadata.get("status"),
                    "reason": metadata.get("reason"),
                    "created_at": payload.get("created_at"),
                }
            )
    return hooks


def _compaction_payloads(events: list[Any]) -> list[dict[str, Any]]:
    compactions: list[dict[str, Any]] = []
    for event in events:
        payload = _event_payload(event)
        metadata = _event_metadata_from_payload(payload)
        event_type = str(payload.get("event_type") or "")
        if event_type in {
            "context_window_status",
            "compaction_skipped",
            "compaction_started",
            "compaction_completed",
            "tool_result_budget_pass",
        }:
            continue
        if "compact" in event_type or metadata.get("kind") == "compaction":
            compactions.append(
                {
                    "event_id": payload.get("id"),
                    "sequence": payload.get("sequence"),
                    "event_type": event_type,
                    "reason": metadata.get("reason"),
                    "summary_ref": metadata.get("summary_ref") or metadata.get("summary_path"),
                    "created_at": payload.get("created_at"),
                }
            )
    return compactions


def _context_window_payload(events: list[Any]) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    latest_status: dict[str, Any] | None = None
    latest_skipped: dict[str, Any] | None = None
    latest_tool_result_budget: dict[str, Any] | None = None
    for event in events:
        payload = _event_payload(event)
        metadata = {**payload, **_event_metadata_from_payload(payload)}
        event_type = str(payload.get("event_type") or "")
        if event_type not in {
            "context_window_status",
            "compaction_skipped",
            "compaction_started",
            "compaction_completed",
            "tool_result_budget_pass",
        }:
            continue
        item = {
            "event_id": payload.get("id"),
            "sequence": payload.get("sequence"),
            "event_type": event_type,
            "reason": metadata.get("reason"),
            "created_at": payload.get("created_at"),
            "active_context_tokens": metadata.get("active_context_tokens"),
            "auto_compact_scope_tokens": metadata.get("auto_compact_scope_tokens"),
            "auto_compact_scope_limit": metadata.get("auto_compact_scope_limit"),
            "tokens_until_compaction": metadata.get("tokens_until_compaction"),
            "full_context_window_limit": metadata.get("full_context_window_limit"),
            "cumulative_run_tokens": metadata.get("cumulative_run_tokens"),
            "trimmed_count": metadata.get("trimmed_count"),
            "changed": metadata.get("changed"),
        }
        decisions.append(item)
        if event_type == "context_window_status":
            latest_status = item
        elif event_type == "compaction_skipped":
            latest_skipped = item
        elif event_type == "tool_result_budget_pass":
            latest_tool_result_budget = item

    return {
        "schema": "hive.ccplus.context_window.v1",
        "decision_count": len(decisions),
        "latest_status": latest_status,
        "latest_skipped": latest_skipped,
        "latest_tool_result_budget": latest_tool_result_budget,
        "decisions": decisions[-20:],
    }


def _approval_payload(approval: ApprovalRequest) -> dict[str, Any]:
    return {
        "id": str(approval.id),
        "agent_id": str(approval.agent_id),
        "tenant_id": str(approval.tenant_id) if approval.tenant_id else None,
        "action_type": approval.action_type,
        "status": approval.status,
        "details": approval.details or {},
        "created_at": _iso(approval.created_at),
        "resolved_at": _iso(approval.resolved_at),
        "resolved_by": str(approval.resolved_by) if approval.resolved_by else None,
    }


def _branch_payload(session: ChatSession) -> dict[str, Any]:
    metadata = _session_metadata(session)
    return {
        "id": str(session.id),
        "title": session.title,
        "parent_session_id": str(session.parent_session_id) if session.parent_session_id else None,
        "root_session_id": str(session.root_session_id) if session.root_session_id else None,
        "branch_mode": metadata.get("branch_mode"),
        "anchor_event_id": metadata.get("anchor_event_id"),
        "created_at": _iso(session.created_at),
        "last_message_at": _iso(session.last_message_at),
    }


async def _list_runtime_tasks(db: AsyncSession, *, agent_id: Any, session_id: Any, limit: int = 50) -> list[RuntimeTask]:
    session_key = str(session_id)
    result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.parent_agent_id == agent_id,
            or_(
                RuntimeTask.parent_session_id == session_key,
                RuntimeTask.child_session_id == session_key,
            ),
        )
        .order_by(RuntimeTask.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _list_goals(db: AsyncSession, *, agent_id: Any, session_id: Any) -> list[AgentSessionGoal]:
    result = await db.execute(
        select(AgentSessionGoal)
        .where(AgentSessionGoal.agent_id == agent_id, AgentSessionGoal.chat_session_id == session_id)
        .order_by(AgentSessionGoal.created_at.desc())
    )
    return list(result.scalars().all())


async def _list_teams(db: AsyncSession, *, agent_id: Any, session_id: Any) -> list[dict[str, Any]]:
    result = await db.execute(
        select(AgentTeam)
        .where(AgentTeam.lead_agent_id == agent_id, AgentTeam.parent_session_id == session_id)
        .order_by(AgentTeam.created_at.desc())
    )
    teams = list(result.scalars().all())
    payloads: list[dict[str, Any]] = []
    for team in teams:
        members_result = await db.execute(
            select(AgentTeamMember).where(AgentTeamMember.team_id == team.id).order_by(AgentTeamMember.created_at.asc())
        )
        members = list(members_result.scalars().all())
        payloads.append(_team_payload(team, members))
    return payloads


async def _list_pending_approvals(
    db: AsyncSession,
    *,
    agent_id: Any,
    session_id: Any,
    tenant_id: Any | None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.agent_id == agent_id,
            ApprovalRequest.status == "pending",
        )
        .order_by(ApprovalRequest.created_at.desc())
        .limit(limit)
    )
    approvals = []
    for approval in result.scalars().all():
        if not is_visible_enterprise_approval(approval):
            continue
        payload = _approval_payload(approval)
        details = _mapping(payload.get("details"))
        approval_session = details.get("session_id") or details.get("parent_session_id")
        if approval_session and str(approval_session) != str(session_id):
            continue
        if tenant_id and payload.get("tenant_id") and str(payload["tenant_id"]) != str(tenant_id):
            continue
        approvals.append(payload)
    return approvals


async def _list_branches(db: AsyncSession, *, agent_id: Any, session_id: Any, limit: int = 50) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.agent_id == agent_id, ChatSession.parent_session_id == session_id)
        .order_by(ChatSession.created_at.desc())
        .limit(limit)
    )
    return [_branch_payload(branch) for branch in result.scalars().all()]


async def build_session_workbench(db: AsyncSession, *, agent: Agent, session: ChatSession) -> dict[str, Any]:
    timeline_limit = 1000
    events, truth_source = await _load_events(db, agent=agent, session=session, limit=timeline_limit)
    checkpoints = _checkpoint_payloads(events)
    latest_event = _event_payload(events[-1]) if events else None
    active_run = await get_active_web_chat_run(db=db, agent_id=agent.id, session_id=session.id)
    session_index = await read_session_index(db, agent_id=agent.id, session_id=session.id)
    runtime_tasks = await _list_runtime_tasks(db, agent_id=agent.id, session_id=session.id)
    goals = await _list_goals(db, agent_id=agent.id, session_id=session.id)
    teams = await _list_teams(db, agent_id=agent.id, session_id=session.id)
    turn_state = _build_active_turn_state(session=session, active_run=active_run, events=events)
    active_turn = _active_turn_payload(turn_state=turn_state)
    agent_session = _agent_session_payload(session=session, runtime_tasks=runtime_tasks, turn_state=turn_state)
    session_graph = _session_graph_payload(session=session, runtime_tasks=runtime_tasks, teams=teams)
    approvals = await _list_pending_approvals(
        db,
        agent_id=agent.id,
        session_id=session.id,
        tenant_id=getattr(session, "tenant_id", None),
    )
    branches = await _list_branches(db, agent_id=agent.id, session_id=session.id)
    permission_profile = _permission_profile_payload(active_run=active_run, session=session)
    context_policy = _context_policy_payload(active_run=active_run, session=session)
    return {
        "schema": "hive.ccplus.session_workbench.v1",
        "agent_id": str(agent.id),
        "session": _session_payload(session),
        "active_turn": active_turn,
        "agent_session": agent_session,
        "session_graph": session_graph,
        "timeline": _timeline_payload(events=events, truth_source=truth_source, limit=timeline_limit),
        "tool_calls": _tool_call_payloads(events),
        "approvals": approvals,
        "hooks": _hook_payloads(events),
        "compactions": _compaction_payloads(events),
        "context_window": _context_window_payload(events),
        "branches": branches,
        "permission_profile": permission_profile,
        "context_policy": context_policy,
        "turn": {
            "truth_source": truth_source,
            "event_count": len(events),
            "checkpoint_count": len(checkpoints),
            "latest_event": latest_event,
            "checkpoints": checkpoints,
        },
        "controls": {
            "can_start_turn": active_run is None,
            "can_stop_active_run": active_run is not None,
            "can_export_json": True,
            "can_branch": bool(checkpoints),
            "can_start_goal": active_run is None,
            "can_create_agent_team": True,
            "expected_turn_id": active_turn.get("expected_turn_id") if active_turn else None,
        },
        "active_run": active_run,
        "runtime_tasks": [_runtime_task_payload(task) for task in runtime_tasks],
        "goals": [_goal_payload(goal) for goal in goals],
        "teams": teams,
        "session_index": session_index,
        "links": {
            "export": f"/api/agents/{agent.id}/sessions/{session.id}/export",
            "transcript": f"/api/agents/{agent.id}/sessions/{session.id}/transcript",
        },
    }


async def build_session_json_export(db: AsyncSession, *, agent: Agent, session: ChatSession) -> dict[str, Any]:
    workbench = await build_session_workbench(db, agent=agent, session=session)
    events, truth_source = await _load_events(db, agent=agent, session=session, limit=10000)
    return {
        "schema": "hive.ccplus.session_export.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent.id),
        "session": workbench["session"],
        "workbench": workbench,
        "transcript": {
            "truth_source": truth_source,
            "event_count": len(events),
            "events": [_event_payload(event) for event in events],
        },
    }
