"""CCPlus session workbench and JSON export aggregation."""

from __future__ import annotations

import json
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
from app.models.coordination import CoordinationCheckpoint
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowLeafCall, WorkflowStep
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
from app.runtime.codex_optimization_ledger import build_codex_optimization_ledger
from app.runtime.decision_ledger import build_agent_cycle_decision_matrix
from app.runtime.runtime_reminder_candidate import build_runtime_reminder_candidate
from app.runtime.subagent_decision_entry import subagent_decision_entry_from_metadata
from app.runtime.subagent_return_contract import subagent_return_contract_from_metadata
from app.runtime.turn_envelope import build_prompt_assembly_manifest, build_turn_envelope
from app.services.agent_team_contract import teammate_creation_discovery
from app.services.agent_team_runtime_service import (
    ACTIVE_AGENT_TEAM_MEMBER_STATUSES,
    build_agent_team_decision_entry,
    team_close_projection,
)
from app.services.enterprise_approval_visibility import is_visible_enterprise_approval
from app.services.session_goal_projection import build_session_goal_projection
from app.services.session_command_runtime import _checkpoint_payloads, _event_payload, _load_events
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


def _prompt_manifest_payload(*, active_run: Any, session: ChatSession, turn_envelope: dict[str, Any]) -> dict[str, Any]:
    from app.runtime.context import runtime_assembly_metadata

    active_metadata = _active_run_metadata(active_run)
    session_metadata = _session_metadata(session)
    runtime_manifest = _mapping(runtime_assembly_metadata(active_metadata).get("prompt_assembly_manifest")) or _mapping(
        runtime_assembly_metadata(session_metadata).get("prompt_assembly_manifest")
    )
    if runtime_manifest:
        return _compact_runtime_task_metadata(runtime_manifest)
    return build_prompt_assembly_manifest(turn_envelope)


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


_WORKBENCH_METADATA_VALUE_CHAR_LIMIT = 2000
_WORKBENCH_METADATA_OMITTED = {"omitted_oversize_value": True, "workbench_projection": True}


