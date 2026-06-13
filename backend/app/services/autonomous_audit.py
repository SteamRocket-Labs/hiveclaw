"""Read-only audit helpers for autonomous trigger/self-evolution continuity."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.trigger import AgentTrigger
from app.services.heartbeat_policy import MANAGED_HEARTBEAT_ENABLED

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
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "agent_id": str(agent_id),
        "trigger_id": _as_str(trigger_id),
        "message": message,
        "evidence": evidence or {},
        "recommendation": recommendation,
    }


def _enabled_triggers(triggers: list[Any]) -> list[Any]:
    return [trigger for trigger in triggers if bool(getattr(trigger, "is_enabled", False))]


def audit_agent_autonomy_snapshot(
    *,
    agent: Any,
    triggers: list[Any],
    trigger_session_count: int = 0,
    heartbeat_session_count: int = 0,
    trigger_runtime_count: int = 0,
    heartbeat_runtime_count: int = 0,
) -> dict[str, Any]:
    """Audit one agent snapshot without reading or mutating external state."""
    agent_id = getattr(agent, "id")
    agent_name = getattr(agent, "name", "")
    tenant_id = getattr(agent, "tenant_id", None)
    enabled = _enabled_triggers(triggers)
    findings: list[dict[str, Any]] = []

    for trigger in enabled:
        trigger_config = getattr(trigger, "config", None) or {}
        trigger_class = str(trigger_config.get("trigger_class") or "").strip()
        if trigger_class == "event_wait" and not getattr(trigger, "max_fires", None) and not getattr(trigger, "expires_at", None):
            findings.append(_finding(
                severity="warning",
                category="event_wait_missing_lifecycle",
                agent_id=agent_id,
                trigger_id=getattr(trigger, "id", None),
                message=f"event_wait trigger '{getattr(trigger, 'name', '')}' has no max_fires or expires_at.",
                evidence={"trigger_name": getattr(trigger, "name", None), "trigger_type": getattr(trigger, "type", None)},
                recommendation="Set max_fires or expires_at so event waits cannot run forever.",
            ))

    has_autonomous_wake = bool(enabled) or MANAGED_HEARTBEAT_ENABLED
    if has_autonomous_wake and not getattr(agent, "primary_model_id", None):
        findings.append(_finding(
            severity="error",
            category="agent_no_model_blocking_autonomy",
            agent_id=agent_id,
            message=f"Agent '{agent_name or agent_id}' has autonomous wake paths but no primary model configured.",
            evidence={
                "heartbeat_enabled": MANAGED_HEARTBEAT_ENABLED,
                "enabled_triggers": len(enabled),
            },
            recommendation="Assign a primary model. Disable user-configured triggers only if they should not run yet.",
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
    """Build a read-only autonomous continuity report from DB snapshots."""
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
        per_agent_sessions = session_counts.get(agent.id, {})
        per_agent_runtime = runtime_counts.get(agent.id, {})
        agent_reports.append(audit_agent_autonomy_snapshot(
            agent=agent,
            triggers=triggers_by_agent.get(agent.id, []),
            trigger_session_count=per_agent_sessions.get("trigger", 0),
            heartbeat_session_count=per_agent_sessions.get("heartbeat", 0),
            trigger_runtime_count=per_agent_runtime.get("trigger", 0),
            heartbeat_runtime_count=per_agent_runtime.get("heartbeat", 0),
        ))

    return build_autonomous_audit_payload(
        generated_at=now,
        lookback_hours=lookback_hours,
        agent_reports=agent_reports,
    )
