"""WorkflowRuntimeService (§9 P3): start / resume / kill / load + PG journal.

The Imperative Shell around the pure engine (§3.3):

* a workflow run IS a ``RuntimeTask(task_type="workflow")`` — run metadata
  (definition archive, args, hashes, tenant mirror) lives in
  ``metadata_json``; no fifth background ledger;
* the step journal is the ``workflow_steps`` table (FORCEd RLS — every
  session here goes through ``tenant_scoped_session``);
* kill is persisted state: the engine polls ``should_continue`` before every
  step, which reads the run row, so a kill lands mid-run;
* startup resume (``resume_pending_runs``) follows the
  ``resume_persisted_async_delegations`` precedent: scan running/suspended
  workflow runs and drive the recoverable ones; killed runs are never
  auto-resumed.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.coordination_wiring import gateway_scope
from app.config import get_settings
from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowQuota, WorkflowStep
from app.runtime.dynamic_workflow import (
    attach_workflow_decision_outcome,
    build_dynamic_workflow_repair_plan,
    summarize_dynamic_workflow_outcome,
)
from app.services.channel_delivery_service import ChannelDeliveryService
from app.services.chat_message_parts import build_session_native_event
from app.services.chat_transcript import append_session_event
from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, RuntimeBudgetRunCreate, RuntimeBudgetService
from app.services.runtime_task_service import list_active_runtime_task_records
from app.runtime.workflow_admission import (
    AdmissionLimits,
    WorkflowAdmissionError,
    admit_workflow,
    normalize_workflow_args,
)
from app.runtime.workflow_compiler import CompiledWorkflow, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash
from app.runtime.workflow_engine import (
    GateDecision,
    LeafExecutor,
    LeafOutcome,
    LeafRecord,
    LeafRequest,
    StepRecord,
    WorkflowRunOutcome,
    execute_workflow,
)

logger = logging.getLogger(__name__)

_RESUMABLE_STATUSES = ("running", "suspended")
_WORKFLOW_RUNTIME_ACTION_BY_RUN_STATUS = {
    "running": "runtime_action_started",
    "completed": "runtime_action_completed",
    "failed": "runtime_action_failed",
    "killed": "runtime_action_failed",
    "suspended": "runtime_action_blocked",
}


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError):
        return None


def _workflow_runtime_action_payload(
    *,
    run_id: str,
    status: str,
    parent_session_id: str | None,
    root_session_id: str | None,
    definition_source: str | None = None,
    definition_hash: str | None = None,
    reason: str | None = None,
    step_id: str | None = None,
    result_ref: str | None = None,
) -> dict[str, Any]:
    event_type = (
        "runtime_action_progress"
        if step_id
        else _WORKFLOW_RUNTIME_ACTION_BY_RUN_STATUS.get(status, "runtime_action_progress")
    )
    subject = f"Workflow step {step_id}" if step_id else "Workflow run"
    payload: dict[str, Any] = {
        "type": event_type,
        "status": status,
        "message": f"{subject} {status}",
        "action_kind": "workflow",
        "notification_source": "workflow",
        "tool_name": "start_workflow",
        "workflow_run_id": run_id,
        "runtime_task_id": run_id,
        "parent_session_id": parent_session_id,
        "root_session_id": root_session_id or parent_session_id,
        "reason": reason,
    }
    if step_id:
        payload["workflow_step_id"] = step_id
    if definition_source:
        payload["definition_source"] = definition_source
    if definition_hash:
        payload["definition_hash"] = definition_hash
    if result_ref is not None:
        payload["result_ref"] = result_ref
    return payload


class WorkflowRunNotFound(LookupError):
    pass


@dataclass(slots=True)
class WorkflowRunHandle:
    run_id: uuid.UUID
    outcome: WorkflowRunOutcome


@dataclass(slots=True)
class LoadedWorkflowRun:
    task: RuntimeTask
    steps: list[WorkflowStep] = field(default_factory=list)
    leaf_calls: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class WorkflowRunSummary:
    """One row of the agent's run history (asset view): the archived run plus
    step aggregates and promote provenance."""

    task: RuntimeTask
    step_counts: dict[str, int] = field(default_factory=dict)
    promoted_definition_id: uuid.UUID | None = None


@dataclass(slots=True)
class ResumedRun:
    run_id: uuid.UUID
    outcome: WorkflowRunOutcome


class _PGWorkflowJournal:
    """Real-PG step journal bound to one (tenant, run).

    When an ``agent_id`` is bound, every step-status write is MIRRORED onto
    the agent's work ledger (§10 invariant ④ — one-way observation surface:
    mirror failures log and never block; the engine never reads it back)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        tenant_id: uuid.UUID | str,
        *,
        agent_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        parent_session_id: uuid.UUID | str | None = None,
        root_session_id: uuid.UUID | str | None = None,
        user_id: uuid.UUID | str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._agent_id = agent_id
        self._run_id = run_id
        self._parent_session_id = str(parent_session_id) if parent_session_id else None
        self._root_session_id = str(root_session_id) if root_session_id else None
        self._user_id = str(user_id) if user_id else None
        self._step_started_at: dict[tuple[str, str], float] = {}

    def _mirror_step(self, step_id: str, status: str) -> None:
        if self._agent_id is None or self._run_id is None:
            return
        try:
            from app.services.agent_work_ledger import upsert_agent_work_ledger_todo

            ledger_status = {
                "running": "in_progress",
                "done": "completed",
                "skipped": "completed",
                "failed": "pending",
                "suspended": "pending",
            }.get(status, "pending")
            upsert_agent_work_ledger_todo(
                agent_id=self._agent_id,
                title=f"workflow step: {step_id}",
                status=ledger_status,
                runtime_task_id=self._run_id,
            )
        except Exception as exc:
            logger.warning("[Workflow] ledger mirror for step %s failed (non-fatal): %s", step_id, exc)

    def _session(self):
        return tenant_scoped_session(str(self._tenant_id), session_factory=self._session_factory)

    @staticmethod
    def _decode_output(result_ref: str | None) -> Any:
        if result_ref is None:
            return None
        try:
            return json.loads(result_ref)
        except (TypeError, ValueError):
            return result_ref

    async def load_steps(self, run_id: str) -> dict[str, StepRecord]:
        async with self._session() as session:
            rows = (
                (await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == uuid.UUID(run_id))))
                .scalars()
                .all()
            )
        return {
            row.step_id: StepRecord(
                step_id=row.step_id,
                status=row.status,
                input_hash=row.input_hash,
                definition_hash=row.definition_hash,
                output=self._decode_output(row.result_ref),
                result_ref=row.result_ref,
                error=row.error,
            )
            for row in rows
        }

    async def _upsert(self, run_id: str, step_id: str, **values: Any) -> None:
        async with self._session() as session:
            row = (
                await session.execute(
                    select(WorkflowStep).where(
                        WorkflowStep.run_id == uuid.UUID(run_id), WorkflowStep.step_id == step_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = WorkflowStep(tenant_id=uuid.UUID(str(self._tenant_id)), run_id=uuid.UUID(run_id), step_id=step_id)
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)

    async def _append_step_event(
        self,
        run_id: str,
        step_id: str,
        *,
        status: str,
        reason: str | None = None,
        result_ref: str | None = None,
    ) -> None:
        if not self._parent_session_id or self._agent_id is None:
            return
        payload = {
            "type": "workflow_step",
            "status": status,
            "message": f"Workflow step {step_id} {status}",
            "workflow_run_id": run_id,
            "workflow_step_id": step_id,
            "runtime_task_id": run_id,
            "parent_session_id": self._parent_session_id,
            "root_session_id": self._root_session_id or self._parent_session_id,
            "reason": reason,
            "result_ref": result_ref,
        }
        runtime_payload = _workflow_runtime_action_payload(
            run_id=run_id,
            status=status,
            parent_session_id=self._parent_session_id,
            root_session_id=self._root_session_id or self._parent_session_id,
            reason=reason,
            step_id=step_id,
            result_ref=result_ref,
        )
        payloads = (payload, runtime_payload)
        try:
            async with self._session() as session:
                for event_payload in payloads:
                    event = build_session_native_event(event_payload)
                    await append_session_event(
                        db=session,
                        agent_id=self._agent_id,
                        tenant_id=self._tenant_id,
                        session_id=self._parent_session_id,
                        actor_type="system",
                        event_type=str(event_payload["type"]),
                        role="system",
                        user_id=self._user_id,
                        run_id=run_id,
                        runtime_task_id=run_id,
                        root_session_id=self._root_session_id or self._parent_session_id,
                        parent_session_id=self._parent_session_id,
                        content=json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
                        source="workflow_runtime",
                        parts=[event["part"]] if isinstance(event.get("part"), dict) else None,
                        metadata={"source": "workflow_runtime", **event_payload},
                    )
                await session.commit()
        except Exception as exc:
            logger.warning("[Workflow] session event projection for step %s failed (non-fatal): %s", step_id, exc)

    async def record_step_start(
        self, run_id: str, step_id: str, *, step_type: str, input_hash: str | None, definition_hash: str
    ) -> None:
        from sqlalchemy import func
        from app.services.workflow_metrics import record_workflow_step

        self._step_started_at[(run_id, step_id)] = time.monotonic()
        await self._upsert(
            run_id,
            step_id,
            step_type=step_type,
            status="running",
            input_hash=input_hash,
            definition_hash=definition_hash,
            started_at=func.now(),
            error=None,
        )
        record_workflow_step("running")
        self._mirror_step(step_id, "running")
        await self._append_step_event(run_id, step_id, status="running")

    def _observe_step_finished(self, run_id: str, step_id: str, status: str) -> None:
        from app.services.workflow_metrics import observe_workflow_step_duration, record_workflow_step

        started = self._step_started_at.pop((run_id, step_id), None)
        if started is not None:
            observe_workflow_step_duration(status, time.monotonic() - started)
        record_workflow_step(status)

    async def record_step_done(self, run_id: str, step_id: str, *, output: Any, result_ref: str | None) -> None:
        from sqlalchemy import func

        encoded = result_ref if result_ref is not None else json.dumps(output, ensure_ascii=False, sort_keys=True)
        await self._upsert(run_id, step_id, status="done", result_ref=encoded, finished_at=func.now())
        self._observe_step_finished(run_id, step_id, "done")
        self._mirror_step(step_id, "done")
        await self._append_step_event(run_id, step_id, status="done", result_ref=encoded)

    async def record_step_failed(self, run_id: str, step_id: str, *, error: str) -> None:
        from sqlalchemy import func

        await self._upsert(run_id, step_id, status="failed", error=error[:4000], finished_at=func.now())
        self._observe_step_finished(run_id, step_id, "failed")
        self._mirror_step(step_id, "failed")
        await self._append_step_event(run_id, step_id, status="failed", reason=error[:4000])

    async def record_step_skipped(self, run_id: str, step_id: str, *, definition_hash: str) -> None:
        from app.services.workflow_metrics import record_workflow_step

        await self._upsert(run_id, step_id, status="skipped", definition_hash=definition_hash)
        record_workflow_step("skipped")
        await self._append_step_event(run_id, step_id, status="skipped")

    async def record_step_suspended(self, run_id: str, step_id: str, *, reason: str) -> None:
        await self._upsert(run_id, step_id, status="suspended", error=reason[:4000])
        self._observe_step_finished(run_id, step_id, "suspended")
        self._mirror_step(step_id, "suspended")
        await self._append_step_event(run_id, step_id, status="suspended", reason=reason[:4000])

    # ── leaf-level journal (v1 decision 6) ───────────────────────

    async def load_leaf_calls(self, run_id: str, step_id: str) -> dict[str, LeafRecord]:
        from app.models.workflow import WorkflowLeafCall

        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(WorkflowLeafCall).where(
                            WorkflowLeafCall.run_id == uuid.UUID(run_id),
                            WorkflowLeafCall.step_id == step_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {
            row.leaf_id: LeafRecord(
                leaf_id=row.leaf_id,
                status=row.status,
                input_hash=row.input_hash,
                definition_hash=row.definition_hash,
                output=self._decode_output(row.result_ref),
                result_ref=row.result_ref,
                error=row.error,
            )
            for row in rows
        }

    async def _upsert_leaf(self, run_id: str, step_id: str, leaf_id: str, **values: Any) -> None:
        from app.models.workflow import WorkflowLeafCall

        async with self._session() as session:
            row = (
                await session.execute(
                    select(WorkflowLeafCall).where(
                        WorkflowLeafCall.run_id == uuid.UUID(run_id),
                        WorkflowLeafCall.step_id == step_id,
                        WorkflowLeafCall.leaf_id == leaf_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = WorkflowLeafCall(
                    tenant_id=uuid.UUID(str(self._tenant_id)),
                    run_id=uuid.UUID(run_id),
                    step_id=step_id,
                    leaf_id=leaf_id,
                )
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)

    async def record_leaf_start(
        self,
        run_id: str,
        step_id: str,
        leaf_id: str,
        *,
        input_hash: str | None,
        definition_hash: str,
        idempotency_key: str,
    ) -> None:
        from sqlalchemy import func
        from app.services.workflow_metrics import record_workflow_leaf_call

        await self._upsert_leaf(
            run_id,
            step_id,
            leaf_id,
            status="running",
            input_hash=input_hash,
            definition_hash=definition_hash,
            idempotency_key=idempotency_key,
            started_at=func.now(),
            error=None,
        )
        record_workflow_leaf_call("running")

    async def record_leaf_done(self, run_id: str, step_id: str, leaf_id: str, *, output: Any, tokens_used: int) -> None:
        from sqlalchemy import func
        from app.services.workflow_metrics import record_workflow_leaf_call

        await self._upsert_leaf(
            run_id,
            step_id,
            leaf_id,
            status="done",
            result_ref=json.dumps(output, ensure_ascii=False, sort_keys=True),
            token_usage={"total": tokens_used},
            finished_at=func.now(),
        )
        record_workflow_leaf_call("done")

    async def record_leaf_failed(self, run_id: str, step_id: str, leaf_id: str, *, error: str) -> None:
        from sqlalchemy import func
        from app.services.workflow_metrics import record_workflow_leaf_call

        await self._upsert_leaf(run_id, step_id, leaf_id, status="failed", error=error[:4000], finished_at=func.now())
        record_workflow_leaf_call("failed")


class PGQuotaReserver:
    """Run-quota envelope on real PG (§9 P5): conditional pre-deduction under
    a Postgres advisory lock, settled with actual usage after each leaf."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        tenant_id: uuid.UUID | str,
        *,
        estimate: int,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._estimate = estimate

    def _session(self):
        return tenant_scoped_session(str(self._tenant_id), session_factory=self._session_factory)

    async def reserve(self, run_id: str) -> bool:
        from sqlalchemy import text

        async with self._session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:rid))"), {"rid": str(run_id)})
            row = (
                await session.execute(
                    text(
                        "UPDATE workflow_quotas SET consumed_tokens = consumed_tokens + :est, "
                        "updated_at = now() WHERE run_id = :rid "
                        "AND consumed_tokens + :est <= allocated_tokens RETURNING id"
                    ),
                    {"est": self._estimate, "rid": uuid.UUID(str(run_id))},
                )
            ).scalar_one_or_none()
        if row is None:
            from app.services.workflow_metrics import record_workflow_quota_denial

            record_workflow_quota_denial()
        return row is not None

    async def settle(self, run_id: str, actual_tokens: int) -> None:
        from sqlalchemy import text

        async with self._session() as session:
            await session.execute(
                text(
                    "UPDATE workflow_quotas SET consumed_tokens = consumed_tokens - :est + :actual, "
                    "updated_at = now() WHERE run_id = :rid"
                ),
                {"est": self._estimate, "actual": actual_tokens, "rid": uuid.UUID(str(run_id))},
            )


class WorkflowRunLease:
    """A held per-run advisory lock on its own dedicated connection.

    Session-level ``pg_advisory_lock`` semantics give exactly what worker
    ownership needs: the lock dies WITH the connection, so a crashed worker
    releases its runs automatically and a healthy peer can take over."""

    def __init__(self, connection, key: int) -> None:
        self._connection = connection
        self._key = key

    async def release(self) -> None:
        from sqlalchemy import text

        try:
            await self._connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
        finally:
            await self._connection.close()


class PGRunLeaseManager:
    """Cross-worker run ownership (§9 P10): one worker resumes a run at a time."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        from app.database import engine as default_engine

        bind = None
        if session_factory is not None:
            bind = session_factory.kw.get("bind")
        self._engine = bind if bind is not None else default_engine

    @staticmethod
    def _key(run_id: uuid.UUID | str) -> int:
        import zlib

        # Stable 32-bit key in advisory-lock space, namespaced for workflows.
        return zlib.crc32(f"wf_run:{run_id}".encode()) & 0x7FFFFFFF

    async def try_acquire(self, run_id: uuid.UUID | str) -> WorkflowRunLease | None:
        from sqlalchemy import text

        connection = await self._engine.connect()
        try:
            got = (await connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key(run_id)})).scalar()
        except Exception:
            await connection.close()
            raise
        if not got:
            await connection.close()
            return None
        return WorkflowRunLease(connection, self._key(run_id))


class CheckpointGateDecider:
    """gate_step → CoordinationCheckpoint (§9 P7).

    One checkpoint per (run, step), keyed through checkpoint.metadata; the
    verdict maps onto the engine's tri-state GateDecision. Approval flips the
    checkpoint (approve_workflow_gate) and an EXPLICIT resume re-runs the
    step — exactly the Plan-Mode-style human-in-the-loop boundary."""

    _ACTION_PREFIX = "workflow_gate"

    def __init__(self, runtime=None, *, approver_id: str = "owner", session_factory=None) -> None:
        self._runtime = runtime
        self._approver_id = approver_id
        self._session_factory = session_factory

    def _find(self, run_id: str, step_id: str):
        if self._runtime is None:
            return None
        checkpoints = getattr(self._runtime, "_checkpoints", {})
        for checkpoint in checkpoints.values():
            metadata = checkpoint.metadata or {}
            if metadata.get("workflow_run_id") == str(run_id) and metadata.get("workflow_step_id") == step_id:
                return checkpoint
        return None

    async def _tenant_for_run(self, run_id: str) -> str | None:
        from app.services.tenant_resolver import resolve_tenant_for_runtime_task

        tenant_id = await resolve_tenant_for_runtime_task(
            run_id,
            session_factory=self._session_factory,
        )
        return str(tenant_id) if tenant_id else None

    async def _find_pg(self, run_id: str, step_id: str):
        from app.models.coordination import CoordinationCheckpoint

        tenant_value = await self._tenant_for_run(run_id)
        if not tenant_value:
            return None, None
        async with tenant_scoped_session(tenant_value, session_factory=self._session_factory) as session:
            checkpoint = (
                await session.execute(
                    select(CoordinationCheckpoint).where(
                        CoordinationCheckpoint.extra_metadata["workflow_run_id"].as_string() == str(run_id),
                        CoordinationCheckpoint.extra_metadata["workflow_step_id"].as_string() == step_id,
                    )
                )
            ).scalar_one_or_none()
        return tenant_value, checkpoint

    async def check(self, run_id: str, step_id: str, *, reason: str) -> GateDecision:
        if self._runtime is None:
            return await self._check_pg(run_id, step_id, reason=reason)

        checkpoint = self._find(run_id, step_id)
        if checkpoint is None:
            from datetime import UTC, datetime, timedelta

            checkpoint = self._runtime.create_checkpoint(
                action=f"{self._ACTION_PREFIX}:{reason}"[:200],
                approver_id=self._approver_id,
                escalation_chain=[],
                deadline_at=datetime.now(UTC) + timedelta(hours=24),
            )
            checkpoint.metadata["workflow_run_id"] = str(run_id)
            checkpoint.metadata["workflow_step_id"] = step_id
            return GateDecision(pending=True, reason=f"checkpoint {checkpoint.id} awaiting approval")
        if checkpoint.status == "approved":
            return GateDecision(approved=True)
        if checkpoint.status in ("rejected", "expired"):
            return GateDecision(rejected=True, reason=f"checkpoint {checkpoint.id} {checkpoint.status}")
        return GateDecision(pending=True, reason=f"checkpoint {checkpoint.id} awaiting approval")

    async def _check_pg(self, run_id: str, step_id: str, *, reason: str) -> GateDecision:
        from datetime import UTC, datetime, timedelta

        from app.models.coordination import CoordinationCheckpoint

        tenant_value, checkpoint = await self._find_pg(run_id, step_id)
        if tenant_value is None:
            return GateDecision(pending=True, reason=f"gate step {step_id!r} has no tenant-bound run metadata")
        if checkpoint is None:
            async with tenant_scoped_session(tenant_value, session_factory=self._session_factory) as session:
                checkpoint = CoordinationCheckpoint(
                    tenant_id=uuid.UUID(str(tenant_value)),
                    action=f"{self._ACTION_PREFIX}:{reason}"[:255],
                    approver_id=self._approver_id,
                    escalation_chain=[],
                    deadline_at=datetime.now(UTC) + timedelta(hours=24),
                    current_approver_id=self._approver_id,
                    status="pending",
                    extra_metadata={"workflow_run_id": str(run_id), "workflow_step_id": step_id},
                )
                session.add(checkpoint)
                await session.flush()
                checkpoint_id = checkpoint.id
            return GateDecision(pending=True, reason=f"checkpoint {checkpoint_id} awaiting approval")

        if checkpoint.status == "approved":
            return GateDecision(approved=True)
        if checkpoint.status in ("rejected", "expired"):
            return GateDecision(rejected=True, reason=f"checkpoint {checkpoint.id} {checkpoint.status}")
        return GateDecision(pending=True, reason=f"checkpoint {checkpoint.id} awaiting approval")

    def approve(self, run_id: str, step_id: str) -> bool:
        checkpoint = self._find(run_id, step_id)
        if checkpoint is None:
            return False
        checkpoint.status = "approved"
        return True

    def reject(self, run_id: str, step_id: str) -> bool:
        checkpoint = self._find(run_id, step_id)
        if checkpoint is None:
            return False
        checkpoint.status = "rejected"
        return True


class WorkflowRuntimeService:
    """start / resume / kill / load — every DB touch tenant-scoped."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        gate_decider: CheckpointGateDecider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gate_decider = (
            gate_decider if gate_decider is not None else CheckpointGateDecider(session_factory=session_factory)
        )
        self._lease_manager = PGRunLeaseManager(session_factory)
        self._draining = False

    def request_drain(self) -> None:
        """Graceful drain (§9 P10): stop taking NEW leaves at the next step
        boundary; unfinished runs stay resumable instead of killed."""
        self._draining = True

    def clear_drain(self) -> None:
        """Reset a previous graceful-drain request before a fresh worker loop starts."""
        self._draining = False

    @property
    def gate_decider(self) -> CheckpointGateDecider:
        return self._gate_decider

    async def _record_resume_at(self, run_id: uuid.UUID, tenant_id: uuid.UUID, resume_at) -> None:
        """Equivalent scheduling record (§9 P7): the suspended run carries its
        wake time in metadata; the startup scan resumes it once due. The once
        trigger binding lands in P8 (§6.2)."""
        async with self._session(tenant_id) as session:
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
            metadata = dict(task.metadata_json or {})
            metadata["resume_at"] = resume_at.isoformat()
            task.metadata_json = metadata

    def _session(self, tenant_id: uuid.UUID | str | None):
        return tenant_scoped_session(str(tenant_id) if tenant_id else None, session_factory=self._session_factory)

    # ── run lifecycle ────────────────────────────────────────────

    async def start_run(
        self,
        *,
        tenant_id: uuid.UUID,
        definition_data: dict,
        args: dict,
        leaf_executor: LeafExecutor,
        definition_source: str = "ephemeral",
        agent_id: uuid.UUID | None = None,
        user_id: uuid.UUID | str | None = None,
        confirmed_plan_id: uuid.UUID | str | None = None,
        allowed_leaves: set[str] | None = None,
        run_id: uuid.UUID | None = None,
        delivery_target: dict[str, Any] | None = None,
        parent_session_id: uuid.UUID | str | None = None,
        root_session_id: uuid.UUID | str | None = None,
        run_metadata: dict[str, Any] | None = None,
        enqueue_only: bool = False,
        budget_run_id: uuid.UUID | str | None = None,
        budget_service: RuntimeBudgetService | None = None,
    ) -> WorkflowRunHandle:
        if not get_settings().WORKFLOW_RUNTIME_ENABLED:
            raise WorkflowAdmissionError("workflow runtime disabled by feature flag WORKFLOW_RUNTIME_ENABLED")
        compiled = compile_workflow(definition_data, known_leaves=allowed_leaves)
        args = normalize_workflow_args(compiled, args)
        limits = AdmissionLimits.from_settings(get_settings())
        admission = admit_workflow(compiled, args=args, limits=limits, allowed_leaves=allowed_leaves)

        args_hash = compute_definition_hash(args)
        # A caller may pre-generate the id so run-scoped artifacts can land
        # BEFORE execution starts.
        run_id = run_id or uuid.uuid4()
        budget_uuid = _uuid_or_none(budget_run_id)
        budget_admission_status = "inherited" if budget_uuid is not None else None
        if budget_uuid is None:
            service = budget_service or RuntimeBudgetService(session_factory=self._session_factory)
            policy = await service.resolve_policy(
                RuntimeBudgetPolicyLookup(
                    tenant_id=tenant_id,
                    source="workflow",
                    profile="workflow",
                    agent_id=agent_id,
                )
            )
            budget_run = await service.create_run(
                RuntimeBudgetRunCreate(
                    tenant_id=tenant_id,
                    root_run_kind="workflow_run",
                    root_run_key=f"workflow:{run_id}",
                    source="workflow",
                    profile="workflow",
                    policy_id=getattr(policy, "id", None),
                    root_runtime_task_id=run_id,
                    root_session_id=str(root_session_id or parent_session_id or ""),
                    root_agent_id=agent_id,
                    root_user_id=_uuid_or_none(user_id),
                    enforcement_mode=str(getattr(policy, "enforcement_mode", None) or "enforce"),
                    fail_mode=str(getattr(policy, "fail_mode", None) or "fail_closed"),
                    max_tokens=getattr(policy, "max_tokens", None),
                    max_cache_miss_tokens=getattr(policy, "max_cache_miss_tokens", None),
                    max_subagents=getattr(policy, "max_subagents", None),
                    max_team_sessions=getattr(policy, "max_team_sessions", None),
                    max_delegations=getattr(policy, "max_delegations", None),
                    max_background_tasks=getattr(policy, "max_background_tasks", None),
                    max_continuation_wakes=getattr(policy, "max_continuation_wakes", None),
                    max_provider_calls=getattr(policy, "max_provider_calls", None),
                    max_failures=getattr(policy, "max_failures", None),
                    max_needs_reconciliation=getattr(policy, "max_needs_reconciliation", None),
                    max_child_failure_ratio=getattr(policy, "max_child_failure_ratio", None),
                    max_parent_invocations=getattr(policy, "max_parent_invocations", None),
                    policy_snapshot={
                        "policy_id": str(getattr(policy, "id", "")),
                        "scope_type": getattr(policy, "scope_type", None),
                        "source": getattr(policy, "source", None),
                        "profile": getattr(policy, "profile", None),
                        "max_team_sessions": getattr(policy, "max_team_sessions", None),
                        "policy_json": getattr(policy, "policy_json", None),
                    },
                )
            )
            budget_uuid = budget_run.id
            budget_admission_status = "root"
        parent_session_value = str(parent_session_id) if parent_session_id else None
        root_session_value = str(root_session_id or parent_session_id) if root_session_id or parent_session_id else None
        metadata_json = {
            "definition_source": definition_source,
            "definition_hash": compiled.definition_hash,
            "args_hash": args_hash,
            "confirmed_plan_id": str(confirmed_plan_id) if confirmed_plan_id else None,
            "tenant_id": str(tenant_id),
            "delivery_target_json": delivery_target,
            "parent_session_id": parent_session_value,
            "root_session_id": root_session_value,
            "user_id": str(user_id) if user_id else None,
            "session_bound": bool(parent_session_value),
            # Ephemeral archive (§3.1): the run must be replayable
            # without the original conversation.
            "definition_json": compiled.definition.canonical_dict()
            if not isinstance(definition_data, dict)
            else definition_data,
            "args": args,
            "budget_run_id": str(budget_uuid) if budget_uuid else None,
        }
        if run_metadata:
            metadata_json.update(run_metadata)
        async with self._session(tenant_id) as session:
            task = RuntimeTask(
                id=run_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="pending" if enqueue_only else "running",
                parent_agent_id=agent_id,
                parent_session_id=parent_session_value,
                child_session_id=parent_session_value,
                budget_run_id=budget_uuid,
                budget_admission_status=budget_admission_status,
                metadata_json=metadata_json,
            )
            session.add(task)
            session.add(
                WorkflowQuota(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    allocated_tokens=admission.budget_tokens,
                )
            )

        # §A-6: a run with no parent session (standalone / scheduled / admin /
        # heartbeat) gets a freshly bound ChatSession so it is session-visible
        # instead of a silent no-op; the "has parent session" path is unchanged.
        parent_session_value, root_session_value, user_session_value = await self._ensure_run_session(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            run_id=run_id,
            parent_session_id=parent_session_value,
            root_session_id=root_session_value,
        )

        await self._append_run_session_event(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_session_value or user_id,
            run_id=run_id,
            parent_session_id=parent_session_value,
            root_session_id=root_session_value,
            status="pending" if enqueue_only else "running",
            definition_source=definition_source,
            definition_hash=compiled.definition_hash,
        )

        from app.services.workflow_metrics import record_workflow_run_started

        record_workflow_run_started()
        if agent_id is not None:
            try:
                from app.services.agent_work_ledger import initialize_agent_work_ledger_artifact

                initialize_agent_work_ledger_artifact(agent_id=agent_id, source="workflow_run", runtime_task_id=run_id)
            except Exception as exc:
                logger.warning("[Workflow] ledger artifact init failed (non-fatal): %s", exc)
        await self._audit(
            "workflow_run_started",
            tenant_id=tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            definition_hash=compiled.definition_hash,
            extra={"definition_source": definition_source},
        )

        if enqueue_only:
            return WorkflowRunHandle(
                run_id=run_id,
                outcome=WorkflowRunOutcome(status="pending", reason="queued_for_worker_claim"),
            )

        outcome = await self._execute(
            compiled, run_id=run_id, tenant_id=tenant_id, args=args, leaf_executor=leaf_executor
        )
        return WorkflowRunHandle(run_id=run_id, outcome=outcome)

    async def _ensure_run_session(
        self,
        *,
        tenant_id: uuid.UUID | str,
        agent_id: uuid.UUID | None,
        user_id: uuid.UUID | str | None,
        run_id: uuid.UUID,
        parent_session_id: uuid.UUID | str | None,
        root_session_id: uuid.UUID | str | None,
    ) -> tuple[str | None, str | None, str | None]:
        """Make a headless workflow run session-visible (§A-6).

        A run started WITHOUT a parent session (standalone / scheduled / admin /
        heartbeat-triggered) would otherwise produce a RuntimeTask + journal that
        the session timeline never sees. Mirror the subagent precedent
        (``create_subagent_child_session``): bind the run to a freshly created
        ``ChatSession`` so its lifecycle/step events project into a real session.

        Returns the resolved ``(parent_session_id, root_session_id, user_id)`` —
        the existing "has parent session" path is preserved untouched, and a run
        with no agent to attach to stays unbound (returns the inputs)."""
        resolved_user = str(user_id) if user_id else None
        if parent_session_id or agent_id is None:
            return (
                str(parent_session_id) if parent_session_id else None,
                str(root_session_id or parent_session_id) if (root_session_id or parent_session_id) else None,
                resolved_user,
            )

        from app.models.agent import Agent
        from app.models.chat_session import ChatSession

        new_session_id = uuid.uuid4()
        try:
            async with self._session(tenant_id) as session:
                agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
                # Headless runs may carry no user; the agent always has an owning
                # principal (owner → creator → sponsor) — a valid users.id FK so
                # the multi-tenant ChatSession row is well-formed.
                if resolved_user is None and agent is not None:
                    owner = (
                        getattr(agent, "owner_user_id", None)
                        or getattr(agent, "creator_id", None)
                        or getattr(agent, "sponsor_user_id", None)
                    )
                    resolved_user = str(owner) if owner else None
                if resolved_user is None:
                    # No principal to attach the session to — leave it unbound
                    # rather than fabricate an FK; the journal still records it.
                    return (None, None, None)

                session.add(
                    ChatSession(
                        id=new_session_id,
                        agent_id=agent_id,
                        tenant_id=uuid.UUID(str(tenant_id)) if tenant_id else None,
                        user_id=uuid.UUID(resolved_user),
                        title="Workflow run",
                        source_channel="workflow",
                        session_kind="workflow",
                        actor_type="system",
                        runtime_source="workflow_runtime",
                        visibility_scope="team",
                        listed_surface="parent",
                        runtime_task_id=run_id,
                        transcript_metadata_json={
                            "session_state": "running",
                            "workflow_run_id": str(run_id),
                            "headless_workflow": True,
                        },
                    )
                )
                task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one_or_none()
                if task is not None:
                    task.parent_session_id = str(new_session_id)
                    task.child_session_id = str(new_session_id)
                    metadata = dict(task.metadata_json or {})
                    metadata["parent_session_id"] = str(new_session_id)
                    metadata["root_session_id"] = str(new_session_id)
                    metadata["session_bound"] = True
                    metadata["headless_session_created"] = True
                    metadata["user_id"] = resolved_user
                    task.metadata_json = metadata
        except Exception as exc:
            logger.warning("[Workflow] headless session bind for run %s failed (non-fatal): %s", run_id, exc)
            return (
                str(parent_session_id) if parent_session_id else None,
                str(root_session_id or parent_session_id) if (root_session_id or parent_session_id) else None,
                resolved_user,
            )
        return (str(new_session_id), str(new_session_id), resolved_user)

    async def _append_run_session_event(
        self,
        *,
        tenant_id: uuid.UUID | str,
        agent_id: uuid.UUID | None,
        user_id: uuid.UUID | str | None,
        run_id: uuid.UUID | str,
        parent_session_id: uuid.UUID | str | None,
        root_session_id: uuid.UUID | str | None,
        status: str,
        definition_source: str | None = None,
        definition_hash: str | None = None,
        reason: str | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> None:
        if not parent_session_id or agent_id is None:
            return
        session_id = str(parent_session_id)
        payload = {
            "type": "workflow_run",
            "status": status,
            "message": f"Workflow run {status}",
            "workflow_run_id": str(run_id),
            "runtime_task_id": str(run_id),
            "parent_session_id": session_id,
            "root_session_id": str(root_session_id or parent_session_id),
            "definition_source": definition_source,
            "definition_hash": definition_hash,
            "reason": reason,
        }
        if outputs is not None:
            # Make the session the run's truth surface (gap ledger: workflow
            # state is readable from session, not only the workflow journal):
            # project the per-step deliverable outputs plus a compact key list
            # so a session reader sees WHAT the run produced, not just a status.
            payload["outputs"] = outputs
            payload["deliverable_step_ids"] = sorted(str(key) for key in outputs)
        runtime_payload = _workflow_runtime_action_payload(
            run_id=str(run_id),
            status=status,
            parent_session_id=session_id,
            root_session_id=str(root_session_id or parent_session_id),
            definition_source=definition_source,
            definition_hash=definition_hash,
            reason=reason,
        )
        payloads = (payload, runtime_payload) if status == "running" else (runtime_payload, payload)
        try:
            async with self._session(tenant_id) as session:
                for event_payload in payloads:
                    event = build_session_native_event(event_payload)
                    await append_session_event(
                        db=session,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        actor_type="system",
                        event_type=str(event_payload["type"]),
                        role="system",
                        user_id=user_id,
                        run_id=run_id,
                        runtime_task_id=run_id,
                        root_session_id=root_session_id or parent_session_id,
                        parent_session_id=parent_session_id,
                        content=json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
                        source="workflow_runtime",
                        parts=[event["part"]] if isinstance(event.get("part"), dict) else None,
                        metadata={"source": "workflow_runtime", **event_payload},
                    )
                await session.commit()
        except Exception as exc:
            logger.warning("[Workflow] session event projection for run %s failed (non-fatal): %s", run_id, exc)

    async def resume_run(
        self,
        run_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        leaf_executor: LeafExecutor,
    ) -> WorkflowRunOutcome:
        from app.services.workflow_metrics import record_workflow_resume_attempt, record_workflow_resume_finished

        record_workflow_resume_attempt()
        lease = await self._lease_manager.try_acquire(run_id)
        if lease is None:
            outcome = WorkflowRunOutcome(
                status="suspended", reason="run lease held by another worker; not resuming here"
            )
            record_workflow_resume_finished(outcome.status)
            return outcome
        try:
            outcome = await self._resume_run_locked(run_id, tenant_id=tenant_id, leaf_executor=leaf_executor)
            record_workflow_resume_finished(outcome.status)
            return outcome
        finally:
            await lease.release()

    async def _resume_run_locked(
        self,
        run_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        leaf_executor: LeafExecutor,
    ) -> WorkflowRunOutcome:
        loaded = await self.load_run(run_id, tenant_id=tenant_id)
        if loaded is None:
            raise WorkflowRunNotFound(str(run_id))
        metadata = loaded.task.metadata_json or {}
        # NB: an EXPLICIT resume may revive a killed run (kill→resume is the
        # CC pattern); only the automatic startup scan excludes killed runs.

        definition_data = metadata.get("definition_json")
        args = metadata.get("args") or {}
        if not definition_data:
            raise WorkflowRunNotFound(f"run {run_id} has no archived definition")
        compiled = compile_workflow(definition_data)
        archived_hash = metadata.get("definition_hash")
        if archived_hash and compiled.definition_hash != archived_hash:
            # Archive integrity check — the archived definition must hash to
            # the recorded value, else the journal cannot be trusted.
            from app.services.workflow_metrics import record_workflow_hash_mismatch

            record_workflow_hash_mismatch()
            raise WorkflowRunNotFound(
                f"run {run_id}: archived definition hash mismatch ({compiled.definition_hash} != {archived_hash})"
            )

        # §9 P10 drain policy: an external/irreversible step left RUNNING by a
        # hard kill might or might not have produced its side effect — it must
        # NEVER be replayed automatically. Mark it for human reconciliation
        # and keep the run suspended.
        external_step_ids = {
            step.id for step in compiled.definition.steps if step.effects in ("external", "irreversible")
        }
        in_flight_external = [
            step for step in loaded.steps if step.step_id in external_step_ids and step.status == "running"
        ]
        if in_flight_external:
            async with self._session(tenant_id) as session:
                from app.models.workflow import WorkflowStep as _WS

                for row in in_flight_external:
                    db_row = (
                        await session.execute(select(_WS).where(_WS.run_id == run_id, _WS.step_id == row.step_id))
                    ).scalar_one()
                    db_row.status = "unknown_requires_reconciliation"
                task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
                task.status = "suspended"
                meta = dict(task.metadata_json or {})
                meta["needs_reconciliation"] = [row.step_id for row in in_flight_external]
                task.metadata_json = meta
            reason = (
                "external step(s) were in flight during a hard stop: "
                f"{[row.step_id for row in in_flight_external]} → unknown_requires_reconciliation; "
                "human reconciliation required before resume"
            )
            await self._audit(
                "workflow_run_needs_reconciliation",
                tenant_id=tenant_id,
                run_id=run_id,
                definition_hash=archived_hash,
                extra={"steps": [row.step_id for row in in_flight_external]},
            )
            return WorkflowRunOutcome(status="suspended", reason=reason)

        async with self._session(tenant_id) as session:
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
            task.status = "running"

        return await self._execute(compiled, run_id=run_id, tenant_id=tenant_id, args=args, leaf_executor=leaf_executor)

    async def kill_run(self, run_id: uuid.UUID | str, *, tenant_id: uuid.UUID | str | None = None) -> None:
        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(str(run_id))))
            ).scalar_one_or_none()
            if task is None:
                raise WorkflowRunNotFound(str(run_id))
            task.status = "killed"

    async def record_dynamic_repair_attempt(
        self, run_id: uuid.UUID | str, *, tenant_id: uuid.UUID | str | None = None
    ) -> None:
        """Persist a Dynamic Workflow repair attempt before resuming the run."""
        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(str(run_id))))
            ).scalar_one_or_none()
            if task is None or task.task_type != "workflow":
                raise WorkflowRunNotFound(str(run_id))
            metadata = dict(task.metadata_json or {})
            dynamic = dict(metadata.get("dynamic_workflow") or {})
            if not dynamic:
                return
            dynamic["repair_attempts"] = int(dynamic.get("repair_attempts") or 0) + 1
            metadata["dynamic_workflow"] = dynamic
            task.metadata_json = metadata

    async def load_run(
        self, run_id: uuid.UUID | str, *, tenant_id: uuid.UUID | str | None = None
    ) -> LoadedWorkflowRun | None:
        from app.models.workflow import WorkflowLeafCall

        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(str(run_id))))
            ).scalar_one_or_none()
            if task is None or task.task_type != "workflow":
                return None
            steps = (
                (await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == uuid.UUID(str(run_id)))))
                .scalars()
                .all()
            )
            leaf_calls = (
                (
                    await session.execute(
                        select(WorkflowLeafCall).where(WorkflowLeafCall.run_id == uuid.UUID(str(run_id)))
                    )
                )
                .scalars()
                .all()
            )
        return LoadedWorkflowRun(task=task, steps=list(steps), leaf_calls=list(leaf_calls))

    async def list_runs_for_agent(
        self, agent_id: uuid.UUID, *, tenant_id: uuid.UUID | str | None = None, limit: int = 50
    ) -> list[WorkflowRunSummary]:
        """The agent's run history (asset view §4): newest first, with step
        aggregates and promote provenance.

        runtime_tasks.tenant_id exists but is nullable/backfilled — the
        metadata mirror is the authoritative tenant boundary and is enforced
        here, same as ``resume_pending_runs``.
        """
        from sqlalchemy import func

        from app.models.workflow import WorkflowDefinitionRecord

        async with self._session(tenant_id) as session:
            tasks = (
                (
                    await session.execute(
                        select(RuntimeTask)
                        .where(
                            RuntimeTask.task_type == "workflow",
                            RuntimeTask.parent_agent_id == agent_id,
                        )
                        .order_by(RuntimeTask.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if tenant_id is not None:
                tasks = [t for t in tasks if (t.metadata_json or {}).get("tenant_id") == str(tenant_id)]
            run_ids = [t.id for t in tasks]
            counts: dict[uuid.UUID, dict[str, int]] = {rid: {} for rid in run_ids}
            promoted: dict[uuid.UUID, uuid.UUID] = {}
            if run_ids:
                count_rows = (
                    await session.execute(
                        select(WorkflowStep.run_id, WorkflowStep.status, func.count())
                        .where(WorkflowStep.run_id.in_(run_ids))
                        .group_by(WorkflowStep.run_id, WorkflowStep.status)
                    )
                ).all()
                for rid, step_status, n in count_rows:
                    counts[rid][step_status] = n
                promo_rows = (
                    await session.execute(
                        select(WorkflowDefinitionRecord.id, WorkflowDefinitionRecord.promoted_from_run_id).where(
                            WorkflowDefinitionRecord.promoted_from_run_id.in_(run_ids)
                        )
                    )
                ).all()
                for definition_id, rid in promo_rows:
                    promoted[rid] = definition_id
        return [
            WorkflowRunSummary(
                task=task,
                step_counts=counts.get(task.id, {}),
                promoted_definition_id=promoted.get(task.id),
            )
            for task in tasks
        ]

    # ── startup resume (precedent: resume_persisted_async_delegations) ──

    async def resume_pending_runs(self, *, leaf_executor: LeafExecutor) -> list[ResumedRun]:
        records = await list_active_runtime_task_records(
            statuses=_RESUMABLE_STATUSES,
            task_types=("workflow",),
            limit=None,
            session_factory=self._session_factory,
        )
        pending = [
            (
                uuid.UUID(str(record["task_id"])),
                (record.get("metadata") or {}).get("tenant_id") or record.get("tenant_id"),
                record.get("status"),
                record.get("metadata") or {},
            )
            for record in records
        ]

        from datetime import UTC, datetime

        resumed: list[ResumedRun] = []
        for run_id, tenant_value, run_status, metadata in pending:
            if not tenant_value:
                logger.warning("[Workflow] run %s has no tenant mirror; skipping auto-resume", run_id)
                continue
            if run_status == "suspended":
                # Only TIME suspensions auto-resume (and only once due);
                # gate/budget suspensions wait for their own recovery path
                # (approval → explicit resume; quota refill → admission).
                resume_at_raw = metadata.get("resume_at")
                if not resume_at_raw:
                    continue
                try:
                    resume_at = datetime.fromisoformat(resume_at_raw)
                except ValueError:
                    logger.warning("[Workflow] run %s has malformed resume_at %r", run_id, resume_at_raw)
                    continue
                if resume_at.tzinfo is None:
                    resume_at = resume_at.replace(tzinfo=UTC)
                if resume_at > datetime.now(UTC):
                    continue
            try:
                outcome = await self.resume_run(run_id, tenant_id=uuid.UUID(tenant_value), leaf_executor=leaf_executor)
                resumed.append(ResumedRun(run_id=run_id, outcome=outcome))
            except Exception as exc:
                logger.error("[Workflow] auto-resume of run %s failed: %s", run_id, exc, exc_info=True)
        return resumed

    # ── observation surfaces (audit / completion signal) ─────────

    async def _audit(
        self,
        action: str,
        *,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        agent_id: uuid.UUID | None = None,
        definition_hash: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """Audit trail for run lifecycle (§9 P9) — fail-soft, never blocks."""
        try:
            from app.services.audit_logger import write_audit_log

            details = {
                "tenant_id": str(tenant_id),
                "run_id": str(run_id),
                "definition_hash": definition_hash,
            }
            if extra:
                details.update(extra)
            await write_audit_log(action, details=details, agent_id=agent_id)
        except Exception as exc:
            logger.warning("[Workflow] audit write %s failed (non-fatal): %s", action, exc)

    @staticmethod
    async def _emit_completion_signal(
        run_id: uuid.UUID,
        agent_id: uuid.UUID | None,
        status: str,
        *,
        tenant_id: uuid.UUID,
    ) -> None:
        """``workflow_completed`` Signal — NOTIFICATION ONLY (§3.3): read-once
        consumption, never a wait_signal resume promise (that is P11)."""
        if agent_id is None:
            return
        try:
            async with gateway_scope(tenant_id=tenant_id) as gateway:
                await gateway.send_signal(
                    from_agent_id=f"workflow:{run_id}",
                    to_agent_id=str(agent_id),
                    content=f"workflow run {run_id} finished: {status}",
                    signal_type="workflow_completed",
                    thread_id=str(run_id),
                )
        except Exception as exc:
            logger.warning("[Workflow] completion signal failed (non-fatal): %s", exc)

    async def _deliver_completion_notification(
        self,
        *,
        run_id: uuid.UUID,
        agent_id: uuid.UUID | None,
        status: str,
        tenant_id: uuid.UUID,
        metadata: dict[str, Any],
    ) -> None:
        if agent_id is None:
            return
        reply_target = (
            metadata.get("delivery_target_json") or metadata.get("reply_target") or metadata.get("delivery_target")
        )
        if not isinstance(reply_target, dict) or not reply_target.get("channel"):
            return
        text = f"Workflow run {run_id} finished: {status}."
        try:
            async with self._session(tenant_id) as session:
                await ChannelDeliveryService.send_text(
                    db=session,
                    agent_id=agent_id,
                    reply_target=reply_target,
                    text=text,
                    delivery_mode="async_completion",
                    extra_detail={"runtime_task_id": str(run_id), "status": status},
                )
        except Exception as exc:
            logger.warning("[Workflow] completion delivery failed (non-fatal): %s", exc)

    @staticmethod
    def _completion_task_summary(
        *, run_id: uuid.UUID, status: str, reason: str | None, outputs: dict[str, Any] | None
    ) -> str:
        parts = [f"Workflow run {run_id} finished: {status}."]
        if reason:
            parts.append(f"Reason: {reason}")
        if outputs:
            try:
                output_text = json.dumps(outputs, ensure_ascii=False, sort_keys=True, default=str)
            except TypeError:
                output_text = str(outputs)
            if len(output_text) > 4000:
                output_text = output_text[:3997].rstrip() + "..."
            parts.append(f"Outputs: {output_text}")
        return "\n".join(parts)

    async def _claim_parent_task_notification_side_effect(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        status: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Claim the parent-loop wake side effect for terminal workflow runs."""

        from datetime import UTC, datetime

        idempotency_key = f"workflow_parent_task_notification:{run_id}:{status}"
        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id).with_for_update())
            ).scalar_one()
            metadata = dict(task.metadata_json or {})
            existing = metadata.get("parent_task_notification_side_effect")
            if isinstance(existing, dict) and existing.get("idempotency_key") == idempotency_key:
                return False, metadata
            metadata["parent_task_notification_side_effect"] = {
                "idempotency_key": idempotency_key,
                "status": status,
                "claimed_at": datetime.now(UTC).isoformat(),
                "source": "workflow",
            }
            task.metadata_json = metadata
            await session.flush()
            return True, metadata

    async def _wake_parent_session_from_workflow_completion(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID | None,
        parent_session_id: str | uuid.UUID | None,
        status: str,
        summary: str,
        metadata: dict[str, Any],
    ) -> None:
        if agent_id is None or not parent_session_id:
            return
        try:
            from app.models.agent import Agent
            from app.models.chat_session import ChatSession
            from app.models.user import User
            from app.services.agent_session_continuation import continue_parent_session_with_task_notification

            parent_session_uuid = uuid.UUID(str(parent_session_id))
            async with self._session(tenant_id) as session:
                parent_session = (
                    await session.execute(
                        select(ChatSession).where(
                            ChatSession.id == parent_session_uuid, ChatSession.agent_id == agent_id
                        )
                    )
                ).scalar_one_or_none()
                parent_agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
                owner_id = (
                    metadata.get("user_id")
                    or getattr(parent_session, "user_id", None)
                    or getattr(parent_agent, "creator_id", None)
                )
                owner = (
                    (
                        await session.execute(select(User).where(User.id == uuid.UUID(str(owner_id))))
                    ).scalar_one_or_none()
                    if owner_id
                    else None
                )
                if parent_session is None or parent_agent is None or owner is None:
                    return
                await continue_parent_session_with_task_notification(
                    db=session,
                    agent=parent_agent,
                    user=owner,
                    session=parent_session,
                    task_id=str(run_id),
                    task_type="workflow",
                    status=status,
                    summary=summary,
                    source="workflow",
                    metadata={
                        "workflow_run_id": str(run_id),
                        "parent_agent_id": str(agent_id),
                        "parent_session_id": str(parent_session_uuid),
                        "workflow_session_state": status,
                    },
                )
        except Exception as exc:
            logger.warning(
                "[Workflow] parent task-notification wake failed for run %s session %s: %s",
                run_id,
                parent_session_id,
                exc,
            )

    async def _claim_completion_side_effects(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        status: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Atomically claim run-level completion side effects.

        Step replay is deterministic and may return ``completed`` more than once
        for the same run. Completion notification is an external side effect, so
        the durable run row is the idempotency boundary.
        """
        from datetime import UTC, datetime

        idempotency_key = f"workflow_completed:{run_id}:{status}"
        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id).with_for_update())
            ).scalar_one()
            metadata = dict(task.metadata_json or {})
            existing = metadata.get("completion_side_effects")
            if isinstance(existing, dict) and existing.get("idempotency_key") == idempotency_key:
                return False, metadata
            metadata["completion_side_effects"] = {
                "idempotency_key": idempotency_key,
                "status": status,
                "claimed_at": datetime.now(UTC).isoformat(),
                "signal_type": "workflow_completed",
            }
            task.metadata_json = metadata
            await session.flush()
            return True, metadata

    # ── internals ────────────────────────────────────────────────

    async def _execute(
        self,
        compiled: CompiledWorkflow,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        args: dict,
        leaf_executor: LeafExecutor,
    ) -> WorkflowRunOutcome:
        agent_for_mirror: uuid.UUID | None = None
        parent_session_id: str | None = None
        root_session_id: str | None = None
        user_id: str | None = None
        async with self._session(tenant_id) as session:
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
            agent_for_mirror = task.parent_agent_id
            task_metadata = dict(task.metadata_json or {})
            parent_session_id = task.parent_session_id or task_metadata.get("parent_session_id")
            root_session_id = task_metadata.get("root_session_id") or parent_session_id
            user_id = task_metadata.get("user_id")
        # §A-6: cover the resume / startup-scan paths too — bind a session for a
        # still-headless run. Idempotent: already-bound runs return unchanged.
        parent_session_id, root_session_id, user_id = await self._ensure_run_session(
            tenant_id=tenant_id,
            agent_id=agent_for_mirror,
            user_id=user_id,
            run_id=run_id,
            parent_session_id=parent_session_id,
            root_session_id=root_session_id,
        )
        journal = _PGWorkflowJournal(
            self._session_factory,
            tenant_id,
            agent_id=agent_for_mirror,
            run_id=run_id,
            parent_session_id=parent_session_id,
            root_session_id=root_session_id,
            user_id=user_id,
        )
        quota = PGQuotaReserver(self._session_factory, tenant_id, estimate=get_settings().WORKFLOW_LEAF_TOKEN_ESTIMATE)

        service = self

        class _MetadataWaitScheduler:
            async def schedule_resume(self, rid: str, *, resume_at) -> None:
                await service._record_resume_at(uuid.UUID(rid), tenant_id, resume_at)

        wait_scheduler = _MetadataWaitScheduler()

        class _MetadataSignalWaitRegistrar:
            async def register_wait(self, rid: str, *, step_id: str, signal_type: str) -> None:
                async with service._session(tenant_id) as session:
                    task = (
                        await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(rid)))
                    ).scalar_one()
                    metadata = dict(task.metadata_json or {})
                    metadata["waiting_for_signal"] = {"step_id": step_id, "signal_type": signal_type}
                    task.metadata_json = metadata

        signal_wait_registrar = _MetadataSignalWaitRegistrar()

        async def metered_leaf_executor(request: LeafRequest) -> LeafOutcome:
            if request.leaf_id is None:
                from app.services.workflow_metrics import record_workflow_leaf_call

                record_workflow_leaf_call("running")
            try:
                outcome = await leaf_executor(request)
            except Exception:
                if request.leaf_id is None:
                    from app.services.workflow_metrics import record_workflow_leaf_call

                    record_workflow_leaf_call("failed")
                raise
            if request.leaf_id is None:
                from app.services.workflow_metrics import record_workflow_leaf_call

                record_workflow_leaf_call("done" if outcome.ok else "failed")
            return outcome

        async def should_continue() -> bool:
            if self._draining:
                return False  # graceful drain: stop at the next step boundary
            async with self._session(tenant_id) as session:
                status = (
                    await session.execute(select(RuntimeTask.status).where(RuntimeTask.id == run_id))
                ).scalar_one_or_none()
            # During execution the row is "running"; mid-run it can only become
            # "killed" (user/admin cancel) or "suspended" (admin force-suspend).
            # Both are stop signals at the next step boundary — a force-suspend
            # that kept executing would let the final status overwrite the
            # operator's persisted state.
            return status not in ("killed", "suspended")

        try:
            outcome = await execute_workflow(
                compiled,
                run_id=str(run_id),
                args=args,
                journal=journal,
                leaf_executor=metered_leaf_executor,
                should_continue=should_continue,
                tenant_id=str(tenant_id),
                quota=quota,
                gate_decider=self._gate_decider,
                wait_scheduler=wait_scheduler,
                signal_wait_registrar=signal_wait_registrar,
            )
        except Exception:
            # Engine/leaf raised out of contract (e.g. process-crash
            # simulation in tests): leave the run 'running' so the startup
            # scan can pick it up, then surface the error.
            raise

        if self._draining and outcome.status == "killed":
            # Drain stop, not a user kill: the run stays RESUMABLE — a fresh
            # worker's startup scan picks it up after the restart.
            outcome = WorkflowRunOutcome(
                status="suspended", reason="worker draining; run left resumable", outputs=outcome.outputs
            )
            async with self._session(tenant_id) as session:
                task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
                task.status = "running"  # crash-equivalent: startup scan reclaims it
            from app.services.workflow_metrics import record_workflow_run_finished

            record_workflow_run_finished(outcome.status)
            return outcome

        agent_for_signal: uuid.UUID | None = None
        definition_hash: str | None = None
        task_metadata: dict[str, Any] = {}
        async with self._session(tenant_id) as session:
            from app.models.workflow import WorkflowLeafCall

            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
            if outcome.status == "killed" and task.status == "suspended":
                # The stop flag the engine saw was an admin force-suspend, not a
                # kill: report it truthfully and keep the operator's state.
                outcome = WorkflowRunOutcome(
                    status="suspended",
                    reason="force-suspended by operator; stopped at step boundary",
                    outputs=outcome.outputs,
                )
            if task.status != "killed":
                task.status = {
                    "completed": "completed",
                    "failed": "failed",
                    "suspended": "suspended",
                    "killed": "killed",
                }[outcome.status]
            if outcome.reason:
                metadata = dict(task.metadata_json or {})
                metadata["last_outcome_reason"] = outcome.reason
                task.metadata_json = metadata
            agent_for_signal = task.parent_agent_id
            task_metadata = dict(task.metadata_json or {})
            if task_metadata.get("dynamic_workflow"):
                steps = (
                    (await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == run_id))).scalars().all()
                )
                leaf_calls = (
                    (await session.execute(select(WorkflowLeafCall).where(WorkflowLeafCall.run_id == run_id)))
                    .scalars()
                    .all()
                )
                dynamic = dict(task_metadata.get("dynamic_workflow") or {})
                task_for_summary = task
                task_for_summary.metadata_json = task_metadata
                outcome_evidence = summarize_dynamic_workflow_outcome(
                    task=task_for_summary,
                    steps=list(steps),
                    leaf_calls=list(leaf_calls),
                )
                repair_plan = build_dynamic_workflow_repair_plan(
                    task=task_for_summary,
                    steps=list(steps),
                    leaf_calls=list(leaf_calls),
                )
                dynamic = attach_workflow_decision_outcome(
                    dynamic_workflow=dynamic,
                    run_id=str(run_id),
                    outcome_evidence=outcome_evidence,
                    repair_plan=repair_plan,
                )
                task_metadata["dynamic_workflow"] = dynamic
                task.metadata_json = task_metadata
            definition_hash = task_metadata.get("definition_hash")

        await self._audit(
            f"workflow_run_{outcome.status}",
            tenant_id=tenant_id,
            run_id=run_id,
            agent_id=agent_for_signal,
            definition_hash=definition_hash,
            extra={"reason": outcome.reason} if outcome.reason else None,
        )
        from app.services.workflow_metrics import record_workflow_run_finished

        record_workflow_run_finished(outcome.status)
        await self._append_run_session_event(
            tenant_id=tenant_id,
            agent_id=agent_for_signal,
            user_id=task_metadata.get("user_id"),
            run_id=run_id,
            parent_session_id=task_metadata.get("parent_session_id"),
            root_session_id=task_metadata.get("root_session_id") or task_metadata.get("parent_session_id"),
            status=outcome.status,
            definition_source=task_metadata.get("definition_source"),
            definition_hash=definition_hash,
            reason=outcome.reason,
            outputs=outcome.outputs,
        )
        if outcome.status in {"completed", "failed", "killed"}:
            notification_claimed, notification_metadata = await self._claim_parent_task_notification_side_effect(
                run_id=run_id,
                tenant_id=tenant_id,
                status=outcome.status,
            )
            if notification_claimed:
                await self._wake_parent_session_from_workflow_completion(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    agent_id=agent_for_signal,
                    parent_session_id=task_metadata.get("parent_session_id"),
                    status=outcome.status,
                    summary=self._completion_task_summary(
                        run_id=run_id,
                        status=outcome.status,
                        reason=outcome.reason,
                        outputs=outcome.outputs,
                    ),
                    metadata=notification_metadata,
                )
                task_metadata = notification_metadata
        if outcome.status == "completed":
            claimed, task_metadata = await self._claim_completion_side_effects(
                run_id=run_id,
                tenant_id=tenant_id,
                status=outcome.status,
            )
            if claimed:
                await self._emit_completion_signal(run_id, agent_for_signal, outcome.status, tenant_id=tenant_id)
                await self._deliver_completion_notification(
                    run_id=run_id,
                    agent_id=agent_for_signal,
                    status=outcome.status,
                    tenant_id=tenant_id,
                    metadata=task_metadata,
                )
        return outcome