def _compact_runtime_task_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Size-bounded metadata projection for the workbench read model (plan B5).

    Runtime-task metadata carries replay journals, restart contracts, and
    pending-message payloads that reach hundreds of KB per task (a 50-task
    session pushed the workbench response to 5.7MB in production). The
    workbench consumes only small scalar keys (names, ids, reasons); full
    metadata stays on the RuntimeTask row and internal builders keep reading
    the ORM object directly.
    """
    compact: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, str):
            if len(value) > _WORKBENCH_METADATA_VALUE_CHAR_LIMIT:
                compact[key] = value[:200] + "…[truncated for workbench]"
            else:
                compact[key] = value
            continue
        if isinstance(value, (dict, list)):
            try:
                oversize = len(json.dumps(value, default=str)) > _WORKBENCH_METADATA_VALUE_CHAR_LIMIT
            except (TypeError, ValueError):
                oversize = True
            compact[key] = dict(_WORKBENCH_METADATA_OMITTED) if oversize else value
            continue
        compact[key] = value
    return compact


_WORKBENCH_EVENT_CONTENT_CHAR_LIMIT = 8_000
_WORKBENCH_EVENT_METADATA_TEXT_CHAR_LIMIT = 2_000
_WORKBENCH_EVENT_METADATA_VALUE_BYTE_LIMIT = 8_000
_WORKBENCH_EVENT_HEAVY_KEYS = {
    "blob",
    "bytes",
    "content_replacement",
    "data",
    "file_content",
    "html",
    "inline_content",
    "payload",
    "raw",
}


def _workbench_json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return _WORKBENCH_EVENT_METADATA_VALUE_BYTE_LIMIT + 1


def _compact_workbench_event_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return f"{value[:limit]}...[truncated for workbench]", True


def _compact_workbench_event_metadata(metadata: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    compact: dict[str, Any] = {}
    truncated = False
    for key, value in metadata.items():
        key = str(key)
        value_size = _workbench_json_size(value)
        if key in _WORKBENCH_EVENT_HEAVY_KEYS and value_size > 512:
            truncated = True
            continue
        if isinstance(value, str):
            compact_value, value_truncated = _compact_workbench_event_text(
                value,
                _WORKBENCH_EVENT_METADATA_TEXT_CHAR_LIMIT,
            )
            compact[key] = compact_value
            truncated = truncated or value_truncated
            continue
        if isinstance(value, (dict, list)) and value_size > _WORKBENCH_EVENT_METADATA_VALUE_BYTE_LIMIT:
            compact[key] = dict(_WORKBENCH_METADATA_OMITTED)
            truncated = True
            continue
        compact[key] = value
    if truncated:
        compact["_workbench_truncated"] = True
    return compact, truncated


def _compact_workbench_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    truncated = False
    content = compact.get("content")
    if isinstance(content, str):
        compact["content"], content_truncated = _compact_workbench_event_text(
            content,
            _WORKBENCH_EVENT_CONTENT_CHAR_LIMIT,
        )
        truncated = truncated or content_truncated

    metadata, metadata_truncated = _compact_workbench_event_metadata(_mapping(compact.get("metadata")))
    truncated = truncated or metadata_truncated
    if truncated:
        metadata["_workbench_truncated"] = True
    compact["metadata"] = metadata
    return compact


def _runtime_task_user_blocker(task: RuntimeTask) -> dict[str, Any] | None:
    task_status = str(getattr(task, "status", None) or "").strip().lower()
    metadata = _mapping(getattr(task, "metadata_json", None))
    if task_status == "needs_reconciliation" or bool(metadata.get("needs_reconciliation")):
        return {
            "kind": "runtime_reconciliation",
            "status": "blocked",
            "title": "需要平台管理员核对后继续",
            "reason": "任务在可能产生外部副作用时中断，系统不会自动重放。",
            "next_action": "你可以继续其他工作；平台管理员核对证据后可重试、标记完成或归档。",
            "owner": "platform_admin",
            "can_continue_other_work": True,
            "auto_resume": False,
            "retry_available": bool(metadata.get("reconciliation_retry_allowed")),
        }
    admission_status = str(getattr(task, "budget_admission_status", None) or "").strip()
    if admission_status == "waiting_budget_approval":
        return {
            "kind": "runtime_budget_approval",
            "status": "waiting",
            "title": "等待运行额度批准",
            "reason": "本任务达到公司设置的运行上限，尚未继续执行。",
            "next_action": "你可以继续其他工作；管理员批准后本任务会自动恢复。",
            "owner": "company_admin",
            "can_continue_other_work": True,
            "auto_resume": True,
        }
    if admission_status == "rejected":
        return {
            "kind": "runtime_budget_approval",
            "status": "rejected",
            "title": "运行额度申请未获批准",
            "reason": "等待中的工作已停止，已经完成的结果仍然保留。",
            "next_action": "可查看已有结果，或调整任务范围后重新发起。",
            "owner": "company_admin",
            "can_continue_other_work": True,
            "auto_resume": False,
        }
    return None


def _runtime_task_payload(task: RuntimeTask) -> dict[str, Any]:
    metadata = task.metadata_json or {}
    payload = {
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
        "metadata": _compact_runtime_task_metadata(metadata),
        "terminal_reason": metadata.get("terminal_reason"),
    }
    user_blocker = _runtime_task_user_blocker(task)
    if user_blocker is not None:
        payload["user_blocker"] = user_blocker
    if str(getattr(task, "task_type", "") or "") == "subagent" or metadata.get("subagent_return_contract"):
        return_contract = subagent_return_contract_from_metadata(
            metadata,
            status=getattr(task, "status", None),
            run_id=getattr(task, "id", None),
            child_session_id=getattr(task, "child_session_id", None),
            parent_session_id=getattr(task, "parent_session_id", None),
        )
        payload["return_contract"] = return_contract["return_contract"]
        payload["subagent_return_contract"] = return_contract
        decision_entry = subagent_decision_entry_from_metadata(
            metadata,
            status=getattr(task, "status", None),
            run_id=getattr(task, "id", None),
            child_session_id=getattr(task, "child_session_id", None),
            parent_session_id=getattr(task, "parent_session_id", None),
            summary=getattr(task, "result_summary", None),
        )
        payload["subagent_decision_entry"] = decision_entry
        payload["safe_to_retry"] = decision_entry["safe_to_retry"]
        payload["required_user_action"] = decision_entry["required_user_action"]
    return payload


def _runtime_task_label(task: RuntimeTask, metadata: dict[str, Any]) -> str:
    return _completion_task_label(task, metadata)


def _runtime_task_runtime_row(task: RuntimeTask, *, runtime_kind: str | None = None) -> dict[str, Any]:
    metadata = _mapping(getattr(task, "metadata_json", None))
    child_session_id = getattr(task, "child_session_id", None)
    row_runtime_kind = runtime_kind or getattr(task, "task_type", None)
    is_subagent_row = (
        str(row_runtime_kind or "").strip().lower() == "subagent"
        or str(getattr(task, "task_type", None) or "").strip().lower() == "subagent"
    )
    row = {
        "runtime_task_id": str(getattr(task, "id", "")),
        "task_type": getattr(task, "task_type", None),
        "runtime_kind": row_runtime_kind,
        "label": _runtime_task_label(task, metadata),
        "status": getattr(task, "status", None),
        "state": _completion_state(getattr(task, "status", None)),
        "parent_session_id": getattr(task, "parent_session_id", None),
        "child_session_id": child_session_id,
        "enterable": bool(child_session_id) and not is_subagent_row,
        "trace_id": getattr(task, "trace_id", None),
        "summary": getattr(task, "result_summary", None) or "",
        "token_usage": getattr(task, "token_usage", None) or {},
        "terminal_reason": metadata.get("terminal_reason"),
        "created_at": _iso(getattr(task, "created_at", None)),
        "started_at": _iso(getattr(task, "started_at", None)),
        "completed_at": _iso(getattr(task, "completed_at", None)),
        "metadata": _compact_runtime_task_metadata(metadata),
    }
    user_blocker = _runtime_task_user_blocker(task)
    if user_blocker is not None:
        row["user_blocker"] = user_blocker
    if metadata.get("subagent_type"):
        row["subagent_type"] = metadata.get("subagent_type")
    if is_subagent_row:
        return_contract = subagent_return_contract_from_metadata(
            metadata,
            status=getattr(task, "status", None),
            run_id=getattr(task, "id", None),
            child_session_id=child_session_id,
            parent_session_id=getattr(task, "parent_session_id", None),
        )
        row["return_contract"] = return_contract["return_contract"]
        row["subagent_return_contract"] = return_contract
        decision_entry = subagent_decision_entry_from_metadata(
            metadata,
            status=getattr(task, "status", None),
            run_id=getattr(task, "id", None),
            child_session_id=child_session_id,
            parent_session_id=getattr(task, "parent_session_id", None),
            summary=getattr(task, "result_summary", None),
        )
        row["subagent_decision_entry"] = decision_entry
        row["safe_to_retry"] = decision_entry["safe_to_retry"]
        row["required_user_action"] = decision_entry["required_user_action"]
        row["inspectable"] = bool(child_session_id)
        row["continuable"] = bool(child_session_id)
    return row


def _goal_payload(goal: AgentSessionGoal) -> dict[str, Any]:
    return build_session_goal_projection(goal)


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
        "t0_refs": metadata.get("t0_refs") or [],
        "artifacts": metadata.get("artifacts") or [],
    }


def _team_payload(team: AgentTeam, members: list[AgentTeamMember]) -> dict[str, Any]:
    decision_entry = build_agent_team_decision_entry(team, list(members))
    return {
        "id": str(team.id),
        "name": team.name,
        "status": team.status,
        "transcript_truth": team.transcript_truth,
        "lead_agent_id": str(team.lead_agent_id),
        "parent_session_id": str(team.parent_session_id),
        "member_count": len(members),
        "running_member_count": sum(1 for member in members if member.status in ACTIVE_AGENT_TEAM_MEMBER_STATUSES),
        "members": [_team_member_payload(member) for member in members],
        "agent_team_decision_entry": decision_entry,
        "team_outcome": decision_entry["team_outcome"],
        "lead_required_actions": decision_entry["lead_required_actions"],
        **team_close_projection(team),
        "created_at": _iso(team.created_at),
        "closed_at": _iso(team.closed_at),
        **teammate_creation_discovery(team.name),
    }


def _agent_team_section_item(team: dict[str, Any]) -> dict[str, Any]:
    item = dict(team)
    item["runtime_kind"] = "agent_team"
    item["enterable"] = False
    item["members"] = []
    for member in team.get("members", []):
        if not isinstance(member, dict):
            continue
        member_item = dict(member)
        member_item["runtime_kind"] = "team_member"
        member_item["enterable"] = bool(member_item.get("chat_session_id"))
        item["members"].append(member_item)
    decision_entry = item.get("agent_team_decision_entry")
    if not isinstance(decision_entry, dict):
        decision_entry = build_agent_team_decision_entry(item, list(item["members"]))
    item["agent_team_decision_entry"] = decision_entry
    item["team_outcome"] = decision_entry["team_outcome"]
    item["lead_required_actions"] = decision_entry["lead_required_actions"]
    running_statuses = ACTIVE_AGENT_TEAM_MEMBER_STATUSES
    terminal_statuses = {
        "completed",
        "succeeded",
        "success",
        "done",
        "failed",
        "error",
        "killed",
        "cancelled",
        "canceled",
        "closed",
    }
    item["running_count"] = sum(
        1 for member in item["members"] if str(member.get("status") or "").lower() in running_statuses
    )
    item["terminal_count"] = sum(
        1
        for member in item["members"]
        if str(member.get("last_turn_status") or member.get("status") or "").lower() in terminal_statuses
    )
    return item


_BACKGROUND_COMPLETION_TASK_TYPES = {
    "subagent",
    "team_member",
    "workflow",
    "delegation",
    "agent_delegation",
    "a2a_delegation",
    "advanced_plan",
    "goal_continuation",
    "long_task",
    "task",
    "trigger",
    "schedule",
}
_COMPLETION_PENDING_STATUSES = {"created", "pending", "queued", "scheduled"}
_COMPLETION_RUNNING_STATUSES = {"running", "started", "in_progress", "resuming", "waiting", "blocked"}
_COMPLETION_COMPLETED_STATUSES = {"completed", "succeeded", "success", "done"}
_COMPLETION_FAILED_STATUSES = {
    "failed",
    "error",
    "killed",
    "cancelled",
    "canceled",
    "skipped",
    "needs_reconciliation",
}


def _completion_state(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in _COMPLETION_PENDING_STATUSES:
        return "pending"
    if value in _COMPLETION_RUNNING_STATUSES:
        return "running"
    if value in _COMPLETION_COMPLETED_STATUSES:
        return "completed"
    if value in _COMPLETION_FAILED_STATUSES:
        return "failed"
    return "running" if value else "pending"


def _completion_task_label(task: RuntimeTask, metadata: dict[str, Any]) -> str:
    for key in ("subagent_name", "workflow_name", "child_agent_name", "task_name", "name"):
        raw = metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return str(getattr(task, "task_type", "") or "background_task")


def _runtime_completion_wake_payload(task: RuntimeTask) -> dict[str, Any] | None:
    task_type = str(getattr(task, "task_type", "") or "")
    if task_type not in _BACKGROUND_COMPLETION_TASK_TYPES:
        return None
    metadata = _mapping(getattr(task, "metadata_json", None))
    state = _completion_state(getattr(task, "status", None))
    terminal = state in {"completed", "failed"}
    payload = {
        "schema": "hive.ccplus.completion_wake.v1",
        "id": f"runtime_task:{task.id}",
        "kind": task_type,
        "source": "runtime_task",
        "truth_source": "runtime_tasks",
        "wake_channel": "session_mailbox_and_workbench",
        "label": _completion_task_label(task, metadata),
        "status": getattr(task, "status", None),
        "state": state,
        "terminal": terminal,
        "needs_parent_observation": terminal,
        "runtime_task_id": str(task.id),
        "parent_session_id": getattr(task, "parent_session_id", None),
        "child_session_id": getattr(task, "child_session_id", None),
        "trace_id": getattr(task, "trace_id", None),
        "summary": getattr(task, "result_summary", None) or "",
        "completed_at": _iso(getattr(task, "completed_at", None)),
        "metadata": metadata,
        "artifacts": metadata.get("artifacts") or metadata.get("artifact_paths") or [],
        "t0_refs": metadata.get("t0_refs") or metadata.get("transcript_refs") or [],
    }
    if task_type == "subagent" or metadata.get("subagent_return_contract"):
        return_contract = subagent_return_contract_from_metadata(
            metadata,
            status=getattr(task, "status", None),
            run_id=getattr(task, "id", None),
            child_session_id=getattr(task, "child_session_id", None),
            parent_session_id=getattr(task, "parent_session_id", None),
        )
        payload["return_contract"] = return_contract["return_contract"]
        payload["subagent_return_contract"] = return_contract
        decision_entry = subagent_decision_entry_from_metadata(
            metadata,
            status=getattr(task, "status", None),
            run_id=getattr(task, "id", None),
            child_session_id=getattr(task, "child_session_id", None),
            parent_session_id=getattr(task, "parent_session_id", None),
            summary=getattr(task, "result_summary", None),
        )
        payload["subagent_decision_entry"] = decision_entry
        payload["safe_to_retry"] = decision_entry["safe_to_retry"]
        payload["required_user_action"] = decision_entry["required_user_action"]
    return payload


def _team_completion_wake_payload(team: dict[str, Any], member: dict[str, Any]) -> dict[str, Any] | None:
    status = member.get("status")
    state = _completion_state(status)
    runtime_task_id = member.get("runtime_task_id")
    if not runtime_task_id and state not in {"running", "completed", "failed"}:
        return None
    terminal = state in {"completed", "failed"}
    return {
        "schema": "hive.ccplus.completion_wake.v1",
        "id": f"team_member:{member.get('id') or runtime_task_id or member.get('chat_session_id')}",
        "kind": "team_member",
        "source": "agent_team_read_model",
        "truth_source": "agent_team_member.metadata_json",
        "wake_channel": "session_mailbox_and_workbench",
        "label": str(member.get("member_name") or "team member"),
        "status": status,
        "state": state,
        "terminal": terminal,
        "needs_parent_observation": terminal,
        "team_id": team.get("id"),
        "team_name": team.get("name"),
        "member_id": member.get("id"),
        "runtime_task_id": str(runtime_task_id) if runtime_task_id else None,
        "parent_session_id": team.get("parent_session_id"),
        "child_session_id": member.get("chat_session_id"),
        "summary": member.get("summary") or "",
        "completed_at": member.get("completed_at"),
        "metadata": {
            "member_role": member.get("member_role"),
            "runtime_task_type": member.get("runtime_task_type"),
        },
        "artifacts": member.get("artifacts") or [],
        "t0_refs": member.get("t0_refs") or [],
    }


def _completion_wake_sort_key(wake: dict[str, Any]) -> tuple[int, int, int, str]:
    state_order = {"completed": 0, "failed": 0, "running": 1, "pending": 2}.get(str(wake.get("state")), 3)
    source_order = 0 if wake.get("source") == "agent_team_read_model" else 1
    kind_order = {"team_member": 0, "subagent": 1, "workflow": 2}.get(str(wake.get("kind")), 3)
    return (state_order, source_order, kind_order, str(wake.get("id") or ""))


def _completion_wake_payloads(
    *,
    runtime_tasks: list[RuntimeTask],
    teams: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    wakes: list[dict[str, Any]] = []
    seen_runtime_task_ids: set[str] = set()

    for team in teams:
        for member in team.get("members", []):
            if not isinstance(member, dict):
                continue
            payload = _team_completion_wake_payload(team, member)
            if payload is None:
                continue
            runtime_task_id = payload.get("runtime_task_id")
            if runtime_task_id:
                seen_runtime_task_ids.add(str(runtime_task_id))
            wakes.append(payload)

    for task in runtime_tasks:
        task_id = str(getattr(task, "id", "") or "")
        if task_id and task_id in seen_runtime_task_ids:
            continue
        payload = _runtime_completion_wake_payload(task)
        if payload is not None:
            wakes.append(payload)

    wakes.sort(key=_completion_wake_sort_key)
    return wakes


def _completion_wake_summary(wakes: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": len(wakes),
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "terminal": 0,
        "needs_parent_observation": 0,
    }
    for wake in wakes:
        state = str(wake.get("state") or "")
        if state in {"pending", "running", "completed", "failed"}:
            summary[state] += 1
        if wake.get("terminal") is True:
            summary["terminal"] += 1
        if wake.get("needs_parent_observation") is True:
            summary["needs_parent_observation"] += 1
    return summary


def _completion_wake_runtime_reminder_text(wake: dict[str, Any]) -> str:
    label = str(wake.get("label") or wake.get("kind") or "background work").strip()
    state = str(wake.get("state") or "unknown").strip()
    summary = str(wake.get("summary") or "").strip()
    next_action = "Observe the completion wake before deciding whether to continue, retry, or ask the user."
    if state not in {"completed", "failed"}:
        next_action = "Keep this wake visible as background work; do not busy-poll."
    parts = [f"{label} is {state}.", next_action]
    if summary:
        parts.append(f"Summary: {summary}")
    return " ".join(parts)


def _runtime_reminder_candidates_from_completion_wakes(wakes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for wake in wakes:
        priority = 90 if wake.get("needs_parent_observation") is True else 60
        candidate = build_runtime_reminder_candidate(
            source="completion_wake",
            item_id="completion_wake",
            text=_completion_wake_runtime_reminder_text(wake),
            ttl="until_parent_observed",
            priority=priority,
            consumed_at=None,
            reason="completion wake must remain explainable as runtime reminder context",
            payload={
                "wake_id": wake.get("id"),
                "wake_kind": wake.get("kind"),
                "state": wake.get("state"),
                "runtime_task_id": wake.get("runtime_task_id"),
                "child_session_id": wake.get("child_session_id"),
            },
        )
        wake["runtime_reminder_candidate"] = candidate
        candidates.append(candidate)
    return candidates


def _prompt_manifest_with_runtime_reminders(
    prompt_manifest: dict[str, Any],
    runtime_reminder_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not runtime_reminder_candidates:
        return prompt_manifest
    manifest = dict(prompt_manifest)
    existing_candidates = list(manifest.get("context_candidates") or [])
    existing_refs = list(manifest.get("context_candidate_refs") or [])
    manifest["context_candidates"] = existing_candidates + [
        dict(candidate) for candidate in runtime_reminder_candidates
    ]
    manifest["context_candidate_refs"] = existing_refs + [
        dict(candidate["candidate_ref"]) for candidate in runtime_reminder_candidates if isinstance(candidate, dict)
    ]
    manifest["runtime_reminder_candidates"] = [dict(candidate) for candidate in runtime_reminder_candidates]
    return manifest


def _completion_wake_policy_payload() -> dict[str, Any]:
    return {
        "schema": "hive.ccplus.completion_wake_policy.v1",
        "truth_source": "runtime_tasks+agent_team_read_model+session_timeline",
        "delivery_order": [
            "session_event",
            "parent_agent_wake",
            "session_workbench",
            "notification",
        ],
        "recovery": "rebuild_from_runtime_tasks_and_team_read_model_after_disconnect_or_restart",
        "busy_polling": False,
        "single_read_model": "session_workbench.completion_wakes",
    }


_RUNTIME_SECTION_KEYS = (
    "agent_teams",
    "peer_a2a",
    "subagents",
    "workflows",
    "background",
    "notifications",
    "runs",
    "raw",
)

_PEER_A2A_TASK_TYPES = {"delegation", "a2a_delegation"}
_SUBAGENT_TASK_TYPES = {"subagent"}
_WORKFLOW_TASK_TYPES = {"workflow"}
_AGENT_TEAM_TASK_TYPES = {"team_member"}
_BACKGROUND_TASK_TYPES = (
    _BACKGROUND_COMPLETION_TASK_TYPES
    - _PEER_A2A_TASK_TYPES
    - _SUBAGENT_TASK_TYPES
    - _WORKFLOW_TASK_TYPES
    - _AGENT_TEAM_TASK_TYPES
)


def _runtime_section_payload(key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "hive.ccplus.runtime_section.v1",
        "key": key,
        "count": len(items),
        "items": items,
    }


def _workflow_step_payload(row: Any) -> dict[str, Any]:
    return {
        "id": str(getattr(row, "id", "")) if getattr(row, "id", None) else None,
        "step_id": getattr(row, "step_id", None),
        "step_type": getattr(row, "step_type", None),
        "status": getattr(row, "status", None),
        "input_hash": getattr(row, "input_hash", None),
        "definition_hash": getattr(row, "definition_hash", None),
        "result_ref": getattr(row, "result_ref", None),
        "error": getattr(row, "error", None),
        "created_at": _iso(getattr(row, "created_at", None)),
        "started_at": _iso(getattr(row, "started_at", None)),
        "finished_at": _iso(getattr(row, "finished_at", None)),
    }


def _workflow_leaf_call_payload(row: Any) -> dict[str, Any]:
    metadata = _mapping(getattr(row, "metadata_json", None))
    child_session_id = getattr(row, "child_session_id", None) or metadata.get("child_session_id")
    return {
        "id": str(getattr(row, "id", "")) if getattr(row, "id", None) else None,
        "step_id": getattr(row, "step_id", None),
        "leaf_id": getattr(row, "leaf_id", None),
        "status": getattr(row, "status", None),
        "input_hash": getattr(row, "input_hash", None),
        "definition_hash": getattr(row, "definition_hash", None),
        "idempotency_key": getattr(row, "idempotency_key", None),
        "result_ref": getattr(row, "result_ref", None),
        "token_usage": getattr(row, "token_usage", None) or {},
        "error": getattr(row, "error", None),
        "child_session_id": child_session_id,
        "enterable": bool(child_session_id),
        "started_at": _iso(getattr(row, "started_at", None)),
        "finished_at": _iso(getattr(row, "finished_at", None)),
        "created_at": _iso(getattr(row, "created_at", None)),
    }


def _workflow_status_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _workflow_gate_status(
    *,
    steps: list[dict[str, Any]],
    waiting_for_signal: dict[str, Any],
    checkpoints: list[dict[str, Any]],
) -> str:
    if waiting_for_signal or any(_workflow_status_value(item.get("status")) == "pending" for item in checkpoints):
        return "waiting"
    for step in steps:
        step_type = _workflow_status_value(step.get("step_type"))
        status = _workflow_status_value(step.get("status"))
        if "gate" in step_type and status in {"pending", "running", "waiting", "suspended", "blocked"}:
            return "waiting"
    return "clear"


def _workflow_controls_payload(
    *,
    task: RuntimeTask,
    metadata: dict[str, Any],
    dynamic_workflow: dict[str, Any],
    steps: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
) -> dict[str, Any]:
    status = _workflow_status_value(getattr(task, "status", None))
    state = _completion_state(status)
    waiting_for_signal = _mapping(metadata.get("waiting_for_signal"))
    repair_plan = _mapping(metadata.get("repair_plan") or dynamic_workflow.get("repair_plan"))
    model_promotion_review = _mapping(
        metadata.get("model_promotion_review") or dynamic_workflow.get("model_promotion_review")
    )
    repairable = bool(repair_plan.get("repairable"))
    promotion_recommended = str(model_promotion_review.get("decision") or "").strip().lower() in {
        "promote",
        "recommend_promote",
    }
    run_id = str(getattr(task, "id", ""))
    preview_id = dynamic_workflow.get("preview_id") or metadata.get("preview_id")
    proposal_id = dynamic_workflow.get("proposal_id") or metadata.get("proposal_id")
    candidate_id = dynamic_workflow.get("candidate_id") or metadata.get("candidate_id")
    pending_gate = next(
        (item for item in checkpoints if _workflow_status_value(item.get("status")) == "pending"),
        None,
    )
    gate_status = _workflow_gate_status(
        steps=steps,
        waiting_for_signal=waiting_for_signal,
        checkpoints=checkpoints,
    )
    wait_status = (
        "waiting_for_gate"
        if pending_gate
        else (
            "waiting_for_signal"
            if waiting_for_signal
            else ("suspended" if status in {"waiting", "suspended", "blocked"} else "not_waiting")
        )
    )
    can_repair = repairable
    can_cancel = state in {"pending", "running"} or status in {"waiting", "suspended", "blocked"}

    def action_payload(action: str, *, enabled: bool, reason: Any, step_id: str | None = None) -> dict[str, Any]:
        return {
            "action": action,
            "enabled": bool(enabled),
            "run_id": run_id,
            "preview_id": preview_id,
            "proposal_id": proposal_id,
            "candidate_id": candidate_id,
            "step_id": step_id,
            "reason": str(reason or ""),
        }

    actions: list[dict[str, Any]] = []
    if pending_gate:
        gate_reason = str(pending_gate.get("action") or "").partition(":")[2] or "approval required"
        gate_step_id = str(pending_gate.get("step_id") or "").strip()
        actions.extend(
            [
                action_payload("approve_gate", enabled=bool(gate_step_id), reason=gate_reason, step_id=gate_step_id),
                action_payload("reject_gate", enabled=bool(gate_step_id), reason=gate_reason, step_id=gate_step_id),
            ]
        )
    elif can_repair:
        actions.append(
            action_payload("repair", enabled=True, reason=repair_plan.get("strategy") or repair_plan.get("reason"))
        )
    if can_cancel:
        actions.append(action_payload("cancel", enabled=True, reason="run is active"))
    if not pending_gate and promotion_recommended:
        actions.append(
            action_payload(
                "promote",
                enabled=True,
                reason=model_promotion_review.get("reason") or "model recommended promotion",
            )
        )

    return {
        "schema": "hive.ccplus.workflow_controls.v1",
        "run_id": run_id,
        "status": getattr(task, "status", None),
        "gate_status": gate_status,
        "wait_status": wait_status,
        "waiting_for_signal": _compact_runtime_task_metadata(waiting_for_signal) if waiting_for_signal else None,
        "repairable": repairable,
        "repair_plan": _compact_runtime_task_metadata(repair_plan),
        "model_promotion_review": _compact_runtime_task_metadata(model_promotion_review),
        "actions": actions,
    }


def _workflow_checkpoint_payload(row: Any) -> dict[str, Any]:
    metadata = _mapping(getattr(row, "extra_metadata", None))
    return {
        "checkpoint_id": str(getattr(row, "id", "")),
        "step_id": metadata.get("workflow_step_id"),
        "status": getattr(row, "status", None),
        "action": getattr(row, "action", None),
        "deadline_at": _iso(getattr(row, "deadline_at", None)),
    }


async def _list_workflow_journals(
    db: AsyncSession,
    *,
    runtime_tasks: list[RuntimeTask],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    run_ids = [getattr(task, "id", None) for task in runtime_tasks if getattr(task, "task_type", None) == "workflow"]
    run_ids = [run_id for run_id in run_ids if run_id]
    if not run_ids:
        return {}
    steps_result = await db.execute(select(WorkflowStep).where(WorkflowStep.run_id.in_(run_ids)))
    leaf_result = await db.execute(select(WorkflowLeafCall).where(WorkflowLeafCall.run_id.in_(run_ids)))
    checkpoint_result = await db.execute(
        select(CoordinationCheckpoint).where(
            CoordinationCheckpoint.extra_metadata["workflow_run_id"]
            .as_string()
            .in_([str(run_id) for run_id in run_ids])
        )
    )
    journals: dict[str, dict[str, list[dict[str, Any]]]] = {
        str(run_id): {"steps": [], "leaf_calls": [], "checkpoints": []} for run_id in run_ids
    }
    for row in steps_result.scalars().all():
        journals.setdefault(str(row.run_id), {"steps": [], "leaf_calls": [], "checkpoints": []})["steps"].append(
            _workflow_step_payload(row)
        )
    for row in leaf_result.scalars().all():
        journals.setdefault(str(row.run_id), {"steps": [], "leaf_calls": [], "checkpoints": []})["leaf_calls"].append(
            _workflow_leaf_call_payload(row)
        )
    for row in checkpoint_result.scalars().all():
        metadata = _mapping(getattr(row, "extra_metadata", None))
        run_id = str(metadata.get("workflow_run_id") or "")
        if run_id in journals:
            journals[run_id]["checkpoints"].append(_workflow_checkpoint_payload(row))
    for journal in journals.values():
        journal["steps"].sort(key=lambda item: str(item.get("step_id") or ""))
        journal["leaf_calls"].sort(key=lambda item: (str(item.get("step_id") or ""), str(item.get("leaf_id") or "")))
        journal["checkpoints"].sort(key=lambda item: str(item.get("step_id") or ""))
    return journals


def _workflow_section_item(task: RuntimeTask, journal: dict[str, Any] | None) -> dict[str, Any]:
    metadata = _mapping(getattr(task, "metadata_json", None))
    dynamic_workflow = _mapping(metadata.get("dynamic_workflow"))
    item = _runtime_task_runtime_row(task, runtime_kind="workflow")
    item["definition_source"] = metadata.get("definition_source") or dynamic_workflow.get("definition_source")
    item["dynamic_workflow"] = _compact_runtime_task_metadata(dynamic_workflow)
    item["proposal_id"] = dynamic_workflow.get("proposal_id") or metadata.get("proposal_id")
    item["preview_hash"] = dynamic_workflow.get("preview_hash") or metadata.get("preview_hash")
    item["waiting_for_signal"] = _compact_runtime_task_metadata(_mapping(metadata.get("waiting_for_signal")))
    item["repair_plan"] = _compact_runtime_task_metadata(
        _mapping(metadata.get("repair_plan") or dynamic_workflow.get("repair_plan"))
    )
    journal = _mapping(journal)
    item["steps"] = list(journal.get("steps") or [])
    item["leaf_calls"] = [
        {**leaf, "enterable": bool(leaf.get("child_session_id"))} for leaf in list(journal.get("leaf_calls") or [])
    ]
    item["workflow_controls"] = _workflow_controls_payload(
        task=task,
        metadata=metadata,
        dynamic_workflow=dynamic_workflow,
        steps=item["steps"],
        checkpoints=list(journal.get("checkpoints") or []),
    )
    return item


def _runtime_sections_payload(
    *,
    runtime_tasks: list[RuntimeTask],
    teams: list[dict[str, Any]],
    completion_wakes: list[dict[str, Any]],
    workflow_journals: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    workflow_journals = _mapping(workflow_journals)
    agent_teams = [_agent_team_section_item(team) for team in teams]
    peer_a2a: list[dict[str, Any]] = []
    subagents: list[dict[str, Any]] = []
    workflows: list[dict[str, Any]] = []
    background: list[dict[str, Any]] = []
    for task in runtime_tasks:
        task_type = str(getattr(task, "task_type", "") or "")
        if task_type in _WORKFLOW_TASK_TYPES:
            workflows.append(_workflow_section_item(task, workflow_journals.get(str(getattr(task, "id", "")))))
        elif task_type in _PEER_A2A_TASK_TYPES:
            peer_a2a.append(_runtime_task_runtime_row(task, runtime_kind="peer_a2a"))
        elif task_type in _SUBAGENT_TASK_TYPES:
            subagents.append(_runtime_task_runtime_row(task, runtime_kind="subagent"))
        elif task_type in _BACKGROUND_TASK_TYPES:
            background.append(_runtime_task_runtime_row(task, runtime_kind="background_agent"))

    categorized_task_types = (
        _PEER_A2A_TASK_TYPES
        | _SUBAGENT_TASK_TYPES
        | _WORKFLOW_TASK_TYPES
        | _AGENT_TEAM_TASK_TYPES
        | _BACKGROUND_TASK_TYPES
    )
    runs = [
        _runtime_task_payload(task)
        for task in runtime_tasks
        if str(getattr(task, "task_type", "") or "") not in categorized_task_types
    ]
    all_runtime_tasks = [_runtime_task_payload(task) for task in runtime_tasks]
    raw = [
        {
            "runtime_tasks": all_runtime_tasks,
            "completion_wakes": completion_wakes,
            "teams": teams,
        }
    ]
    return {
        "agent_teams": _runtime_section_payload("agent_teams", agent_teams),
        "peer_a2a": _runtime_section_payload("peer_a2a", peer_a2a),
        "subagents": _runtime_section_payload("subagents", subagents),
        "workflows": _runtime_section_payload("workflows", workflows),
        "background": _runtime_section_payload("background", background),
        "notifications": _runtime_section_payload("notifications", completion_wakes),
        "runs": _runtime_section_payload(
            "runs",
            [
                {
                    "runtime_task_id": run.get("id"),
                    **run,
                }
                for run in runs
            ],
        ),
        "raw": _runtime_section_payload("raw", raw),
    }


def _resume_health_payload(
    *,
    session_index: dict[str, Any],
    active_run: Any,
    checkpoint_count: int,
) -> dict[str, Any]:
    existing = _mapping(session_index.get("resume_health"))
    if existing:
        return existing
    active_run_id = _active_run_value(active_run, "id") if active_run is not None else None
    if active_run_id:
        return {
            "status": "running",
            "reason": "active_run_present",
            "active_run_id": str(active_run_id),
            "checkpoint_count": checkpoint_count,
        }
    return {
        "status": "ok",
        "reason": "no_active_run",
        "active_run_id": None,
        "checkpoint_count": checkpoint_count,
    }


def _session_index_with_resume_health(*, session_index: Any, active_run: Any, checkpoint_count: int) -> dict[str, Any]:
    payload = _mapping(session_index)
    payload.setdefault("schema", "hive.session_index.v1")
    payload["resume_health"] = _resume_health_payload(
        session_index=payload,
        active_run=active_run,
        checkpoint_count=checkpoint_count,
    )
    return payload


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
        # Read-only compatibility for pre-Session-V2 rows. No production path
        # may write or drain this legacy RuntimeTask metadata field.
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
    included: bool = True,
) -> dict[str, Any]:
    event_payloads = [_event_payload(event) for event in events] if included else []
    return {
        "schema": "hive.ccplus.session_timeline.v1",
        "truth_source": truth_source,
        "included": included,
        "event_count": len(events),
        "window_limit": limit,
        "truncated": len(events) >= limit,
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


def _tool_args(metadata: dict[str, Any]) -> dict[str, Any]:
    for key in ("args", "arguments", "tool_args"):
        raw = metadata.get(key)
        if isinstance(raw, dict):
            return dict(raw)
    return {}


def _append_unique(values: list[Any], item: Any) -> None:
    if item and item not in values:
        values.append(item)


def _mcp_server_ref(tool_name: str, args: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    for key in ("server", "server_id", "mcp_server", "mcp_server_id"):
        value = args.get(key) or metadata.get(key)
        if value:
            return f"mcp_server:{value}"
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) >= 3 and parts[1]:
            return f"mcp_server:{parts[1]}"
    return None


def _runtime_refs_from_tool_events(events: list[Any]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {
        "active_tool_names": [],
        "skill_catalog_refs": [],
        "mcp_server_refs": [],
    }
    for event in events:
        payload = _event_payload(event)
        metadata = _event_metadata_from_payload(payload)
        event_type = str(payload.get("event_type") or "")
        role = str(payload.get("role") or "")
        tool_name = str(metadata.get("tool_name") or metadata.get("name") or "").strip()
        if not (event_type in {"tool_call", "tool_result"} or role == "tool_call" or tool_name):
            continue
        if tool_name:
            _append_unique(refs["active_tool_names"], tool_name)

        args = _tool_args(metadata)
        if tool_name == "load_skill":
            skill_name = args.get("name") or args.get("skill_name") or metadata.get("skill_name")
            if skill_name:
                _append_unique(refs["skill_catalog_refs"], f"skill:{skill_name}")
        if tool_name in {"call_mcp_tool", "mcp_list_resources", "mcp_read_resource"} or tool_name.startswith("mcp__"):
            _append_unique(refs["mcp_server_refs"], _mcp_server_ref(tool_name, args, metadata))
    return refs


def _active_run_with_event_refs(active_run: Any, events: list[Any]) -> Any:
    if not isinstance(active_run, dict):
        return active_run
    enriched = dict(active_run)
    metadata = _mapping(enriched.get("metadata") or enriched.get("metadata_json"))
    refs = _runtime_refs_from_tool_events(events)
    for key, additions in refs.items():
        merged = list(metadata.get(key) or [])
        for item in additions:
            _append_unique(merged, item)
        if merged:
            metadata[key] = merged
    enriched["metadata"] = metadata
    return enriched


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


def _provider_call_ledger_payload(events: list[Any]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    for event in events:
        payload = _event_payload(event)
        metadata = {**payload, **_event_metadata_from_payload(payload)}
        if str(payload.get("event_type") or "") != "provider_call_ledger":
            continue
        ledger = _mapping(metadata.get("provider_prompt_ledger"))
        cache_metrics = _mapping(metadata.get("cache_metrics"))
        calls.append(
            {
                "event_id": payload.get("id"),
                "sequence": payload.get("sequence"),
                "created_at": payload.get("created_at"),
                "provider_call_id": ledger.get("provider_call_id"),
                "provider": ledger.get("provider"),
                "model": ledger.get("model"),
                "projected_input_tokens": ledger.get("projected_input_tokens"),
                "projected_uncached_input_tokens": ledger.get("projected_uncached_input_tokens"),
                "tool_schema_tokens": ledger.get("tool_schema_tokens"),
                "cache_read_tokens": cache_metrics.get("cache_read_tokens"),
                "cache_write_tokens": cache_metrics.get("cache_write_tokens"),
                "uncached_input_tokens": cache_metrics.get("uncached_input_tokens"),
                "total_input_tokens": cache_metrics.get("total_input_tokens"),
                "cache_hit": cache_metrics.get("cache_hit"),
                "cache_hit_rate": cache_metrics.get("cache_hit_rate"),
                "tool_count": metadata.get("tool_count"),
                "tool_call_count": metadata.get("tool_call_count"),
                "categories": ledger.get("categories") or [],
            }
        )
    return {
        "schema": "hive.ccplus.provider_call_ledger.v1",
        "call_count": len(calls),
        "latest": calls[-1] if calls else None,
        "calls": calls[-20:],
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


async def _list_runtime_tasks(
    db: AsyncSession, *, agent_id: Any, session_id: Any, limit: int = 50
) -> list[RuntimeTask]:
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
    if not teams:
        return []
    members_result = await db.execute(
        select(AgentTeamMember)
        .where(AgentTeamMember.team_id.in_([team.id for team in teams]))
        .order_by(AgentTeamMember.created_at.asc())
    )
    members_by_team: dict[Any, list[AgentTeamMember]] = {}
    for member in members_result.scalars().all():
        members_by_team.setdefault(member.team_id, []).append(member)
    return [_team_payload(team, members_by_team.get(team.id, [])) for team in teams]


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
        approval_session = (
            details.get("session_id") or details.get("parent_session_id") or details.get("chat_session_id")
        )
        if not approval_session:
            continue
        if str(approval_session) != str(session_id):
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


WORKBENCH_HEAVY_SECTIONS = frozenset({"timeline", "tool_calls", "hooks", "compactions"})
WORKBENCH_DEFAULT_TIMELINE_LIMIT = 50
WORKBENCH_MAX_TIMELINE_LIMIT = 1000

_USER_RUNTIME_STATUS = {
    "pending": "等待开始",
    "queued": "等待开始",
    "running": "正在处理",
    "resumable": "等待恢复",
    "waiting_budget_approval": "等待批准",
    "completed": "已完成",
    "succeeded": "已完成",
    "failed": "需要处理",
    "needs_reconciliation": "等待管理员核对",
    "killed": "已停止",
    "cancelled": "已取消",
}
_USER_WORKBENCH_KEYS = {
    "schema",
    "agent_id",
    "session",
    "active_turn",
    "agent_session",
    "session_graph",
    "timeline",
    "approvals",
    "branches",
    "permission_profile",
    "context_policy",
    "turn",
    "controls",
    "active_run",
    "runtime_tasks",
    "completion_wake_policy",
    "completion_wake_summary",
    "completion_wakes",
    "runtime_sections",
    "goals",
    "teams",
    "session_index",
    "links",
}
_OPERATOR_FIELD_MARKERS = (
    "secret",
    "password",
    "credential",
    "api_key",
    "provider_",
    "span_id",
    "trace_id",
    "hash",
    "arguments",
    "metadata",
    "prompt",
    "evidence",
    "raw",
)


def _sanitize_user_workbench_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_user_workbench_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    clean: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if any(marker in normalized for marker in _OPERATOR_FIELD_MARKERS):
            continue
        clean[str(key)] = _sanitize_user_workbench_value(item)
    return clean


def _user_timeline_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or event.get("type") or "event")
    summary = {
        "tool_call": "Agent 正在执行一个步骤。",
        "tool_result": "一个执行步骤已完成。",
        "permission_request": "Agent 正在等待你的确认。",
        "approval_request": "Agent 正在等待你的确认。",
        "workflow_started": "工作流已开始。",
        "workflow_completed": "工作流已完成。",
        "workflow_failed": "工作流需要处理。",
        "run_started": "任务已开始。",
        "run_completed": "任务已完成。",
        "run_cancelled": "任务已取消。",
    }.get(event_type, "运行状态已更新。")
    return {key: event[key] for key in ("id", "sequence", "event_type", "created_at") if event.get(key) is not None} | {
        "user_summary": summary
    }


def _user_runtime_task(task: dict[str, Any]) -> dict[str, Any]:
    status = str(task.get("status") or "").lower()
    clean = {
        "id": task.get("id"),
        "task_type": task.get("task_type"),
        "status": task.get("status"),
        "user_status": _USER_RUNTIME_STATUS.get(status, "正在处理"),
        "result_summary": task.get("result_summary"),
        "user_blocker": _sanitize_user_workbench_value(task.get("user_blocker")),
        "safe_to_retry": task.get("safe_to_retry"),
        "required_user_action": task.get("required_user_action"),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
    }
    return {key: value for key, value in clean.items() if value is not None}


def project_session_workbench_for_audience(
    payload: dict[str, Any],
    *,
    audience: str,
) -> dict[str, Any]:
    """Split the always-on user surface from explicit Operator View evidence."""

    if audience == "operator":
        return {**payload, "audience": "operator", "operator_details_available": True}
    user = {key: payload[key] for key in _USER_WORKBENCH_KEYS if key in payload}
    user["audience"] = "user"
    user["operator_details_available"] = True
    timeline = dict(user.get("timeline") or {})
    timeline["events"] = [_user_timeline_event(dict(item)) for item in list(timeline.get("events") or [])]
    user["timeline"] = timeline
    user["runtime_tasks"] = [_user_runtime_task(dict(item)) for item in list(user.get("runtime_tasks") or [])]
    for key in (
        "active_turn",
        "agent_session",
        "session_graph",
        "approvals",
        "branches",
        "permission_profile",
        "context_policy",
        "turn",
        "controls",
        "active_run",
        "completion_wake_policy",
        "completion_wake_summary",
        "completion_wakes",
        "runtime_sections",
        "goals",
        "teams",
        "session_index",
    ):
        if key in user:
            user[key] = _sanitize_user_workbench_value(user[key])
    return user


async def build_session_workbench(
    db: AsyncSession,
    *,
    agent: Agent,
    session: ChatSession,
    timeline_limit: int = WORKBENCH_DEFAULT_TIMELINE_LIMIT,
    include: set[str] | frozenset[str] | None = None,
    audience: str = "operator",
) -> dict[str, Any]:
    """Session workbench read model.

    Heavy events-derived sections (timeline/tool_calls/hooks/compactions) are
    returned only when explicitly requested via ``include`` — the default
    response stays light for the always-on chat surface (plan B1); the JSON
    export path requests everything.
    """
    included_sections = WORKBENCH_HEAVY_SECTIONS & set(include or ())
    if timeline_limit <= 0:
        timeline_limit = WORKBENCH_DEFAULT_TIMELINE_LIMIT
    timeline_limit = min(timeline_limit, WORKBENCH_MAX_TIMELINE_LIMIT)
    events, truth_source = await _load_events(db, agent=agent, session=session, limit=timeline_limit)
    checkpoints = [_compact_workbench_event_payload(payload) for payload in _checkpoint_payloads(events)]
    latest_event = _compact_workbench_event_payload(_event_payload(events[-1])) if events else None
    active_run = await get_active_web_chat_run(db=db, agent_id=agent.id, session_id=session.id)
    active_run_projection = _active_run_with_event_refs(active_run, events)
    runtime_tasks = await _list_runtime_tasks(db, agent_id=agent.id, session_id=session.id)
    goals = await _list_goals(db, agent_id=agent.id, session_id=session.id)
    teams = await _list_teams(db, agent_id=agent.id, session_id=session.id)
    workflow_journals = await _list_workflow_journals(db, runtime_tasks=runtime_tasks)
    completion_wakes = _completion_wake_payloads(runtime_tasks=runtime_tasks, teams=teams)
    runtime_reminder_candidates = _runtime_reminder_candidates_from_completion_wakes(completion_wakes)
    session_index = _session_index_with_resume_health(
        session_index=await read_session_index(db, agent_id=agent.id, session_id=session.id),
        active_run=active_run_projection,
        checkpoint_count=len(checkpoints),
    )
    runtime_sections = _runtime_sections_payload(
        runtime_tasks=runtime_tasks,
        teams=teams,
        completion_wakes=completion_wakes,
        workflow_journals=workflow_journals,
    )
    turn_state = _build_active_turn_state(session=session, active_run=active_run_projection, events=events)
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
    permission_profile = _permission_profile_payload(active_run=active_run_projection, session=session)
    context_policy = _context_policy_payload(active_run=active_run_projection, session=session)
    turn_envelope = build_turn_envelope(agent_id=agent.id, session=session, active_run=active_run_projection)
    prompt_manifest = _prompt_manifest_payload(
        active_run=active_run_projection,
        session=session,
        turn_envelope=turn_envelope,
    )
    prompt_manifest = _prompt_manifest_with_runtime_reminders(prompt_manifest, runtime_reminder_candidates)
    payload = {
        "schema": "hive.ccplus.session_workbench.v1",
        "agent_id": str(agent.id),
        "session": _session_payload(session),
        "active_turn": active_turn,
        "agent_session": agent_session,
        "session_graph": session_graph,
        "timeline": _timeline_payload(
            events=events,
            truth_source=truth_source,
            limit=timeline_limit,
            included="timeline" in included_sections,
        ),
        "tool_calls": _tool_call_payloads(events) if "tool_calls" in included_sections else [],
        "approvals": approvals,
        "hooks": _hook_payloads(events) if "hooks" in included_sections else [],
        "compactions": _compaction_payloads(events) if "compactions" in included_sections else [],
        "context_window": _context_window_payload(events),
        "provider_call_ledger": _provider_call_ledger_payload(events),
        "branches": branches,
        "permission_profile": permission_profile,
        "context_policy": context_policy,
        "codex_optimization_ledger": build_codex_optimization_ledger(),
        "agent_cycle_decision_matrix": build_agent_cycle_decision_matrix(),
        "turn_envelope": turn_envelope,
        "prompt_manifest": prompt_manifest,
        "turn": {
            "truth_source": truth_source,
            "event_count": len(events),
            "checkpoint_count": len(checkpoints),
            "latest_event": latest_event,
            "checkpoints": checkpoints,
        },
        "controls": {
            "can_start_turn": active_run_projection is None,
            "can_stop_active_run": active_run_projection is not None,
            "can_export_json": True,
            "can_branch": bool(checkpoints),
            "can_start_goal": active_run_projection is None,
            "can_create_agent_team": True,
            "expected_turn_id": active_turn.get("expected_turn_id") if active_turn else None,
        },
        "active_run": active_run_projection,
        "runtime_tasks": [_runtime_task_payload(task) for task in runtime_tasks],
        "completion_wake_policy": _completion_wake_policy_payload(),
        "completion_wake_summary": _completion_wake_summary(completion_wakes),
        "completion_wakes": completion_wakes,
        "runtime_reminder_candidates": runtime_reminder_candidates,
        "runtime_sections": runtime_sections,
        "goals": [_goal_payload(goal) for goal in goals],
        "teams": teams,
        "session_index": session_index,
        "links": {
            "export": f"/api/agents/{agent.id}/sessions/{session.id}/export",
            "transcript": f"/api/agents/{agent.id}/sessions/{session.id}/transcript",
        },
    }
    return project_session_workbench_for_audience(payload, audience=audience)


async def build_session_json_export(
    db: AsyncSession,
    *,
    agent: Agent,
    session: ChatSession,
    audience: str = "operator",
) -> dict[str, Any]:
    workbench = await build_session_workbench(
        db,
        agent=agent,
        session=session,
        timeline_limit=WORKBENCH_MAX_TIMELINE_LIMIT,
        include=WORKBENCH_HEAVY_SECTIONS,
        audience=audience,
    )
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
            "events": (
                [_event_payload(event) for event in events]
                if audience == "operator"
                else [_user_timeline_event(_event_payload(event)) for event in events]
            ),
        },
        "audience": "operator" if audience == "operator" else "user",
    }
