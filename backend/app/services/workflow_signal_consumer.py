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

from sqlalchemy import select, text

from app.database import enter_rls_bypass, tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.runtime.workflow_engine import LeafExecutor, WorkflowRunOutcome
from app.services.workflow_runtime_service import WorkflowRuntimeService

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
    """Match waiting runs against persisted Signals and resume the hits.

    For every ``RuntimeTask(workflow, suspended)`` carrying a
    ``waiting_for_signal`` registration: find a PG Signal addressed to the
    run's agent on the run's thread with the registered signal_type, consume
    it atomically, mark the waiting step done (signal payload as output),
    clear the registration, and resume under the run lease."""
    runtime = service or WorkflowRuntimeService(session_factory=session_factory)

    async with tenant_scoped_session(session_factory=session_factory) as session:
        async with enter_rls_bypass(session, reason="workflow signal daemon — enumerate suspended workflow waits"):
            rows = (
                (
                    await session.execute(
                        select(RuntimeTask).where(RuntimeTask.task_type == "workflow", RuntimeTask.status == "suspended")
                    )
                )
                .scalars()
                .all()
            )
        waiting = [
            (
                row.id,
                (row.metadata_json or {}).get("tenant_id"),
                str(row.parent_agent_id) if row.parent_agent_id else None,
                (row.metadata_json or {}).get("waiting_for_signal"),
            )
            for row in rows
            if (row.metadata_json or {}).get("waiting_for_signal")
        ]

    resumed: list[SignalResumedRun] = []
    for run_id, tenant_value, agent_value, registration in waiting:
        if not tenant_value or not agent_value or not isinstance(registration, dict):
            continue
        signal_type = registration.get("signal_type")
        step_id = registration.get("step_id")
        if not signal_type or not step_id:
            continue

        # Atomic consume: DELETE ... RETURNING — exactly one drainer wins the
        # row; tenant + recipient + thread(=run) + type all must match.
        async with tenant_scoped_session(tenant_value, session_factory=session_factory) as session:
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
                        "tenant": uuid.UUID(tenant_value),
                        "agent": agent_value,
                        "thread": str(run_id),
                        "stype": signal_type,
                    },
                )
            ).first()
        if consumed is None:
            continue  # no matching signal yet — keep waiting

        signal_id, signal_content = consumed
        # Mark the waiting step done with the signal payload as its output,
        # then clear the registration — the engine replays it on resume.
        async with tenant_scoped_session(tenant_value, session_factory=session_factory) as session:
            from app.models.workflow import WorkflowStep

            step_row = (
                await session.execute(
                    select(WorkflowStep).where(WorkflowStep.run_id == run_id, WorkflowStep.step_id == step_id)
                )
            ).scalar_one_or_none()
            if step_row is not None:
                step_row.status = "done"
                step_row.result_ref = json.dumps(
                    {"signal": signal_content, "signal_type": signal_type}, ensure_ascii=False
                )
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
            metadata = dict(task.metadata_json or {})
            metadata.pop("waiting_for_signal", None)
            task.metadata_json = metadata

        try:
            outcome = await runtime.resume_run(run_id, tenant_id=uuid.UUID(tenant_value), leaf_executor=leaf_executor)
            resumed.append(SignalResumedRun(run_id=run_id, signal_id=signal_id, outcome=outcome))
        except Exception as exc:
            logger.error("[WorkflowSignal] resume of run %s after signal failed: %s", run_id, exc, exc_info=True)
    return resumed
