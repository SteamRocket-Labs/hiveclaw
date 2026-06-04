from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowLeafCall, WorkflowQuota, WorkflowStep
from app.services.workflow_runtime_service import WorkflowRunNotFound, WorkflowRuntimeService

logger = logging.getLogger(__name__)


class WorkflowOpsConflict(RuntimeError):
    """Operator command is valid, but the run is not in a quiescent state."""


class WorkflowOpsService:
    """Admin repair / inspection shell for workflow runs.

    These commands operate on the canonical RuntimeTask + workflow journal
    tables. They never execute leaves directly; replay only rewinds journal
    rows and leaves actual execution to the normal resume path.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory
        self._runtime = WorkflowRuntimeService(session_factory=session_factory)

    def _session(self, tenant_id: uuid.UUID | str | None):
        return tenant_scoped_session(str(tenant_id) if tenant_id else None, session_factory=self._session_factory)

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, list):
            return [WorkflowOpsService._jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(key): WorkflowOpsService._jsonable(item) for key, item in value.items()}
        return value

    @staticmethod
    def _task_payload(task: RuntimeTask) -> dict[str, Any]:
        metadata = task.metadata_json or {}
        return {
            "run_id": str(task.id),
            "task_type": task.task_type,
            "status": task.status,
            "tenant_id": metadata.get("tenant_id"),
            "agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
            "definition_source": metadata.get("definition_source"),
            "definition_hash": metadata.get("definition_hash"),
            "args_hash": metadata.get("args_hash"),
            "confirmed_plan_id": metadata.get("confirmed_plan_id"),
            "last_outcome_reason": metadata.get("last_outcome_reason"),
            "metadata": WorkflowOpsService._jsonable(metadata),
        }

    @staticmethod
    def _step_payload(step: WorkflowStep) -> dict[str, Any]:
        return {
            "id": str(step.id),
            "step_id": step.step_id,
            "step_type": step.step_type,
            "status": step.status,
            "input_hash": step.input_hash,
            "definition_hash": step.definition_hash,
            "result_ref": step.result_ref,
            "error": step.error,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "finished_at": step.finished_at.isoformat() if step.finished_at else None,
        }

    @staticmethod
    def _leaf_payload(leaf: WorkflowLeafCall) -> dict[str, Any]:
        return {
            "id": str(leaf.id),
            "step_id": leaf.step_id,
            "leaf_id": leaf.leaf_id,
            "status": leaf.status,
            "input_hash": leaf.input_hash,
            "definition_hash": leaf.definition_hash,
            "idempotency_key": leaf.idempotency_key,
            "result_ref": leaf.result_ref,
            "token_usage": WorkflowOpsService._jsonable(leaf.token_usage or {}),
            "error": leaf.error,
            "started_at": leaf.started_at.isoformat() if leaf.started_at else None,
            "finished_at": leaf.finished_at.isoformat() if leaf.finished_at else None,
        }

    @staticmethod
    async def _audit(
        action: str,
        *,
        tenant_id: uuid.UUID | str,
        run_id: uuid.UUID | str,
        reason: str,
        extra: dict | None = None,
    ) -> None:
        """Audit trail for destructive admin operations — same fail-soft
        contract as the P9 run-lifecycle `_audit` (never blocks the repair)."""
        try:
            from app.services.audit_logger import write_audit_log

            details = {"tenant_id": str(tenant_id), "run_id": str(run_id), "reason": reason}
            if extra:
                details.update(extra)
            await write_audit_log(action, details=details)
        except Exception as exc:
            logger.warning("[WorkflowOps] audit write %s failed (non-fatal): %s", action, exc)

    async def _load_task(self, run_id: uuid.UUID | str, *, tenant_id: uuid.UUID | str) -> RuntimeTask:
        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(str(run_id))))
            ).scalar_one_or_none()
        if task is None or task.task_type != "workflow":
            raise WorkflowRunNotFound(str(run_id))
        metadata_tenant = (task.metadata_json or {}).get("tenant_id")
        if metadata_tenant and str(metadata_tenant) != str(tenant_id):
            raise WorkflowRunNotFound(f"workflow run {run_id} is not in tenant {tenant_id}")
        return task

    async def inspect_run(self, run_id: uuid.UUID | str, *, tenant_id: uuid.UUID | str) -> dict[str, Any]:
        task = await self._load_task(run_id, tenant_id=tenant_id)
        async with self._session(tenant_id) as session:
            steps = (
                (
                    await session.execute(
                        select(WorkflowStep)
                        .where(WorkflowStep.run_id == uuid.UUID(str(run_id)))
                        .order_by(WorkflowStep.created_at, WorkflowStep.step_id)
                    )
                )
                .scalars()
                .all()
            )
            quota = (
                await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == uuid.UUID(str(run_id))))
            ).scalar_one_or_none()
        payload = self._task_payload(task)
        payload["steps"] = [self._step_payload(step) for step in steps]
        payload["quota"] = (
            {
                "allocated_tokens": quota.allocated_tokens,
                "consumed_tokens": quota.consumed_tokens,
                "updated_at": quota.updated_at.isoformat() if quota.updated_at else None,
            }
            if quota
            else None
        )
        return payload

    async def export_journal(self, run_id: uuid.UUID | str, *, tenant_id: uuid.UUID | str) -> dict[str, Any]:
        run = await self.inspect_run(run_id, tenant_id=tenant_id)
        async with self._session(tenant_id) as session:
            leaves = (
                (
                    await session.execute(
                        select(WorkflowLeafCall)
                        .where(WorkflowLeafCall.run_id == uuid.UUID(str(run_id)))
                        .order_by(WorkflowLeafCall.step_id, WorkflowLeafCall.leaf_id)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "run": run,
            "steps": run["steps"],
            "leaf_calls": [self._leaf_payload(leaf) for leaf in leaves],
        }

    async def cancel_run(
        self, run_id: uuid.UUID | str, *, tenant_id: uuid.UUID | str, reason: str = "cancelled by admin"
    ) -> dict[str, Any]:
        await self._runtime.kill_run(run_id, tenant_id=tenant_id)
        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(str(run_id))))
            ).scalar_one()
            metadata = dict(task.metadata_json or {})
            metadata["admin_cancel_reason"] = reason
            task.metadata_json = metadata
        await self._audit("workflow_admin_cancelled", tenant_id=tenant_id, run_id=run_id, reason=reason)
        return {"run_id": str(run_id), "tenant_id": str(tenant_id), "status": "killed", "reason": reason}

    async def force_suspend_run(
        self, run_id: uuid.UUID | str, *, tenant_id: uuid.UUID | str, reason: str = "force suspended by admin"
    ) -> dict[str, Any]:
        await self._load_task(run_id, tenant_id=tenant_id)
        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(str(run_id))))
            ).scalar_one()
            task.status = "suspended"
            metadata = dict(task.metadata_json or {})
            metadata["admin_force_suspend_reason"] = reason
            task.metadata_json = metadata
        await self._audit("workflow_admin_force_suspended", tenant_id=tenant_id, run_id=run_id, reason=reason)
        return {"run_id": str(run_id), "tenant_id": str(tenant_id), "status": "suspended", "reason": reason}

    async def replay_from_step(
        self,
        run_id: uuid.UUID | str,
        *,
        tenant_id: uuid.UUID | str,
        step_id: str,
        reason: str = "replay requested by admin",
    ) -> dict[str, Any]:
        task = await self._load_task(run_id, tenant_id=tenant_id)
        definition = (task.metadata_json or {}).get("definition_json") or {}
        steps = definition.get("steps") if isinstance(definition, dict) else None
        if not isinstance(steps, list):
            raise WorkflowRunNotFound(f"workflow run {run_id} has no archived step list")
        step_ids = [str(step.get("id")) for step in steps if isinstance(step, dict) and step.get("id")]
        if step_id not in step_ids:
            raise WorkflowRunNotFound(f"step {step_id!r} not found in workflow run {run_id}")
        replay_step_ids = step_ids[step_ids.index(step_id) :]
        run_uuid = uuid.UUID(str(run_id))

        async with self._session(tenant_id) as session:
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_uuid))).scalar_one()
            if task.status == "running":
                raise WorkflowOpsConflict(
                    "workflow run is still running; force-suspend it and wait for the current step boundary before replay"
                )
            running_steps = (
                (
                    await session.execute(
                        select(WorkflowStep.step_id).where(
                            WorkflowStep.run_id == run_uuid,
                            WorkflowStep.status == "running",
                        )
                    )
                )
                .scalars()
                .all()
            )
            running_leaves = (
                (
                    await session.execute(
                        select(WorkflowLeafCall.step_id, WorkflowLeafCall.leaf_id).where(
                            WorkflowLeafCall.run_id == run_uuid,
                            WorkflowLeafCall.status == "running",
                        )
                    )
                )
                .all()
            )
            if running_steps or running_leaves:
                raise WorkflowOpsConflict(
                    "workflow run has in-flight journal rows; wait for force-suspend to quiesce before replay"
                )
            # P10 invariant carried into admin surgery: a step parked as
            # unknown_requires_reconciliation is an EXTERNAL effect whose
            # outcome is unknown. Deleting its row would erase the anchor and
            # — with the gate's checkpoint approval still persisted — resume
            # would silently re-fire the external action. Refuse until the
            # operator has reconciled it (see runbook).
            unreconciled = (
                (
                    await session.execute(
                        select(WorkflowStep.step_id).where(
                            WorkflowStep.run_id == run_uuid,
                            WorkflowStep.step_id.in_(replay_step_ids),
                            WorkflowStep.status == "unknown_requires_reconciliation",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if unreconciled:
                raise WorkflowOpsConflict(
                    f"replay range sweeps unreconciled external steps {sorted(unreconciled)}; "
                    "reconcile them first (mark done if the effect happened, delete the row if it "
                    "verifiably did not), then replay"
                )

            # Refund what the soon-to-be-deleted leaf rows already settled into
            # the quota — without this the rerun double-charges and can wedge
            # the run on ``quota``. Only fanout leaves carry row-level metering;
            # plain agent_step consumption has no rows and stays charged.
            doomed_leaves = (
                (
                    await session.execute(
                        select(WorkflowLeafCall).where(
                            WorkflowLeafCall.run_id == run_uuid,
                            WorkflowLeafCall.step_id.in_(replay_step_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            refund = sum(int((leaf.token_usage or {}).get("total") or 0) for leaf in doomed_leaves)
            if refund > 0:
                quota = (
                    await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == run_uuid))
                ).scalar_one_or_none()
                if quota is not None:
                    quota.consumed_tokens = max(0, (quota.consumed_tokens or 0) - refund)
            await session.execute(
                delete(WorkflowLeafCall).where(
                    WorkflowLeafCall.run_id == run_uuid,
                    WorkflowLeafCall.step_id.in_(replay_step_ids),
                )
            )
            await session.execute(
                delete(WorkflowStep).where(
                    WorkflowStep.run_id == run_uuid,
                    WorkflowStep.step_id.in_(replay_step_ids),
                )
            )
            task.status = "running"
            metadata = dict(task.metadata_json or {})
            metadata["admin_replay_from_step"] = step_id
            metadata["admin_replay_reason"] = reason
            task.metadata_json = metadata
        await self._audit(
            "workflow_admin_replay_from_step",
            tenant_id=tenant_id,
            run_id=run_id,
            reason=reason,
            extra={"replay_from_step": step_id, "rewound_step_ids": replay_step_ids},
        )
        return {
            "run_id": str(run_id),
            "tenant_id": str(tenant_id),
            "status": "running",
            "replay_from_step": step_id,
            "reason": reason,
        }
