"""Read-only audit helpers for autonomous trigger/self-evolution continuity."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.trigger import AgentTrigger
from app.services.focus_state import normalize_focus_task_id, parse_focus_tasks

_SCHEDULED_TRIGGER_TYPES = {"cron", "once", "interval"}
_RUNTIME_AUDIT_SOURCES = {"trigger", "heartbeat"}


def _as_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _finding(
    *,
    severity: str,
    category: str,
    agent_id: Any,
    message: str,
    recommendation: str,
    trigger_id: Any = None,
    focus_ref: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "agent_id": str(agent_id),
        "trigger_id": _as_str(trigger_id),
        "focus_ref": focus_ref,
        "message": message,
        "evidence": evidence or {},
        "recommendation": recommendation,
    }


def _trigger_focus_ref(trigger: Any) -> str | None:
    raw = getattr(trigger, "focus_ref", None)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _enabled_triggers(triggers: list[Any]) -> list[Any]:
    return [trigger for trigger in triggers if bool(getattr(trigger, "is_enabled", False))]


def _detect_noncanonical_focus_items(focus_text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    in_tasks_section = False
    for line_no, line in enumerate(focus_text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("##"):
            header = stripped.lstrip("#").strip().lower()
            in_tasks_section = "task" in header or "focus" in header
            continue
        if not in_tasks_section and not stripped.startswith(("- [", "* [")):
            continue
        if parse_focus_tasks(stripped):
            continue
        looks_like_task = (
            stripped.startswith(("- [", "* ["))
            or (in_tasks_section and stripped.startswith(("- ", "* ")))
            or (in_tasks_section and stripped[:1].isdigit())
        )
        if looks_like_task:
            items.append({"line_no": line_no, "line": stripped})
    return items


def audit_agent_autonomy_snapshot(
    *,
    agent: Any,
    focus_text: str | None,
    triggers: list[Any],
    trigger_session_count: int = 0,
    heartbeat_session_count: int = 0,
    trigger_runtime_count: int = 0,
    heartbeat_runtime_count: int = 0,
    focus_read_error: str | None = None,
) -> dict[str, Any]:
    """Audit one agent snapshot without reading or mutating external state."""
    agent_id = getattr(agent, "id")
    agent_name = getattr(agent, "name", "")
    tenant_id = getattr(agent, "tenant_id", None)
    enabled = _enabled_triggers(triggers)
    findings: list[dict[str, Any]] = []

    if focus_read_error:
        findings.append(_finding(
            severity="warning",
            category="focus_read_error",
            agent_id=agent_id,
            message=f"Could not read focus.md for agent {agent_name or agent_id}.",
            evidence={"error": focus_read_error},
            recommendation="Verify the agent workspace volume and focus.md permissions.",
        ))

    focus_tasks = parse_focus_tasks(focus_text or "")
    focus_by_id = {task.task_id: task for task in focus_tasks}
    active_focus_ids = {task.task_id for task in focus_tasks if not task.completed}
    completed_focus_ids = {task.task_id for task in focus_tasks if task.completed}
    enabled_focus_refs = {
        normalize_focus_task_id(ref)
        for trigger in enabled
        if (ref := _trigger_focus_ref(trigger)) is not None
    }

    for task in focus_tasks:
        if not task.completed and task.task_id not in enabled_focus_refs:
            findings.append(_finding(
                severity="error",
                category="orphan_focus_task",
                agent_id=agent_id,
                focus_ref=task.task_id,
                message=f"Unfinished focus task '{task.task_id}' has no enabled trigger bound to it.",
                evidence={"task": task.raw_line},
                recommendation="Create or bind an enabled trigger with focus_ref matching this task, or mark the task as manual/no-wake in the future objective ledger.",
            ))

    for item in _detect_noncanonical_focus_items(focus_text or ""):
        findings.append(_finding(
            severity="warning",
            category="noncanonical_focus_item",
            agent_id=agent_id,
            message="focus.md contains a task-like line that the canonical parser will ignore.",
            evidence=item,
            recommendation="Rewrite the line as '- [ ] task_id :: description' so the trigger audit can track it.",
        ))

    for trigger in enabled:
        raw_ref = _trigger_focus_ref(trigger)
        normalized_ref = normalize_focus_task_id(raw_ref) if raw_ref else None
        if normalized_ref and normalized_ref not in focus_by_id:
            findings.append(_finding(
                severity="error",
                category="trigger_focus_ref_missing",
                agent_id=agent_id,
                trigger_id=getattr(trigger, "id", None),
                focus_ref=normalized_ref,
                message=f"Enabled trigger '{getattr(trigger, 'name', '')}' references missing focus_ref '{raw_ref}'.",
                evidence={"trigger_name": getattr(trigger, "name", None), "raw_focus_ref": raw_ref},
                recommendation="Create the matching focus.md task or update/cancel the trigger.",
            ))
        if normalized_ref and normalized_ref in completed_focus_ids:
            findings.append(_finding(
                severity="error",
                category="completed_focus_trigger_active",
                agent_id=agent_id,
                trigger_id=getattr(trigger, "id", None),
                focus_ref=normalized_ref,
                message=f"Enabled trigger '{getattr(trigger, 'name', '')}' is still bound to completed focus task '{normalized_ref}'.",
                evidence={"trigger_name": getattr(trigger, "name", None)},
                recommendation="Disable the trigger or reopen the focus task if it still needs work.",
            ))
        if getattr(trigger, "type", None) in _SCHEDULED_TRIGGER_TYPES and not raw_ref:
            findings.append(_finding(
                severity="warning",
                category="scheduled_trigger_without_focus_ref",
                agent_id=agent_id,
                trigger_id=getattr(trigger, "id", None),
                message=f"Scheduled trigger '{getattr(trigger, 'name', '')}' has no focus_ref.",
                evidence={"trigger_name": getattr(trigger, "name", None), "trigger_type": getattr(trigger, "type", None)},
                recommendation="If this is task work, bind it to focus.md; if it is a standalone scheduled job, mark it as such in P1 metadata.",
            ))

    has_autonomous_wake = bool(enabled) or bool(getattr(agent, "heartbeat_enabled", False))
    if has_autonomous_wake and not getattr(agent, "primary_model_id", None):
        findings.append(_finding(
            severity="error",
            category="agent_no_model_blocking_autonomy",
            agent_id=agent_id,
            message=f"Agent '{agent_name or agent_id}' has autonomous wake paths but no primary model configured.",
            evidence={
                "heartbeat_enabled": bool(getattr(agent, "heartbeat_enabled", False)),
                "enabled_triggers": len(enabled),
            },
            recommendation="Assign a primary model or disable heartbeat/triggers until the agent can execute.",
        ))

    if trigger_session_count > 0 and trigger_runtime_count == 0:
        findings.append(_finding(
            severity="error",
            category="trigger_runtime_gap",
            agent_id=agent_id,
            message="Trigger reflection sessions exist in the lookback window, but no trigger RuntimeTask records exist.",
            evidence={"trigger_sessions": trigger_session_count, "trigger_runtime_tasks": trigger_runtime_count},
            recommendation="P1 should create RuntimeTask(task_type='trigger') for every trigger attempt, including skips/failures.",
        ))
    if heartbeat_session_count > 0 and heartbeat_runtime_count == 0:
        findings.append(_finding(
            severity="error",
            category="heartbeat_runtime_gap",
            agent_id=agent_id,
            message="Heartbeat sessions exist in the lookback window, but no heartbeat RuntimeTask records exist.",
            evidence={"heartbeat_sessions": heartbeat_session_count, "heartbeat_runtime_tasks": heartbeat_runtime_count},
            recommendation="P1 should create RuntimeTask(task_type='heartbeat') for every heartbeat attempt, including skips/failures.",
        ))

    return {
        "agent_id": str(agent_id),
        "agent_name": agent_name,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "findings": findings,
        "counts": {
            "enabled_triggers": len(enabled),
            "focus_tasks": len(focus_tasks),
            "active_focus_tasks": len(active_focus_ids),
            "completed_focus_tasks": len(completed_focus_ids),
            "trigger_sessions": trigger_session_count,
            "heartbeat_sessions": heartbeat_session_count,
            "trigger_runtime_tasks": trigger_runtime_count,
            "heartbeat_runtime_tasks": heartbeat_runtime_count,
        },
    }


def build_autonomous_audit_payload(
    *,
    generated_at: datetime,
    lookback_hours: int,
    agent_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    findings = [finding for report in agent_reports for finding in report.get("findings", [])]
    totals = {
        "agents": len(agent_reports),
        "findings": len(findings),
        "errors": sum(1 for finding in findings if finding.get("severity") == "error"),
        "warnings": sum(1 for finding in findings if finding.get("severity") == "warning"),
        "infos": sum(1 for finding in findings if finding.get("severity") == "info"),
    }
    by_category: dict[str, int] = {}
    for finding in findings:
        category = str(finding.get("category") or "unknown")
        by_category[category] = by_category.get(category, 0) + 1
    totals["by_category"] = by_category

    return {
        "generated_at": generated_at.isoformat(),
        "lookback_hours": lookback_hours,
        "totals": totals,
        "findings": findings,
        "agents": agent_reports,
    }


def _focus_path_candidates(agent_id: uuid.UUID) -> list[Path]:
    settings = get_settings()
    candidates = [Path(settings.AGENT_DATA_DIR) / str(agent_id) / "focus.md"]
    fallback = Path("/tmp/hive_workspaces") / str(agent_id) / "focus.md"
    if fallback not in candidates:
        candidates.append(fallback)
    return candidates


def _read_focus_text(agent_id: uuid.UUID) -> tuple[str | None, str | None]:
    for path in _focus_path_candidates(agent_id):
        try:
            if path.exists():
                return path.read_text(encoding="utf-8"), None
        except Exception as exc:
            return None, f"{path}: {exc}"
    return None, None


def _count_sessions_by_agent(sessions: list[ChatSession]) -> dict[uuid.UUID, dict[str, int]]:
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for session in sessions:
        source = getattr(session, "source_channel", None)
        if source not in _RUNTIME_AUDIT_SOURCES:
            continue
        agent_counts = counts.setdefault(session.agent_id, {"trigger": 0, "heartbeat": 0})
        agent_counts[source] += 1
    return counts


def _count_runtime_tasks_by_agent(tasks: list[RuntimeTask]) -> dict[uuid.UUID, dict[str, int]]:
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for task in tasks:
        task_type = getattr(task, "task_type", None)
        if task_type not in _RUNTIME_AUDIT_SOURCES:
            continue
        metadata = getattr(task, "metadata_json", None) or {}
        raw_agent_id = (
            getattr(task, "parent_agent_id", None)
            or getattr(task, "child_agent_id", None)
            or metadata.get("agent_id")
            or metadata.get("parent_agent_id")
        )
        try:
            agent_id = raw_agent_id if isinstance(raw_agent_id, uuid.UUID) else uuid.UUID(str(raw_agent_id))
        except (TypeError, ValueError, AttributeError):
            continue
        agent_counts = counts.setdefault(agent_id, {"trigger": 0, "heartbeat": 0})
        agent_counts[task_type] += 1
    return counts


async def build_autonomous_audit_report(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    lookback_hours: int = 24,
) -> dict[str, Any]:
    """Build a read-only autonomous continuity report from DB + focus.md snapshots."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)

    agent_stmt = select(Agent)
    if tenant_id is not None:
        agent_stmt = agent_stmt.where(Agent.tenant_id == tenant_id)
    if agent_id is not None:
        agent_stmt = agent_stmt.where(Agent.id == agent_id)
    agent_result = await db.execute(agent_stmt)
    agents = list(agent_result.scalars().all())

    if not agents:
        return build_autonomous_audit_payload(generated_at=now, lookback_hours=lookback_hours, agent_reports=[])

    agent_ids = [agent.id for agent in agents]

    trigger_result = await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id.in_(agent_ids)))
    triggers_by_agent: dict[uuid.UUID, list[AgentTrigger]] = {aid: [] for aid in agent_ids}
    for trigger in trigger_result.scalars().all():
        triggers_by_agent.setdefault(trigger.agent_id, []).append(trigger)

    session_result = await db.execute(
        select(ChatSession).where(
            ChatSession.agent_id.in_(agent_ids),
            ChatSession.source_channel.in_(tuple(_RUNTIME_AUDIT_SOURCES)),
            ChatSession.created_at >= cutoff,
        )
    )
    session_counts = _count_sessions_by_agent(list(session_result.scalars().all()))

    task_result = await db.execute(
        select(RuntimeTask).where(
            RuntimeTask.task_type.in_(tuple(_RUNTIME_AUDIT_SOURCES)),
            RuntimeTask.created_at >= cutoff,
        )
    )
    runtime_counts = _count_runtime_tasks_by_agent(list(task_result.scalars().all()))

    agent_reports: list[dict[str, Any]] = []
    for agent in agents:
        focus_text, focus_error = _read_focus_text(agent.id)
        per_agent_sessions = session_counts.get(agent.id, {})
        per_agent_runtime = runtime_counts.get(agent.id, {})
        agent_reports.append(audit_agent_autonomy_snapshot(
            agent=agent,
            focus_text=focus_text,
            triggers=triggers_by_agent.get(agent.id, []),
            trigger_session_count=per_agent_sessions.get("trigger", 0),
            heartbeat_session_count=per_agent_sessions.get("heartbeat", 0),
            trigger_runtime_count=per_agent_runtime.get("trigger", 0),
            heartbeat_runtime_count=per_agent_runtime.get("heartbeat", 0),
            focus_read_error=focus_error,
        ))

    return build_autonomous_audit_payload(
        generated_at=now,
        lookback_hours=lookback_hours,
        agent_reports=agent_reports,
    )
