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
from app.services.channel_delivery_service import ChannelDeliveryService
from app.runtime.workflow_admission import AdmissionLimits, WorkflowAdmissionError, admit_workflow
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
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._agent_id = agent_id
        self._run_id = run_id
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

    async def record_step_failed(self, run_id: str, step_id: str, *, error: str) -> None:
        from sqlalchemy import func

        await self._upsert(run_id, step_id, status="failed", error=error[:4000], finished_at=func.now())
        self._observe_step_finished(run_id, step_id, "failed")
        self._mirror_step(step_id, "failed")

    async def record_step_skipped(self, run_id: str, step_id: str, *, definition_hash: str) -> None:
        from app.services.workflow_metrics import record_workflow_step

        await self._upsert(run_id, step_id, status="skipped", definition_hash=definition_hash)
        record_workflow_step("skipped")

    async def record_step_suspended(self, run_id: str, step_id: str, *, reason: str) -> None:
        await self._upsert(run_id, step_id, status="suspended", error=reason[:4000])
        self._observe_step_finished(run_id, step_id, "suspended")
        self._mirror_step(step_id, "suspended")

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
        async with tenant_scoped_session(session_factory=self._session_factory) as session:
            row = (
                await session.execute(select(RuntimeTask.metadata_json).where(RuntimeTask.id == uuid.UUID(str(run_id))))
            ).scalar_one_or_none()
        if not isinstance(row, dict):
            return None
        tenant_value = row.get("tenant_id")
        return str(tenant_value) if tenant_value else None

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
        confirmed_plan_id: uuid.UUID | str | None = None,
        allowed_leaves: set[str] | None = None,
        run_id: uuid.UUID | None = None,
        delivery_target: dict[str, Any] | None = None,
    ) -> WorkflowRunHandle:
        if not get_settings().WORKFLOW_RUNTIME_ENABLED:
            raise WorkflowAdmissionError("workflow runtime disabled by feature flag WORKFLOW_RUNTIME_ENABLED")
        compiled = compile_workflow(definition_data, known_leaves=allowed_leaves)
        limits = AdmissionLimits.from_settings(get_settings())
        admission = admit_workflow(compiled, args=args, limits=limits, allowed_leaves=allowed_leaves)

        args_hash = compute_definition_hash(args)
        # A caller may pre-generate the id so run-scoped artifacts (e.g. the
        # Deep Research request.json) can land BEFORE execution starts.
        run_id = run_id or uuid.uuid4()
        async with self._session(tenant_id) as session:
            task = RuntimeTask(
                id=run_id,
                task_type="workflow",
                status="running",
                parent_agent_id=agent_id,
                metadata_json={
                    "definition_source": definition_source,
                    "definition_hash": compiled.definition_hash,
                    "args_hash": args_hash,
                    "confirmed_plan_id": str(confirmed_plan_id) if confirmed_plan_id else None,
                    "tenant_id": str(tenant_id),
                    "delivery_target_json": delivery_target,
                    # Ephemeral archive (§3.1): the run must be replayable
                    # without the original conversation.
                    "definition_json": compiled.definition.canonical_dict()
                    if not isinstance(definition_data, dict)
                    else definition_data,
                    "args": args,
                },
            )
            session.add(task)
            session.add(
                WorkflowQuota(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    allocated_tokens=admission.budget_tokens,
                )
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

        outcome = await self._execute(
            compiled, run_id=run_id, tenant_id=tenant_id, args=args, leaf_executor=leaf_executor
        )
        return WorkflowRunHandle(run_id=run_id, outcome=outcome)

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

    async def load_run(
        self, run_id: uuid.UUID | str, *, tenant_id: uuid.UUID | str | None = None
    ) -> LoadedWorkflowRun | None:
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
        return LoadedWorkflowRun(task=task, steps=list(steps))

    async def list_runs_for_agent(
        self, agent_id: uuid.UUID, *, tenant_id: uuid.UUID | str | None = None, limit: int = 50
    ) -> list[WorkflowRunSummary]:
        """The agent's run history (asset view §4): newest first, with step
        aggregates and promote provenance.

        runtime_tasks has no tenant column — the metadata mirror is the
        tenant boundary and is enforced here, same as ``resume_pending_runs``.
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
        async with self._session(None) as session:
            # runtime_tasks has no tenant column; tenant comes from each
            # run's metadata mirror and scopes the per-run journal sessions.
            rows = (
                (
                    await session.execute(
                        select(RuntimeTask).where(
                            RuntimeTask.task_type == "workflow",
                            RuntimeTask.status.in_(_RESUMABLE_STATUSES),
                        )
                    )
                )
                .scalars()
                .all()
            )
            pending = [
                (row.id, (row.metadata_json or {}).get("tenant_id"), row.status, row.metadata_json or {})
                for row in rows
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
            metadata.get("delivery_target_json")
            or metadata.get("reply_target")
            or metadata.get("delivery_target")
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
        async with self._session(tenant_id) as session:
            agent_for_mirror = (
                await session.execute(select(RuntimeTask.parent_agent_id).where(RuntimeTask.id == run_id))
            ).scalar_one_or_none()
        journal = _PGWorkflowJournal(self._session_factory, tenant_id, agent_id=agent_for_mirror, run_id=run_id)
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
