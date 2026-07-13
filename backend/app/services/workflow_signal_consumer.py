"""Persistent signal-resume consumer (§9 P11) — the v2 wait_signal backend.

The piece v1 honestly refused to fake (§3.3): a durable loop that turns an
arriving PostgreSQL coordination Signal into the resume of a suspended
WorkflowRun. Matching is tenant + recipient agent + thread (= run id) +
signal_type; consumption is a row-level ``DELETE ... RETURNING`` — exactly
one consumer wins, so one Signal resumes at most one run exactly once, and
an unconsumed Signal survives process restarts (it is a PG row, not memory).
In-process signals are NOT a valid backend for persistent waits — tests/local
only.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.runtime.workflow_engine import LeafExecutor, WorkflowRunOutcome
from app.services.workflow_runtime_service import WorkflowRuntimeService
from app.services.runtime_task_service import list_active_runtime_task_records

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SignalResumedRun:
    run_id: uuid.UUID
    signal_id: uuid.UUID
    outcome: WorkflowRunOutcome


async def drain_signal_resumes(
    *,
    leaf_executor: LeafExecutor,
    session_factory=None,
    service: WorkflowRuntimeService | None = None,
) -> list[SignalResumedRun]:
    """Atomically consume Signals and requeue matching workflow RuntimeTasks.

    For every ``RuntimeTask(workflow, suspended)`` carrying a
    ``waiting_for_signal`` registration: find a PG Signal addressed to the
    run's agent on the run's thread with the registered signal_type. Signal
    deletion, step completion, and the ``resumable`` transition share one DB
    transaction; leaf execution remains exclusively owned by the shared worker.

    ``leaf_executor`` and ``service`` remain compatibility parameters for older
    callers, but are deliberately never invoked here.
    """
    del leaf_executor, service

    records = await list_active_runtime_task_records(
        statuses=("suspended",),
        task_types=("workflow",),
        limit=None,
        session_factory=session_factory,
    )
    waiting = [
        (
            uuid.UUID(str(record["task_id"])),
            (record.get("metadata") or {}).get("tenant_id") or record.get("tenant_id"),
            record.get("parent_agent_id"),
            (record.get("metadata") or {}).get("waiting_for_signal"),
        )
        for record in records
        if (record.get("metadata") or {}).get("waiting_for_signal")
    ]

    resumed: list[SignalResumedRun] = []
    for run_id, tenant_value, agent_value, registration in waiting:
        if not tenant_value or not agent_value or not isinstance(registration, dict):
            continue
        signal_type = registration.get("signal_type")
        step_id = registration.get("step_id")
        if not signal_type or not step_id:
            continue

        try:
            tenant_uuid = uuid.UUID(str(tenant_value))
            async with tenant_scoped_session(tenant_uuid, session_factory=session_factory) as session:
                from app.models.workflow import WorkflowStep

                task = (
                    await session.execute(
                        select(RuntimeTask)
                        .where(
                            RuntimeTask.id == run_id,
                            RuntimeTask.tenant_id == tenant_uuid,
                            RuntimeTask.task_type == "workflow",
                            RuntimeTask.status == "suspended",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if task is None or str(task.parent_agent_id or "") != str(agent_value):
                    continue
                metadata = dict(task.metadata_json or {})
                current_registration = metadata.get("waiting_for_signal")
                if not isinstance(current_registration, dict):
                    continue
                if str(current_registration.get("step_id") or "") != str(step_id) or str(
                    current_registration.get("signal_type") or ""
                ) != str(signal_type):
                    continue

                step_row = (
                    await session.execute(
                        select(WorkflowStep)
                        .where(
                            WorkflowStep.run_id == run_id,
                            WorkflowStep.step_id == step_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if step_row is None or step_row.status != "suspended":
                    continue

                consumed = (
                    await session.execute(
                        text(
                            "DELETE FROM coordination_signals "
                            "WHERE id = ("
                            "  SELECT id FROM coordination_signals "
                            "  WHERE tenant_id = :tenant AND to_agent_id = :agent "
                            "    AND thread_id = :thread AND signal_type = :stype "
                            "  ORDER BY created_at LIMIT 1"
                            ") RETURNING id, content"
                        ),
                        {
                            "tenant": tenant_uuid,
                            "agent": str(agent_value),
                            "thread": str(run_id),
                            "stype": signal_type,
                        },
                    )
                ).first()
                if consumed is None:
                    continue

                signal_id, signal_content = consumed
                transitioned_at = datetime.now(UTC)
                step_row.status = "done"
                step_row.result_ref = json.dumps(
                    {"signal": signal_content, "signal_type": signal_type},
                    ensure_ascii=False,
                )
                step_row.error = None
                step_row.finished_at = transitioned_at
                metadata.pop("waiting_for_signal", None)
                metadata.pop("resume_at", None)
                metadata.pop("resume_step_id", None)
                metadata.update(
                    {
                        "recovery_state": "queued_for_claim",
                        "workflow_requeue_reason": "workflow_signal_consumed",
                        "workflow_requeued_at": transitioned_at.isoformat(),
                        "consumed_signal_id": str(signal_id),
                    }
                )
                task.status = "resumable"
                task.claimed_by = None
                task.claim_expires_at = None
                task.metadata_json = metadata
        except Exception as exc:
            logger.error(
                "[WorkflowSignal] atomic consume/requeue of run %s failed: %s",
                run_id,
                exc,
                exc_info=True,
            )
            continue

        try:
            from app.services.runtime_task_worker import notify_runtime_task_worker

            await notify_runtime_task_worker(reason="workflow_signal_consumed", runtime_task_id=run_id)
        except Exception as exc:
            logger.warning("[WorkflowSignal] runtime worker wakeup failed for %s: %s", run_id, exc)
        resumed.append(
            SignalResumedRun(
                run_id=run_id,
                signal_id=signal_id,
                outcome=WorkflowRunOutcome(
                    status="suspended",
                    reason="workflow signal consumed atomically; run queued for shared worker claim",
                ),
            )
        )
    return resumed
