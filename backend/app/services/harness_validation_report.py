"""Read-only production validation for H4/H5 harness evidence.

This report does not create or repair artifacts. It inspects RuntimeTask-backed
long-task artifacts and self-evolution ledgers to show whether deployed agents
have real H4/H5 evidence, not just enabled autonomy configuration.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.trigger import AgentTrigger
from app.services.evolution_ledger import load_evolution_ledger
from app.services.evolution_validation import validate_evolution_ledger
from app.services.long_task_runtime import _artifact_dir
from app.services.long_task_validation import validate_long_task_run


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _metadata(obj: Any) -> dict[str, Any]:
    raw = _value(obj, "metadata")
    if isinstance(raw, dict):
        return raw
    raw = _value(obj, "metadata_json")
    return raw if isinstance(raw, dict) else {}


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _agent_workspace(agent_id: uuid.UUID, *, data_root: str | Path | None = None) -> Path:
    root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)
    return root / str(agent_id)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _finding(
    *,
    severity: str,
    category: str,
    agent_id: Any,
    message: str,
    recommendation: str,
    runtime_task_id: Any = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "agent_id": str(agent_id),
        "runtime_task_id": str(runtime_task_id) if runtime_task_id is not None else None,
        "message": message,
        "evidence": evidence or {},
        "recommendation": recommendation,
    }


def _runtime_task_to_dict(task: Any) -> dict[str, Any]:
    task_id = _coerce_uuid(_value(task, "id") or _value(task, "task_id"))
    parent_agent_id = _coerce_uuid(_value(task, "parent_agent_id"))
    child_agent_id = _coerce_uuid(_value(task, "child_agent_id"))
    return {
        "task_id": task_id.hex if task_id else str(_value(task, "id") or _value(task, "task_id") or ""),
        "status": _value(task, "status"),
        "task_type": _value(task, "task_type"),
        "parent_agent_id": str(parent_agent_id) if parent_agent_id else None,
        "child_agent_id": str(child_agent_id) if child_agent_id else None,
        "result": _value(task, "result") or _value(task, "result_summary"),
        "metadata": _metadata(task),
    }


def _runtime_task_agent_id(task: Any) -> uuid.UUID | None:
    metadata = _metadata(task)
    for candidate in (
        _value(task, "parent_agent_id"),
        _value(task, "child_agent_id"),
        metadata.get("agent_id"),
        metadata.get("parent_agent_id"),
        metadata.get("child_agent_id"),
    ):
        if parsed := _coerce_uuid(candidate):
            return parsed
    return None


def _runtime_task_id(task: Any) -> uuid.UUID | None:
    return _coerce_uuid(_value(task, "id") or _value(task, "task_id"))


def _long_task_ids_from_artifacts(agent_id: uuid.UUID, *, data_root: str | Path | None = None) -> set[uuid.UUID]:
    root = _artifact_dir(agent_id, uuid.uuid4(), data_root=data_root).parent
    if not root.exists():
        return set()
    ids: set[uuid.UUID] = set()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        parsed = _coerce_uuid(child.name)
        if parsed:
            ids.add(parsed)
    return ids


def _long_task_ids_from_runtime(runtime_tasks: list[Any]) -> set[uuid.UUID]:
    ids: set[uuid.UUID] = set()
    for task in runtime_tasks:
        task_id = _runtime_task_id(task)
        if task_id is None:
            continue
        metadata = _metadata(task)
        if any(key in metadata for key in ("long_task_plan", "long_task_progress", "long_task_validation")):
            ids.add(task_id)
    return ids


def _audit_h4_long_tasks(
    *,
    agent_id: uuid.UUID,
    runtime_tasks: list[Any],
    data_root: str | Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    runtime_by_id = {
        task_id: task
        for task in runtime_tasks
        if (task_id := _runtime_task_id(task)) is not None
    }
    long_task_ids = _long_task_ids_from_artifacts(agent_id, data_root=data_root)
    long_task_ids.update(_long_task_ids_from_runtime(runtime_tasks))

    findings: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    for runtime_task_id in sorted(long_task_ids, key=lambda value: value.hex):
        artifact_dir = _artifact_dir(agent_id, runtime_task_id, data_root=data_root)
        plan_present = (artifact_dir / "plan.json").exists()
        progress_present = (artifact_dir / "progress.jsonl").exists()
        validation_report_path = artifact_dir / "validation_report.json"
        validation_report_present = validation_report_path.exists()
        runtime_task = runtime_by_id.get(runtime_task_id)
        validation = validate_long_task_run(
            agent_id=agent_id,
            runtime_task_id=runtime_task_id,
            runtime_task=_runtime_task_to_dict(runtime_task) if runtime_task is not None else None,
            data_root=data_root,
            write_report=False,
        )
        item = {
            "runtime_task_id": runtime_task_id.hex,
            "runtime_status": _value(runtime_task, "status") if runtime_task is not None else None,
            "plan_present": plan_present,
            "progress_present": progress_present,
            "validation_report_present": validation_report_present,
            "validation_passed": validation["passed"],
            "validation_status": validation["status"],
            "validation_summary": validation["summary"],
        }
        items.append(item)
        if not validation["passed"]:
            findings.append(_finding(
                severity="error",
                category="long_task_validation_failed",
                agent_id=agent_id,
                runtime_task_id=runtime_task_id.hex,
                message="Long-task validation failed for an observed RuntimeTask/artifact.",
                evidence=item,
                recommendation="Inspect plan/progress/status evidence and generate a durable validation report after fixing the task trail.",
            ))
        if (plan_present or progress_present or runtime_task is not None) and not validation_report_present:
            findings.append(_finding(
                severity="warning",
                category="long_task_validation_report_missing",
                agent_id=agent_id,
                runtime_task_id=runtime_task_id.hex,
                message="Long-task evidence exists but validation_report.json is missing.",
                evidence={"plan_present": plan_present, "progress_present": progress_present},
                recommendation="Generate validation_report.json for this long task so future audits have durable validation evidence.",
            ))

    totals = {
        "long_tasks": len(items),
        "passed": sum(1 for item in items if item["validation_passed"] is True),
        "failed": sum(1 for item in items if item["validation_passed"] is False),
        "validation_reports_present": sum(1 for item in items if item["validation_report_present"]),
        "validation_reports_missing": sum(1 for item in items if not item["validation_report_present"]),
        "plans_present": sum(1 for item in items if item["plan_present"]),
        "progress_present": sum(1 for item in items if item["progress_present"]),
    }
    return {"totals": totals, "long_tasks": items}, findings


def _audit_h5_evolution(
    *,
    agent_id: uuid.UUID,
    data_root: str | Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    workspace = _agent_workspace(agent_id, data_root=data_root)
    validation_path = workspace / "evolution" / "evolution_validation_report.json"
    ledger_entries = load_evolution_ledger(workspace)
    ledger_present = bool(ledger_entries)
    validation_report_present = validation_path.exists()
    validation_report = validate_evolution_ledger(workspace, write_report=False)
    persisted_validation = _read_json(validation_path)

    h5 = {
        "ledger_present": ledger_present,
        "ledger_path": "evolution/evolution_ledger.jsonl",
        "ledger_entries": len(ledger_entries),
        "validation_report_present": validation_report_present,
        "validation_report_path": "evolution/evolution_validation_report.json",
        "validation_passed": validation_report["passed"] if ledger_present else None,
        "validation_status": validation_report["status"] if ledger_present else "not_observed",
        "validation_summary": validation_report["summary"],
        "persisted_validation_status": persisted_validation.get("status") if persisted_validation else None,
        "totals": validation_report.get("totals", {}),
    }
    findings: list[dict[str, Any]] = []
    if ledger_present and not validation_report["passed"]:
        findings.append(_finding(
            severity="error",
            category="evolution_validation_failed",
            agent_id=agent_id,
            message="Self-evolution ledger validation failed.",
            evidence={"summary": validation_report["summary"], "totals": validation_report.get("totals", {})},
            recommendation="Hold or roll back invalid promotions, then rerun evolution validation.",
        ))
    if ledger_present and not validation_report_present:
        findings.append(_finding(
            severity="warning",
            category="evolution_validation_report_missing",
            agent_id=agent_id,
            message="Evolution ledger exists but evolution_validation_report.json is missing.",
            evidence={"ledger_entries": len(ledger_entries)},
            recommendation="Run validate_evolution_ledger(write_report=True) after the next self-evolution cycle.",
        ))
    return h5, findings


def audit_agent_harness_snapshot(
    *,
    agent: Any,
    runtime_tasks: list[Any],
    enabled_trigger_count: int = 0,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    agent_id = _coerce_uuid(_value(agent, "id"))
    if agent_id is None:
        raise ValueError("agent.id must be a UUID")
    agent_name = str(_value(agent, "name", "") or "")
    tenant_id = _coerce_uuid(_value(agent, "tenant_id"))
    heartbeat_enabled = bool(_value(agent, "heartbeat_enabled", False))

    h4, h4_findings = _audit_h4_long_tasks(
        agent_id=agent_id,
        runtime_tasks=runtime_tasks,
        data_root=data_root,
    )
    h5, h5_findings = _audit_h5_evolution(agent_id=agent_id, data_root=data_root)
    findings = [*h4_findings, *h5_findings]

    has_autonomy = heartbeat_enabled or enabled_trigger_count > 0
    has_harness_evidence = h4["totals"]["long_tasks"] > 0 or h5["ledger_present"]
    if has_autonomy and not has_harness_evidence:
        findings.append(_finding(
            severity="warning",
            category="autonomy_without_harness_evidence",
            agent_id=agent_id,
            message="Agent has autonomous wake paths but no observed H4 long-task or H5 self-evolution evidence.",
            evidence={"heartbeat_enabled": heartbeat_enabled, "enabled_triggers": enabled_trigger_count},
            recommendation="Run a real long task or skill distillation cycle, then inspect validation reports.",
        ))

    return {
        "agent_id": str(agent_id),
        "agent_name": agent_name,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "autonomy": {
            "heartbeat_enabled": heartbeat_enabled,
            "enabled_triggers": enabled_trigger_count,
        },
        "h4": h4,
        "h5": h5,
        "findings": findings,
    }


def build_harness_validation_payload(
    *,
    generated_at: datetime,
    lookback_hours: int,
    agent_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    findings = [finding for report in agent_reports for finding in report.get("findings", [])]
    by_category: dict[str, int] = {}
    for finding in findings:
        category = str(finding.get("category") or "unknown")
        by_category[category] = by_category.get(category, 0) + 1

    h4_totals = {
        "long_tasks": sum(int(report.get("h4", {}).get("totals", {}).get("long_tasks", 0)) for report in agent_reports),
        "passed": sum(int(report.get("h4", {}).get("totals", {}).get("passed", 0)) for report in agent_reports),
        "failed": sum(int(report.get("h4", {}).get("totals", {}).get("failed", 0)) for report in agent_reports),
        "validation_reports_present": sum(
            int(report.get("h4", {}).get("totals", {}).get("validation_reports_present", 0))
            for report in agent_reports
        ),
    }
    h5_validation_reports = [
        report.get("h5", {})
        for report in agent_reports
        if report.get("h5", {}).get("ledger_present")
    ]
    h5_totals = {
        "ledgers_present": sum(1 for report in agent_reports if report.get("h5", {}).get("ledger_present")),
        "validation_reports_present": sum(
            1 for report in agent_reports if report.get("h5", {}).get("validation_report_present")
        ),
        "passed": sum(1 for h5 in h5_validation_reports if h5.get("validation_passed") is True),
        "failed": sum(1 for h5 in h5_validation_reports if h5.get("validation_passed") is False),
    }

    return {
        "schema": "harness_validation_report.v1",
        "generated_at": generated_at.isoformat(),
        "lookback_hours": lookback_hours,
        "totals": {
            "agents": len(agent_reports),
            "findings": len(findings),
            "errors": sum(1 for finding in findings if finding.get("severity") == "error"),
            "warnings": sum(1 for finding in findings if finding.get("severity") == "warning"),
            "infos": sum(1 for finding in findings if finding.get("severity") == "info"),
            "by_category": by_category,
            "h4": h4_totals,
            "h5": h5_totals,
        },
        "findings": findings,
        "agents": agent_reports,
    }


async def build_harness_validation_report(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    lookback_hours: int = 168,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
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
        return build_harness_validation_payload(generated_at=now, lookback_hours=lookback_hours, agent_reports=[])

    agent_ids = [agent.id for agent in agents]

    trigger_result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.agent_id.in_(agent_ids),
            AgentTrigger.is_enabled.is_(True),
        )
    )
    enabled_triggers_by_agent: dict[uuid.UUID, int] = {aid: 0 for aid in agent_ids}
    for trigger in trigger_result.scalars().all():
        enabled_triggers_by_agent[trigger.agent_id] = enabled_triggers_by_agent.get(trigger.agent_id, 0) + 1

    task_result = await db.execute(
        select(RuntimeTask).where(
            RuntimeTask.created_at >= cutoff,
            or_(
                RuntimeTask.parent_agent_id.in_(agent_ids),
                RuntimeTask.child_agent_id.in_(agent_ids),
            ),
        )
    )
    runtime_tasks_by_agent: dict[uuid.UUID, list[RuntimeTask]] = {aid: [] for aid in agent_ids}
    for task in task_result.scalars().all():
        task_agent_id = _runtime_task_agent_id(task)
        if task_agent_id in runtime_tasks_by_agent:
            runtime_tasks_by_agent[task_agent_id].append(task)

    reports = [
        audit_agent_harness_snapshot(
            agent=agent,
            runtime_tasks=runtime_tasks_by_agent.get(agent.id, []),
            enabled_trigger_count=enabled_triggers_by_agent.get(agent.id, 0),
            data_root=data_root,
        )
        for agent in agents
    ]
    return build_harness_validation_payload(
        generated_at=now,
        lookback_hours=lookback_hours,
        agent_reports=reports,
    )
