"""Writable H4/H5 harness canary.

The read-only harness validation endpoint proves whether evidence exists. This
module creates explicit canary evidence for platform-admin verification without
changing business objectives, triggers, skills, or prompts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.trigger import AgentTrigger
from app.services.evolution_ledger import record_eval_run, record_evolution_candidate, record_promotion_decision
from app.services.evolution_validation import validate_evolution_ledger
from app.services.harness_validation_report import audit_agent_harness_snapshot
from app.services.long_task_runtime import (
    append_long_task_progress_artifact,
    write_long_task_plan_artifact,
)
from app.services.long_task_validation import validate_long_task_run


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _agent_name(agent: Any) -> str:
    return str(_value(agent, "name") or "Agent")


def _agent_workspace(agent_id: uuid.UUID, *, data_root: str | Path | None = None) -> Path:
    from app.config import get_settings

    root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)
    return root / str(agent_id)


def _has_existing_harness_evidence(agent_report: dict[str, Any]) -> bool:
    return (
        int(agent_report.get("h4", {}).get("totals", {}).get("long_tasks", 0) or 0) > 0
        or bool(agent_report.get("h5", {}).get("ledger_present"))
    )


def _result(
    *,
    agent: Any,
    status: str,
    reason: str,
    runtime_task_id: uuid.UUID | None = None,
    h4: dict[str, Any] | None = None,
    h5: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "agent_id": str(_value(agent, "id")),
        "agent_name": _agent_name(agent),
        "tenant_id": str(_value(agent, "tenant_id")) if _value(agent, "tenant_id") else None,
        "status": status,
        "reason": reason,
        "runtime_task_id": runtime_task_id.hex if runtime_task_id else None,
        "h4": h4 or {},
        "h5": h5 or {},
    }


def _write_h4_canary(
    *,
    agent: Any,
    runtime_task_id: uuid.UUID,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    agent_id = _value(agent, "id")
    plan = write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        objective_id=None,
        spec=(
            "Harness canary: verify that this agent can leave a RuntimeTask-backed "
            "long-task plan, progress trail, resume context, and validation report."
        ),
        acceptance_criteria=[
            "RuntimeTask is durable and linked to the agent.",
            "long_task_plan.v1 exists.",
            "long_task_progress.v1 exists.",
            "long_task_validation_report.v1 exists and passes.",
        ],
        verification_commands=[
            "GET /api/admin/harness-validation?lookback_hours=1",
        ],
        risk_gates=[
            "No business objective, trigger, skill, prompt, or permission state is changed.",
            "Canary evidence is explicitly marked and must not be treated as organic task success.",
        ],
        data_root=data_root,
    )
    progress = append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="completed",
        delta="Harness canary completed: durable long-task artifacts were written.",
        output_paths=[plan["path"]],
        data_root=data_root,
    )
    validation = validate_long_task_run(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        runtime_task={
            "status": "completed",
            "result": "Harness canary completed.",
            "metadata": {"harness_canary": True, "no_behavior_change": True},
        },
        data_root=data_root,
        write_report=True,
    )
    return {
        "plan": plan,
        "progress": progress,
        "validation": validation.get("report_artifact"),
        "validation_passed": validation["passed"],
        "validation_summary": validation["summary"],
    }


def _write_h5_canary(
    *,
    agent: Any,
    runtime_task_id: uuid.UUID,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    agent_id = _value(agent, "id")
    workspace = _agent_workspace(agent_id, data_root=data_root)
    candidate = record_evolution_candidate(
        workspace,
        target_type="harness_canary",
        target_id="autonomous-harness-health",
        diff="+ canary: verified candidate/eval/decision ledger path without behavior change",
        source_attempt_ids=[runtime_task_id.hex],
        baseline_version="harness-canary@v1",
        metadata={
            "harness_canary": True,
            "no_behavior_change": True,
            "agent_id": str(agent_id),
        },
    )
    eval_run = record_eval_run(
        workspace,
        candidate_id=candidate["candidate_id"],
        dataset="harness_canary.internal",
        reward=1.0,
        baseline_reward=1.0,
        passed=True,
        traces=[runtime_task_id.hex],
        critical_regressions=0,
        metadata={
            "harness_canary": True,
            "no_behavior_change": True,
        },
    )
    record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision="hold",
        reason="Canary validates the self-evolution ledger path only; no behavior change is promoted.",
        metadata={
            "harness_canary": True,
            "no_behavior_change": True,
        },
    )
    validation = validate_evolution_ledger(workspace, write_report=True)
    return {
        "candidate_id": candidate["candidate_id"],
        "eval_dataset": eval_run["dataset"],
        "decision": "hold",
        "validation": validation.get("report_artifact"),
        "validation_passed": validation["passed"],
        "validation_summary": validation["summary"],
    }


async def _fetch_agents(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    max_agents: int,
) -> list[Agent]:
    stmt = select(Agent).order_by(Agent.created_at.asc()).limit(max_agents)
    if tenant_id is not None:
        stmt = stmt.where(Agent.tenant_id == tenant_id)
    if agent_id is not None:
        stmt = stmt.where(Agent.id == agent_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _enabled_trigger_counts(db: AsyncSession, agents: list[Agent]) -> dict[uuid.UUID, int]:
    counts: dict[uuid.UUID, int] = {agent.id: 0 for agent in agents}
    if not agents:
        return counts
    result = await db.execute(
        select(AgentTrigger).where(
            AgentTrigger.agent_id.in_([agent.id for agent in agents]),
            AgentTrigger.is_enabled.is_(True),
        )
    )
    for trigger in result.scalars().all():
        counts[trigger.agent_id] = counts.get(trigger.agent_id, 0) + 1
    return counts


async def run_harness_canary(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    include_h4: bool = True,
    include_h5: bool = True,
    max_agents: int = 50,
    force: bool = False,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Create explicit H4/H5 canary evidence for autonomous agents.

    This is a write endpoint by design. It never mutates objectives, triggers,
    prompts, skills, permissions, or model config.
    """

    generated_at = _now()
    agents = await _fetch_agents(db=db, tenant_id=tenant_id, agent_id=agent_id, max_agents=max_agents)
    trigger_counts = await _enabled_trigger_counts(db, agents)

    results: list[dict[str, Any]] = []
    for agent in agents:
        enabled_trigger_count = trigger_counts.get(agent.id, 0)
        has_autonomy = bool(getattr(agent, "heartbeat_enabled", False)) or enabled_trigger_count > 0
        if not has_autonomy:
            results.append(_result(agent=agent, status="skipped", reason="no_autonomous_wake_path"))
            continue

        if not include_h4 and not include_h5:
            results.append(_result(agent=agent, status="skipped", reason="no_canary_surface_selected"))
            continue

        current_report = audit_agent_harness_snapshot(
            agent=agent,
            runtime_tasks=[],
            enabled_trigger_count=enabled_trigger_count,
            data_root=data_root,
        )
        if not force and _has_existing_harness_evidence(current_report):
            results.append(_result(agent=agent, status="skipped", reason="harness_evidence_already_present"))
            continue

        runtime_task_id = uuid.uuid4()
        h4: dict[str, Any] = {}
        h5: dict[str, Any] = {}
        metadata: dict[str, Any] = {
            "source": "harness_canary",
            "harness_canary": True,
            "no_behavior_change": True,
            "agent_id": str(agent.id),
            "tenant_id": str(agent.tenant_id) if agent.tenant_id else None,
            "enabled_trigger_count": enabled_trigger_count,
            "include_h4": include_h4,
            "include_h5": include_h5,
        }

        if include_h4:
            h4 = _write_h4_canary(agent=agent, runtime_task_id=runtime_task_id, data_root=data_root)
            metadata.update({
                "long_task_plan": h4.get("plan"),
                "long_task_progress": h4.get("progress"),
                "long_task_validation": h4.get("validation"),
                "long_task_validation_passed": h4.get("validation_passed"),
                "long_task_validation_summary": h4.get("validation_summary"),
            })
        if include_h5:
            h5 = _write_h5_canary(agent=agent, runtime_task_id=runtime_task_id, data_root=data_root)
            metadata.update({
                "evolution_candidate_id": h5.get("candidate_id"),
                "evolution_validation": h5.get("validation"),
                "evolution_validation_passed": h5.get("validation_passed"),
                "evolution_validation_summary": h5.get("validation_summary"),
            })

        db.add(RuntimeTask(
            id=runtime_task_id,
            task_type="harness_canary",
            parent_agent_id=agent.id,
            child_agent_id=None,
            child_agent_name=None,
            status="completed",
            prompt="Harness canary verifies H4/H5 evidence paths without changing behavior.",
            result_summary="Harness canary completed; no behavior change was promoted.",
            trace_id=f"harness-canary:{runtime_task_id.hex}",
            parent_session_id=None,
            child_session_id=None,
            depth=0,
            started_at=generated_at,
            completed_at=_now(),
            metadata_json=metadata,
        ))
        await db.commit()
        results.append(
            _result(
                agent=agent,
                status="written",
                reason="harness_canary_written",
                runtime_task_id=runtime_task_id,
                h4=h4,
                h5=h5,
            )
        )

    return {
        "schema": "harness_canary_run.v1",
        "mode": "write",
        "generated_at": generated_at.isoformat(),
        "tenant_id": str(tenant_id) if tenant_id else None,
        "agent_id": str(agent_id) if agent_id else None,
        "include_h4": include_h4,
        "include_h5": include_h5,
        "force": force,
        "max_agents": max_agents,
        "totals": {
            "agents_considered": len(agents),
            "agents_written": sum(1 for item in results if item["status"] == "written"),
            "skipped": sum(1 for item in results if item["status"] == "skipped"),
            "h4_written": sum(1 for item in results if item["status"] == "written" and bool(item.get("h4"))),
            "h5_written": sum(1 for item in results if item["status"] == "written" and bool(item.get("h5"))),
        },
        "results": results,
    }
