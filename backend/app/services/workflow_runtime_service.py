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
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowQuota, WorkflowStep
from app.runtime.workflow_admission import AdmissionLimits, admit_workflow
from app.runtime.workflow_compiler import CompiledWorkflow, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash
from app.runtime.workflow_engine import (
    LeafExecutor,
    LeafRecord,
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
class ResumedRun:
    run_id: uuid.UUID
    outcome: WorkflowRunOutcome


class _PGWorkflowJournal:
    """Real-PG step journal bound to one (tenant, run)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None, tenant_id: uuid.UUID | str) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id

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

    async def record_step_done(self, run_id: str, step_id: str, *, output: Any, result_ref: str | None) -> None:
        from sqlalchemy import func

        encoded = result_ref if result_ref is not None else json.dumps(output, ensure_ascii=False, sort_keys=True)
        await self._upsert(run_id, step_id, status="done", result_ref=encoded, finished_at=func.now())

    async def record_step_failed(self, run_id: str, step_id: str, *, error: str) -> None:
        from sqlalchemy import func

        await self._upsert(run_id, step_id, status="failed", error=error[:4000], finished_at=func.now())

    async def record_step_skipped(self, run_id: str, step_id: str, *, definition_hash: str) -> None:
        await self._upsert(run_id, step_id, status="skipped", definition_hash=definition_hash)

    async def record_step_suspended(self, run_id: str, step_id: str, *, reason: str) -> None:
        await self._upsert(run_id, step_id, status="suspended", error=reason[:4000])

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

    async def record_leaf_done(self, run_id: str, step_id: str, leaf_id: str, *, output: Any, tokens_used: int) -> None:
        from sqlalchemy import func

        await self._upsert_leaf(
            run_id,
            step_id,
            leaf_id,
            status="done",
            result_ref=json.dumps(output, ensure_ascii=False, sort_keys=True),
            token_usage={"total": tokens_used},
            finished_at=func.now(),
        )

    async def record_leaf_failed(self, run_id: str, step_id: str, leaf_id: str, *, error: str) -> None:
        from sqlalchemy import func

        await self._upsert_leaf(run_id, step_id, leaf_id, status="failed", error=error[:4000], finished_at=func.now())


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


class WorkflowRuntimeService:
    """start / resume / kill / load — every DB touch tenant-scoped."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

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
    ) -> WorkflowRunHandle:
        compiled = compile_workflow(definition_data, known_leaves=allowed_leaves)
        limits = AdmissionLimits.from_settings(get_settings())
        admission = admit_workflow(compiled, args=args, limits=limits, allowed_leaves=allowed_leaves)

        args_hash = compute_definition_hash(args)
        run_id = uuid.uuid4()
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
            raise WorkflowRunNotFound(
                f"run {run_id}: archived definition hash mismatch ({compiled.definition_hash} != {archived_hash})"
            )

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
            pending = [(row.id, (row.metadata_json or {}).get("tenant_id")) for row in rows]

        resumed: list[ResumedRun] = []
        for run_id, tenant_value in pending:
            if not tenant_value:
                logger.warning("[Workflow] run %s has no tenant mirror; skipping auto-resume", run_id)
                continue
            try:
                outcome = await self.resume_run(run_id, tenant_id=uuid.UUID(tenant_value), leaf_executor=leaf_executor)
                resumed.append(ResumedRun(run_id=run_id, outcome=outcome))
            except Exception as exc:
                logger.error("[Workflow] auto-resume of run %s failed: %s", run_id, exc, exc_info=True)
        return resumed

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
        journal = _PGWorkflowJournal(self._session_factory, tenant_id)
        quota = PGQuotaReserver(self._session_factory, tenant_id, estimate=get_settings().WORKFLOW_LEAF_TOKEN_ESTIMATE)

        async def should_continue() -> bool:
            async with self._session(tenant_id) as session:
                status = (
                    await session.execute(select(RuntimeTask.status).where(RuntimeTask.id == run_id))
                ).scalar_one_or_none()
            return status not in ("killed",)

        try:
            outcome = await execute_workflow(
                compiled,
                run_id=str(run_id),
                args=args,
                journal=journal,
                leaf_executor=leaf_executor,
                should_continue=should_continue,
                tenant_id=str(tenant_id),
                quota=quota,
            )
        except Exception:
            # Engine/leaf raised out of contract (e.g. process-crash
            # simulation in tests): leave the run 'running' so the startup
            # scan can pick it up, then surface the error.
            raise

        async with self._session(tenant_id) as session:
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
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
        return outcome
