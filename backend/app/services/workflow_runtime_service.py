"""WorkflowRuntimeService (§9 P3): start / resume / kill / load + PG journal.

The Imperative Shell around the pure engine (§3.3):

* a workflow run IS a ``RuntimeTask(task_type="workflow")`` — run metadata
  (definition archive, args, hashes, tenant mirror) lives in
  ``metadata_json``; no fifth background ledger;
* the step journal is the ``workflow_steps`` table (FORCEd RLS — every
  session here goes through ``tenant_scoped_session``);
* kill is persisted state: the engine polls ``should_continue`` before every
  step, which reads the run row, so a kill lands mid-run;
* startup recovery requeues stale/due runs behind the shared RuntimeTask claim
  fence; killed and reconciliation-blocked runs are never auto-resumed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.coordination_wiring import gateway_scope
from app.config import get_settings
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowLeafCall, WorkflowQuota, WorkflowQuotaReservation, WorkflowStep
from app.runtime.recovery_manifest import (
    RecoveryManifest,
    inspect_recovery_manifest_checkpoint,
    reviewed_recovery_manifest_evidence,
)
from app.runtime.dynamic_workflow import (
    attach_workflow_decision_outcome,
    build_dynamic_workflow_repair_plan,
    summarize_dynamic_workflow_outcome,
)
from app.services.channel_delivery_service import ChannelDeliveryService
from app.services.chat_message_parts import build_session_native_event
from app.services.chat_transcript import append_session_event
from app.services.execution_admission import ExecutionAdmission, ExecutionAdmissionDecision
from app.services.runtime_budget_service import (
    RuntimeBudgetPolicyLookup,
    RuntimeBudgetReservation,
    RuntimeBudgetRunCreate,
    RuntimeBudgetService,
)
from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification
from app.services.runtime_task_fence import assert_runtime_task_fence
from app.services.runtime_task_service import list_active_runtime_task_records
from app.runtime.workflow_admission import (
    AdmissionLimits,
    WorkflowAdmissionError,
    admit_workflow,
    normalize_workflow_args,
)
from app.runtime.workflow_compiler import CompiledWorkflow, compile_workflow
from app.runtime.workflow_definition import AgentStep, FanoutStep, compute_definition_hash
from app.runtime.workflow_engine import (
    GateDecision,
    LeafExecutor,
    LeafOutcome,
    LeafRecord,
    LeafRequest,
    StepRecord,
    WorkflowRunOutcome,
    execute_workflow,
    workflow_leaf_recovery_identity,
)
from app.tools.registry import (
    is_destructive_tool,
    is_parallel_safe_tool,
    is_read_only_tool,
    is_workspace_mutating_tool,
)

logger = logging.getLogger(__name__)

_RESUMABLE_STATUSES = ("running", "suspended")
_ACTIVATION_EVIDENCE_GRACE_SECONDS = 300
_RECOVERABLE_TOOL_FRAME_STATUSES = frozenset(
    {"", "pending", "running", "started", "in_progress", "needs_reconciliation"}
)
_WORKFLOW_RUNTIME_ACTION_BY_RUN_STATUS = {
    "running": "runtime_action_started",
    "completed": "runtime_action_completed",
    "failed": "runtime_action_failed",
    "killed": "runtime_action_failed",
    "suspended": "runtime_action_blocked",
}


@dataclass(frozen=True, slots=True)
class WorkflowRecoveryManifestAssessment:
    requires_reconciliation: bool
    reason: str | None = None
    unsafe_frames: tuple[dict[str, Any], ...] = ()
    operator_retry_authorized: bool = False


def assess_workflow_recovery_manifest(
    manifest: RecoveryManifest | None,
    *,
    canonical_path_present: bool,
) -> WorkflowRecoveryManifestAssessment:
    """Classify durable leaf evidence without trusting declared workflow effects."""

    if manifest is None:
        if canonical_path_present:
            return WorkflowRecoveryManifestAssessment(
                requires_reconciliation=True,
                reason="workflow_recovery_manifest_invalid",
            )
        return WorkflowRecoveryManifestAssessment(requires_reconciliation=False)
    if manifest.recovery_reconciliation_blocked:
        return WorkflowRecoveryManifestAssessment(
            requires_reconciliation=True,
            reason="workflow_recovery_manifest_already_blocked",
            unsafe_frames=tuple(dict(frame) for frame in manifest.pending_tool_frames if isinstance(frame, dict)),
        )
    if (
        str((manifest.reconciliation_resolution or {}).get("action") or "") == "retry"
        and not manifest.pending_tool_frames
    ):
        return WorkflowRecoveryManifestAssessment(
            requires_reconciliation=False,
            operator_retry_authorized=True,
        )

    unsafe_frames: list[dict[str, Any]] = []
    for raw_frame in manifest.pending_tool_frames:
        if not isinstance(raw_frame, dict):
            continue
        frame = dict(raw_frame)
        status = str(frame.get("status") or "").strip().lower()
        if status not in _RECOVERABLE_TOOL_FRAME_STATUSES:
            continue
        tool_name = str(frame.get("tool_name") or "").strip()
        replay_safe = bool(
            tool_name
            and is_read_only_tool(tool_name)
            and is_parallel_safe_tool(tool_name)
            and not is_workspace_mutating_tool(tool_name)
            and not is_destructive_tool(tool_name)
        )
        if not replay_safe:
            unsafe_frames.append(frame)
    completed_frames: list[dict[str, Any]] = []
    for index, raw_outcome in enumerate(manifest.recent_tool_outcomes):
        if not isinstance(raw_outcome, dict):
            continue
        tool_name = str(raw_outcome.get("tool") or "").strip()
        if not tool_name or tool_name.startswith("session_memory"):
            continue
        replay_safe = bool(
            is_read_only_tool(tool_name)
            and is_parallel_safe_tool(tool_name)
            and not is_workspace_mutating_tool(tool_name)
            and not is_destructive_tool(tool_name)
        )
        if replay_safe:
            continue
        completed_frames.append(
            {
                "tool_call_id": f"completed-mutation-{index}",
                "tool_name": tool_name,
                "status": "completed_before_workflow_journal",
            }
        )
    if (manifest.current_turn_writes or manifest.recent_writes) and not completed_frames:
        completed_frames.append(
            {
                "tool_call_id": "completed-workspace-mutation",
                "tool_name": "workspace_mutation",
                "status": "completed_before_workflow_journal",
            }
        )
    if completed_frames:
        return WorkflowRecoveryManifestAssessment(
            requires_reconciliation=True,
            reason="workflow_completed_mutation_not_journaled",
            unsafe_frames=tuple(completed_frames),
        )
    if unsafe_frames:
        return WorkflowRecoveryManifestAssessment(
            requires_reconciliation=True,
            reason="workflow_pending_tool_not_replay_safe",
            unsafe_frames=tuple(unsafe_frames),
        )
    return WorkflowRecoveryManifestAssessment(requires_reconciliation=False)


def _merge_workflow_recovery_targets(
    existing_targets: list[dict[str, Any]],
    new_targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace stale CAS data for the same deterministic recovery lane."""

    merged: list[dict[str, Any]] = []
    index_by_key: dict[tuple[str, str, str], int] = {}
    for raw_target in [*existing_targets, *new_targets]:
        target = dict(raw_target)
        key = (
            str(target.get("agent_id") or ""),
            str(target.get("session_id") or ""),
            str(target.get("runtime_task_id") or ""),
        )
        if not all(key):
            continue
        existing_index = index_by_key.get(key)
        if existing_index is None:
            index_by_key[key] = len(merged)
            merged.append(target)
        else:
            merged[existing_index] = target
    return merged


def _merge_workflow_recovery_frames(
    existing_frames: list[dict[str, Any]],
    new_frames: list[dict[str, Any]],
    *,
    replaced_leaf_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Drop frames resolved for a retried leaf before recording its new incident."""

    retained = [
        dict(frame)
        for frame in existing_frames
        if (
            str(frame.get("workflow_step_id") or ""),
            str(frame.get("workflow_leaf_id") or ""),
        )
        not in replaced_leaf_keys
    ]
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_frame in [*retained, *new_frames]:
        frame = dict(raw_frame)
        key = (
            str(frame.get("runtime_task_id") or ""),
            str(frame.get("tool_call_id") or ""),
            str(frame.get("tool_name") or ""),
        )
        if not all(key) or key in seen:
            continue
        seen.add(key)
        merged.append(frame)
    return merged


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

    async def _upsert(
        self,
        run_id: str,
        step_id: str,
        *,
        allowed_task_statuses: frozenset[str] = frozenset({"running"}),
        **values: Any,
    ) -> bool:
        run_uuid = uuid.UUID(run_id)
        async with self._session() as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_uuid).with_for_update())
            ).scalar_one()
            assert_runtime_task_fence(task)
            if task.status not in allowed_task_statuses:
                return False
            row = (
                await session.execute(
                    select(WorkflowStep)
                    .where(
                        WorkflowStep.run_id == run_uuid,
                        WorkflowStep.step_id == step_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                row = WorkflowStep(
                    tenant_id=uuid.UUID(str(self._tenant_id)),
                    run_id=run_uuid,
                    step_id=step_id,
                )
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            return True

    async def _append_step_event(
        self,
        run_id: str,
        step_id: str,
        *,
        status: str,
        reason: str | None = None,
        result_ref: str | None = None,
    ) -> bool:
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
                run_uuid = uuid.UUID(run_id)
                task = (
                    await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_uuid).with_for_update())
                ).scalar_one()
                assert_runtime_task_fence(task)
                step = (
                    await session.execute(
                        select(WorkflowStep)
                        .where(WorkflowStep.run_id == run_uuid, WorkflowStep.step_id == step_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                allowed_task_statuses = (
                    {"running", "killed", "suspended"} if status in {"done", "failed"} else {"running"}
                )
                if step is None or step.status != status or task.status not in allowed_task_statuses:
                    return False
                self._mirror_step(step_id, status)
                if not self._parent_session_id or self._agent_id is None:
                    return True
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
                return True
        except Exception as exc:
            logger.warning("[Workflow] session event projection for step %s failed (non-fatal): %s", step_id, exc)
            return False

    async def record_step_start(
        self, run_id: str, step_id: str, *, step_type: str, input_hash: str | None, definition_hash: str
    ) -> None:
        from sqlalchemy import func
        from app.services.workflow_metrics import record_workflow_step

        self._step_started_at[(run_id, step_id)] = time.monotonic()
        if not await self._upsert(
            run_id,
            step_id,
            step_type=step_type,
            status="running",
            input_hash=input_hash,
            definition_hash=definition_hash,
            started_at=func.now(),
            error=None,
        ):
            return
        record_workflow_step("running")
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
        if not await self._upsert(
            run_id,
            step_id,
            allowed_task_statuses=frozenset({"running", "killed", "suspended"}),
            status="done",
            result_ref=encoded,
            finished_at=func.now(),
        ):
            return
        self._observe_step_finished(run_id, step_id, "done")
        await self._append_step_event(run_id, step_id, status="done", result_ref=encoded)

    async def record_step_failed(self, run_id: str, step_id: str, *, error: str) -> None:
        from sqlalchemy import func

        if not await self._upsert(
            run_id,
            step_id,
            allowed_task_statuses=frozenset({"running", "killed", "suspended"}),
            status="failed",
            error=error[:4000],
            finished_at=func.now(),
        ):
            return
        self._observe_step_finished(run_id, step_id, "failed")
        await self._append_step_event(run_id, step_id, status="failed", reason=error[:4000])

    async def record_step_skipped(self, run_id: str, step_id: str, *, definition_hash: str) -> None:
        from app.services.workflow_metrics import record_workflow_step

        if not await self._upsert(run_id, step_id, status="skipped", definition_hash=definition_hash):
            return
        record_workflow_step("skipped")
        await self._append_step_event(run_id, step_id, status="skipped")

    async def record_step_suspended(self, run_id: str, step_id: str, *, reason: str) -> None:
        if not await self._upsert(run_id, step_id, status="suspended", error=reason[:4000]):
            return
        self._observe_step_finished(run_id, step_id, "suspended")
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

    async def _upsert_leaf(
        self,
        run_id: str,
        step_id: str,
        leaf_id: str,
        *,
        allowed_task_statuses: frozenset[str] = frozenset({"running"}),
        **values: Any,
    ) -> bool:
        from app.models.workflow import WorkflowLeafCall

        run_uuid = uuid.UUID(run_id)
        async with self._session() as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_uuid).with_for_update())
            ).scalar_one()
            assert_runtime_task_fence(task)
            if task.status not in allowed_task_statuses:
                return False
            row = (
                await session.execute(
                    select(WorkflowLeafCall)
                    .where(
                        WorkflowLeafCall.run_id == run_uuid,
                        WorkflowLeafCall.step_id == step_id,
                        WorkflowLeafCall.leaf_id == leaf_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                row = WorkflowLeafCall(
                    tenant_id=uuid.UUID(str(self._tenant_id)),
                    run_id=run_uuid,
                    step_id=step_id,
                    leaf_id=leaf_id,
                )
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            return True

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

        if not await self._upsert_leaf(
            run_id,
            step_id,
            leaf_id,
            status="running",
            input_hash=input_hash,
            definition_hash=definition_hash,
            idempotency_key=idempotency_key,
            started_at=func.now(),
            error=None,
        ):
            return
        record_workflow_leaf_call("running")

    async def record_leaf_done(self, run_id: str, step_id: str, leaf_id: str, *, output: Any, tokens_used: int) -> None:
        from sqlalchemy import func
        from app.services.workflow_metrics import record_workflow_leaf_call

        if not await self._upsert_leaf(
            run_id,
            step_id,
            leaf_id,
            allowed_task_statuses=frozenset({"running", "killed", "suspended"}),
            status="done",
            result_ref=json.dumps(output, ensure_ascii=False, sort_keys=True),
            token_usage={"total": tokens_used},
            finished_at=func.now(),
        ):
            return
        record_workflow_leaf_call("done")

    async def record_leaf_failed(self, run_id: str, step_id: str, leaf_id: str, *, error: str) -> None:
        from sqlalchemy import func
        from app.services.workflow_metrics import record_workflow_leaf_call

        if not await self._upsert_leaf(
            run_id,
            step_id,
            leaf_id,
            allowed_task_statuses=frozenset({"running", "killed", "suspended"}),
            status="failed",
            error=error[:4000],
            finished_at=func.now(),
        ):
            return
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
        recovery_data_root: str | Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._estimate = estimate
        self._recovery_data_root = Path(recovery_data_root) if recovery_data_root is not None else None

    def _session(self):
        return tenant_scoped_session(str(self._tenant_id), session_factory=self._session_factory)

    async def reserve(self, run_id: str, *, reservation_key: str) -> bool:
        """Atomically reuse-or-create one logical leaf attempt reservation."""

        from sqlalchemy import text

        run_uuid = uuid.UUID(str(run_id))
        logical_key = str(reservation_key)
        charged = None
        async with self._session() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"workflow_quota:{run_uuid}:{logical_key}"},
            )
            latest = (
                await session.execute(
                    select(WorkflowQuotaReservation)
                    .where(
                        WorkflowQuotaReservation.run_id == run_uuid,
                        WorkflowQuotaReservation.logical_key == logical_key,
                    )
                    .order_by(WorkflowQuotaReservation.attempt.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if latest is not None and latest.settled_at is None:
                if latest.state in {"executing", "needs_reconciliation"}:
                    raise WorkflowAdmissionError(
                        f"workflow quota reservation {logical_key} requires reconciliation before replay"
                    )
                return True

            charged = (
                await session.execute(
                    text(
                        "UPDATE workflow_quotas SET consumed_tokens = consumed_tokens + :est, "
                        "updated_at = now() WHERE run_id = :rid "
                        "AND consumed_tokens + :est <= allocated_tokens RETURNING id"
                    ),
                    {"est": self._estimate, "rid": run_uuid},
                )
            ).scalar_one_or_none()
            if charged is not None:
                attempt = int(latest.attempt) + 1 if latest is not None else 1
                session.add(
                    WorkflowQuotaReservation(
                        tenant_id=uuid.UUID(str(self._tenant_id)),
                        run_id=run_uuid,
                        logical_key=logical_key,
                        reservation_key=f"{logical_key}:attempt-{attempt}",
                        attempt=attempt,
                        estimated_tokens=self._estimate,
                        state="reserved",
                    )
                )
        if charged is None:
            from app.services.workflow_metrics import record_workflow_quota_denial

            record_workflow_quota_denial()
        return charged is not None

    async def mark_execution_started(
        self,
        run_id: str,
        *,
        reservation_key: str,
        step_id: str,
        leaf_id: str | None,
        input_hash: str,
    ) -> None:
        from sqlalchemy import text

        run_uuid = uuid.UUID(str(run_id))
        logical_key = str(reservation_key)
        async with self._session() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"workflow_quota:{run_uuid}:{logical_key}"},
            )
            reservation = (
                await session.execute(
                    select(WorkflowQuotaReservation)
                    .where(
                        WorkflowQuotaReservation.run_id == run_uuid,
                        WorkflowQuotaReservation.logical_key == logical_key,
                    )
                    .order_by(WorkflowQuotaReservation.attempt.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reservation is None or reservation.settled_at is not None:
                raise RuntimeError(f"workflow quota reservation is not live for {logical_key}")
            if reservation.state != "reserved":
                raise RuntimeError(f"workflow quota reservation cannot start from state {reservation.state}")
            reservation.state = "executing"
            reservation.step_id = str(step_id)
            reservation.leaf_id = str(leaf_id or "singleton")
            reservation.input_hash = str(input_hash)
            reservation.execution_started_at = datetime.now(UTC)

    async def mark_execution_unknown(self, run_id: str, *, reservation_key: str, error: str) -> None:
        from sqlalchemy import text

        run_uuid = uuid.UUID(str(run_id))
        logical_key = str(reservation_key)
        async with self._session() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"workflow_quota:{run_uuid}:{logical_key}"},
            )
            task = (
                await session.execute(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.id == run_uuid,
                        RuntimeTask.tenant_id == uuid.UUID(str(self._tenant_id)),
                        RuntimeTask.task_type == "workflow",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None:
                raise RuntimeError(f"workflow quota reconciliation authority is missing for {logical_key}")
            reservation = (
                await session.execute(
                    select(WorkflowQuotaReservation)
                    .where(
                        WorkflowQuotaReservation.run_id == run_uuid,
                        WorkflowQuotaReservation.logical_key == logical_key,
                    )
                    .order_by(WorkflowQuotaReservation.attempt.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reservation is None or reservation.settled_at is not None:
                raise RuntimeError(f"workflow quota reconciliation authority is missing for {logical_key}")
            reservation.state = "needs_reconciliation"
            reservation.reconciliation_required_at = datetime.now(UTC)
            reservation.reconciliation_reason = str(error)[:4000]
            task.status = "needs_reconciliation"
            metadata = dict(task.metadata_json or {})
            pending = [
                dict(item)
                for item in metadata.get("workflow_quota_reconciliation_pending", [])
                if isinstance(item, dict) and str(item.get("reservation_key") or "") != logical_key
            ]
            pending.append(
                {
                    "reservation_id": str(reservation.id),
                    "reservation_key": logical_key,
                    "attempt": int(reservation.attempt),
                    "step_id": reservation.step_id,
                    "leaf_id": reservation.leaf_id,
                    "input_hash": reservation.input_hash,
                    "estimated_tokens": int(reservation.estimated_tokens),
                    "state": "needs_reconciliation",
                    "reason": str(error)[:4000],
                }
            )
            metadata["workflow_quota_reconciliation_pending"] = pending[-500:]
            if task.parent_agent_id is not None and reservation.step_id:
                identity = workflow_leaf_recovery_identity(
                    run_uuid,
                    reservation.step_id,
                    None if reservation.leaf_id == "singleton" else reservation.leaf_id,
                )
                targets = [
                    dict(item) for item in metadata.get("recovery_resolution_targets", []) if isinstance(item, dict)
                ]
                target = {
                    "agent_id": str(task.parent_agent_id),
                    "session_id": identity.session_id,
                    "runtime_task_id": identity.runtime_task_id,
                    "source": "workflow_leaf",
                    "workflow_step_id": reservation.step_id,
                    "workflow_leaf_id": reservation.leaf_id,
                }
                inspection = await asyncio.to_thread(
                    inspect_recovery_manifest_checkpoint,
                    agent_id=task.parent_agent_id,
                    tenant_id=task.tenant_id,
                    session_id=identity.session_id,
                    runtime_task_id=identity.runtime_task_id,
                    data_root=self._recovery_data_root,
                )
                target.update(reviewed_recovery_manifest_evidence(inspection))
                targets = _merge_workflow_recovery_targets(targets, [target])
                metadata["recovery_resolution_targets"] = sorted(
                    targets,
                    key=lambda item: (
                        str(item.get("workflow_step_id") or ""),
                        str(item.get("workflow_leaf_id") or ""),
                        str(item.get("runtime_task_id") or ""),
                        str(item.get("session_id") or ""),
                    ),
                )
                frames = [dict(item) for item in metadata.get("recovery_tool_frames", []) if isinstance(item, dict)]
                frame_id = f"workflow-quota:{reservation.id}"
                if not any(str(item.get("tool_call_id") or "") == frame_id for item in frames):
                    frames.append(
                        {
                            "runtime_task_id": identity.runtime_task_id,
                            "tool_call_id": frame_id,
                            "tool_name": "workflow_leaf_execution",
                            "status": "needs_reconciliation",
                            "event_type": "executor_outcome_unknown",
                            "reason": str(error)[:4000],
                        }
                    )
                metadata["recovery_tool_frames"] = frames
            else:
                reasons = set(metadata.get("recovery_evidence_incomplete_reasons") or [])
                reasons.add("workflow_quota_reconciliation_authority_missing")
                metadata["recovery_evidence_incomplete_reasons"] = sorted(reasons)
            incomplete_reasons = set(metadata.get("recovery_evidence_incomplete_reasons") or [])
            incomplete_reasons.add("workflow_fanout_evidence_aggregation_pending")
            metadata["recovery_evidence_incomplete_reasons"] = sorted(incomplete_reasons)
            metadata["recovery_evidence_status"] = (
                "incomplete" if metadata.get("recovery_evidence_incomplete_reasons") else "ready"
            )
            metadata["reconciliation_reason"] = "workflow_leaf_executor_outcome_unknown"
            metadata["reconciliation_retry_allowed"] = False
            metadata["side_effect_risk"] = "unknown"
            metadata["needs_reconciliation"] = True
            task.metadata_json = metadata
            if task.parent_session_id and task.parent_agent_id is not None and task.root_user_id is not None:
                await enqueue_completion_notification(
                    session,
                    CompletionNotification(
                        tenant_id=task.tenant_id,
                        source_kind="workflow",
                        source_run_id=str(task.id),
                        parent_session_id=task.parent_session_id,
                        parent_agent_id=task.parent_agent_id,
                        parent_user_id=task.root_user_id,
                        terminal_status="needs_reconciliation",
                        task_type="workflow",
                        summary="Workflow leaf execution outcome requires operator reconciliation.",
                        child_session_id=task.child_session_id,
                        delivery_mode="parent_continuation",
                        metadata={
                            "workflow_quota_reservation_id": str(reservation.id),
                            "workflow_quota_reservation_key": logical_key,
                            "reconciliation_reason": str(error)[:4000],
                        },
                        payload_rank=150,
                    ),
                )

    async def settle(self, run_id: str, actual_tokens: int, *, reservation_key: str) -> None:
        from sqlalchemy import text

        run_uuid = uuid.UUID(str(run_id))
        logical_key = str(reservation_key)
        async with self._session() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"workflow_quota:{run_uuid}:{logical_key}"},
            )
            reservation = (
                await session.execute(
                    select(WorkflowQuotaReservation)
                    .where(
                        WorkflowQuotaReservation.run_id == run_uuid,
                        WorkflowQuotaReservation.logical_key == logical_key,
                    )
                    .order_by(WorkflowQuotaReservation.attempt.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reservation is None:
                raise RuntimeError(f"workflow quota reservation missing for {logical_key}")
            if reservation.settled_at is not None:
                return
            actual = max(0, int(actual_tokens))
            updated = (
                await session.execute(
                    text(
                        "UPDATE workflow_quotas SET consumed_tokens = "
                        "GREATEST(0, consumed_tokens + :actual - :est), updated_at = now() "
                        "WHERE run_id = :rid RETURNING id"
                    ),
                    {
                        "actual": actual,
                        "est": int(reservation.estimated_tokens),
                        "rid": run_uuid,
                    },
                )
            ).scalar_one_or_none()
            if updated is None:
                raise RuntimeError(f"workflow quota row missing for run {run_uuid}")
            reservation.actual_tokens = actual
            reservation.state = "settled"
            reservation.settlement_reason = (
                "pre_execution_release" if reservation.execution_started_at is None and actual == 0 else "actual_usage"
            )
            reservation.settled_at = datetime.now(UTC)


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

    async def acquire(self, run_id: uuid.UUID | str) -> WorkflowRunLease:
        """Wait for deterministic ownership; used by short admission transactions."""

        from sqlalchemy import text

        connection = await self._engine.connect()
        try:
            await connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": self._key(run_id)})
        except Exception:
            await connection.close()
            raise
        return WorkflowRunLease(connection, self._key(run_id))


class PGAdmissionLeaseManager(PGRunLeaseManager):
    """Separate advisory namespace for reserve + RuntimeTask admission."""

    @staticmethod
    def _key(run_id: uuid.UUID | str) -> int:
        import zlib

        return zlib.crc32(f"wf_admission:{run_id}".encode()) & 0x7FFFFFFF


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
        recovery_data_root: str | Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gate_decider = (
            gate_decider if gate_decider is not None else CheckpointGateDecider(session_factory=session_factory)
        )
        self._lease_manager = PGRunLeaseManager(session_factory)
        self._admission_lease_manager = PGAdmissionLeaseManager(session_factory)
        self._recovery_data_root = Path(recovery_data_root) if recovery_data_root is not None else None
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

    async def _record_resume_at(
        self,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        step_id: str,
        resume_at,
    ) -> None:
        """Equivalent scheduling record (§9 P7): the suspended run carries its
        wake time in metadata; the startup scan resumes it once due. The once
        trigger binding lands in P8 (§6.2)."""
        async with self._session(tenant_id) as session:
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
            assert_runtime_task_fence(task)
            metadata = dict(task.metadata_json or {})
            metadata["resume_at"] = resume_at.isoformat()
            metadata["resume_step_id"] = str(step_id)
            task.metadata_json = metadata

    async def _clear_resume_at(self, run_id: uuid.UUID, tenant_id: uuid.UUID, *, step_id: str) -> None:
        async with self._session(tenant_id) as session:
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
            assert_runtime_task_fence(task)
            metadata = dict(task.metadata_json or {})
            if str(metadata.get("resume_step_id") or "") != str(step_id):
                return
            metadata.pop("resume_at", None)
            metadata.pop("resume_step_id", None)
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
        root_runtime_task_id: uuid.UUID | str | None = None,
        delegation_chain: list[str] | tuple[str, ...] | None = None,
        run_metadata: dict[str, Any] | None = None,
        enqueue_only: bool = False,
        activation_pending: bool = False,
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
        runtime_budget_service = budget_service or RuntimeBudgetService(session_factory=self._session_factory)
        budget_admission_status = "inherited" if budget_uuid is not None else None
        if budget_uuid is None:
            policy = await runtime_budget_service.resolve_policy(
                RuntimeBudgetPolicyLookup(
                    tenant_id=tenant_id,
                    source="workflow",
                    profile="workflow",
                    agent_id=agent_id,
                )
            )
            budget_run = await runtime_budget_service.create_run(
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
        budget_reservation_key = f"workflow:{run_id}:start"
        execution_admission = ExecutionAdmission(runtime_budget_service)
        admission_lease = await self._admission_lease_manager.acquire(run_id)
        try:
            admission_decision = await execution_admission.admit(
                RuntimeBudgetReservation(
                    budget_run_id=budget_uuid,
                    reservation_key=budget_reservation_key,
                    background_tasks=1,
                    reason="workflow_start",
                    runtime_task_id=run_id,
                    reopen_after_zero_settlement=True,
                    metadata={
                        "work_type": "workflow",
                        "definition_source": definition_source,
                        "parent_session_id": str(parent_session_id) if parent_session_id else None,
                    },
                )
            )
        except Exception:
            await admission_lease.release()
            raise
        if admission_decision.reservation is not None:
            budget_reservation_key = admission_decision.reservation.reservation_key
        budget_admission_status = "waiting_budget_approval" if admission_decision.waiting else "reserved"
        parent_session_value = str(parent_session_id) if parent_session_id else None
        root_session_value = str(root_session_id or parent_session_id) if root_session_id or parent_session_id else None
        root_runtime_task_value = _uuid_or_none(root_runtime_task_id) or run_id
        delegation_chain_value = [str(item) for item in (delegation_chain or ()) if str(item).strip()]
        if delegation_chain_value:
            delegation_chain_value.append(f"workflow:{run_id}")
        else:
            delegation_chain_value = [f"agent:{agent_id}", f"workflow:{run_id}"]
        initial_status = (
            "pending"
            if admission_decision.waiting
            else "suspended"
            if enqueue_only and activation_pending
            else "pending"
            if enqueue_only
            else "running"
        )
        metadata_json = {
            "definition_source": definition_source,
            "definition_hash": compiled.definition_hash,
            "args_hash": args_hash,
            "confirmed_plan_id": str(confirmed_plan_id) if confirmed_plan_id else None,
            "tenant_id": str(tenant_id),
            "delivery_target_json": delivery_target,
            "parent_session_id": parent_session_value,
            "root_session_id": root_session_value,
            "root_runtime_task_id": str(root_runtime_task_value),
            "delegation_chain": delegation_chain_value,
            "user_id": str(user_id) if user_id else None,
            "session_bound": bool(parent_session_value),
            # Ephemeral archive (§3.1): the run must be replayable
            # without the original conversation.
            "definition_json": compiled.definition.canonical_dict()
            if not isinstance(definition_data, dict)
            else definition_data,
            "args": args,
            "budget_run_id": str(budget_uuid) if budget_uuid else None,
            "budget_reservation_key": budget_reservation_key,
            "execution_admission_status": admission_decision.status,
            "activation_pending": bool(enqueue_only and activation_pending and not admission_decision.waiting),
            "activation_pending_since": (
                datetime.now(UTC).isoformat()
                if enqueue_only and activation_pending and not admission_decision.waiting
                else None
            ),
        }
        if run_metadata:
            metadata_json.update(run_metadata)
        trigger_root_idempotency_key = str(metadata_json.get("trigger_root_idempotency_key") or "").strip() or None
        trigger_parent_task_id = _uuid_or_none(metadata_json.get("trigger_runtime_task_id"))
        trigger_id = str(metadata_json.get("trigger_id") or "").strip() or None
        try:
            async with self._session(tenant_id) as session:
                if trigger_root_idempotency_key is not None:
                    if trigger_parent_task_id is None or trigger_id is None or parent_session_value is None:
                        raise WorkflowAdmissionError(
                            "stable trigger Workflow launch is missing parent, trigger, or session identity"
                        )
                    parent_task = (
                        await session.execute(
                            select(RuntimeTask)
                            .where(
                                RuntimeTask.id == trigger_parent_task_id,
                                RuntimeTask.tenant_id == tenant_id,
                                RuntimeTask.task_type == "trigger",
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if parent_task is None:
                        raise WorkflowAdmissionError("stable trigger Workflow parent RuntimeTask is unavailable")
                    parent_claim = dict(metadata_json.get("parent_trigger_claim") or {})
                    try:
                        claim_task_id = uuid.UUID(str(parent_claim.get("runtime_task_id")))
                        claim_version = int(parent_claim.get("claim_version"))
                    except (TypeError, ValueError, AttributeError) as exc:
                        raise WorkflowAdmissionError(
                            "stable trigger Workflow parent claim authority is missing or invalid"
                        ) from exc
                    claim_worker_id = str(parent_claim.get("worker_id") or "").strip()
                    if (
                        claim_task_id != parent_task.id
                        or parent_task.status != "running"
                        or not claim_worker_id
                        or parent_task.claimed_by != claim_worker_id
                        or int(parent_task.claim_version or 0) != claim_version
                    ):
                        raise WorkflowAdmissionError(
                            "stable trigger Workflow parent claim authority drifted before child commit"
                        )
                    root_user_uuid = _uuid_or_none(user_id)
                    if parent_task.parent_agent_id != agent_id:
                        raise WorkflowAdmissionError("stable trigger Workflow parent Agent authority drifted")
                    if root_user_uuid is None or parent_task.root_user_id != root_user_uuid:
                        raise WorkflowAdmissionError("stable trigger Workflow parent root user authority drifted")
                    expected_root_session = str(parent_task.root_session_id or parent_session_value)
                    if root_session_value != expected_root_session:
                        raise WorkflowAdmissionError("stable trigger Workflow parent root session authority drifted")
                    try:
                        session_ids = {
                            uuid.UUID(str(parent_session_value)),
                            uuid.UUID(str(expected_root_session)),
                        }
                    except (TypeError, ValueError, AttributeError) as exc:
                        raise WorkflowAdmissionError(
                            "stable trigger Workflow parent session authority is invalid"
                        ) from exc
                    session_rows = list(
                        (
                            await session.execute(
                                select(ChatSession).where(
                                    ChatSession.id.in_(session_ids),
                                    ChatSession.tenant_id == tenant_id,
                                    ChatSession.agent_id == agent_id,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(session_rows) != len(session_ids) or any(
                        row.user_id != root_user_uuid for row in session_rows
                    ):
                        raise WorkflowAdmissionError("stable trigger Workflow parent session user authority drifted")
                    parent_metadata = dict(parent_task.metadata_json or {})
                    children = dict(parent_metadata.get("workflow_children") or {})
                    expected_link = {"run_id": str(run_id), "session_id": parent_session_value}
                    existing_link = children.get(trigger_id)
                    if existing_link is not None and existing_link != expected_link:
                        raise WorkflowAdmissionError(
                            "trigger Workflow child identity conflicts with durable parent mapping"
                        )
                    children[trigger_id] = expected_link
                    parent_metadata["workflow_children"] = children
                    parent_task.metadata_json = parent_metadata
                task = RuntimeTask(
                    id=run_id,
                    task_type="workflow",
                    tenant_id=tenant_id,
                    status=initial_status,
                    parent_agent_id=agent_id,
                    parent_session_id=parent_session_value,
                    child_session_id=parent_session_value,
                    root_user_id=user_id,
                    root_session_id=root_session_value,
                    root_runtime_task_id=root_runtime_task_value,
                    delegation_chain_json=delegation_chain_value,
                    budget_run_id=budget_uuid,
                    budget_reservation_key=budget_reservation_key,
                    budget_admission_status=budget_admission_status,
                    budget_terminal_reason=("runtime_budget_approval_required" if admission_decision.waiting else None),
                    root_idempotency_key=trigger_root_idempotency_key or f"workflow:{run_id}",
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
        except Exception:
            exact_child_exists = False
            try:
                async with self._session(tenant_id) as child_session:
                    existing_task = (
                        await child_session.execute(
                            select(RuntimeTask).where(
                                RuntimeTask.id == run_id,
                                RuntimeTask.tenant_id == tenant_id,
                                RuntimeTask.task_type == "workflow",
                            )
                        )
                    ).scalar_one_or_none()
                    exact_child_exists = bool(
                        existing_task is not None
                        and existing_task.budget_run_id == budget_uuid
                        and existing_task.budget_reservation_key == budget_reservation_key
                    )
            except Exception:
                logger.exception("[Workflow] failed to inspect stable child admission receipt")
            if admission_decision.status == "admitted" and not exact_child_exists:
                try:
                    await execution_admission.settle(
                        admission_decision,
                        actual_background_tasks=0,
                        reason="workflow_ledger_create_failed",
                        runtime_task_id=run_id,
                        metadata={
                            "retryable_reservation": True,
                            "logical_reservation_key": f"workflow:{run_id}:start",
                        },
                    )
                except Exception:
                    logger.exception("[Workflow] failed to release reservation after run ledger rejection")
            await admission_lease.release()
            raise
        await admission_lease.release()

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
            status="pending" if enqueue_only or admission_decision.waiting else "running",
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

        if admission_decision.waiting:
            return WorkflowRunHandle(
                run_id=run_id,
                outcome=WorkflowRunOutcome(status="pending", reason="waiting_budget_approval"),
            )
        if enqueue_only and activation_pending:
            return WorkflowRunHandle(
                run_id=run_id,
                outcome=WorkflowRunOutcome(status="pending", reason="awaiting_usage_evidence"),
            )
        if enqueue_only:
            return WorkflowRunHandle(
                run_id=run_id,
                outcome=WorkflowRunOutcome(status="pending", reason="queued_for_worker_claim"),
            )

        execution_lease = await self._lease_manager.try_acquire(run_id)
        if execution_lease is None:
            outcome = WorkflowRunOutcome(
                status="suspended",
                reason="run lease held by another worker before initial execution",
            )
        else:
            try:
                outcome = await self._execute(
                    compiled,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    args=args,
                    leaf_executor=leaf_executor,
                )
            finally:
                await execution_lease.release()
        return WorkflowRunHandle(run_id=run_id, outcome=outcome)

    async def activate_staged_run(self, run_id: uuid.UUID, *, tenant_id: uuid.UUID) -> bool:
        """Publish a usage-authorized staged run to the shared claim queue."""

        async with self._session(tenant_id) as session:
            task = (
                await session.execute(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.id == run_id,
                        RuntimeTask.tenant_id == tenant_id,
                        RuntimeTask.task_type == "workflow",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None:
                return False
            metadata = dict(task.metadata_json or {})
            if task.status == "pending" and not metadata.get("activation_pending"):
                return True
            if task.status != "suspended" or task.claimed_by is not None or not metadata.get("activation_pending"):
                return False
            metadata.pop("activation_pending", None)
            metadata.pop("activation_pending_since", None)
            metadata.pop("activation_evidence_missing_observed_at", None)
            metadata["usage_evidence_committed"] = True
            task.metadata_json = metadata
            task.status = "pending"
            task.result_summary = "Workflow usage evidence committed; queued for worker claim."
            return True

    async def repair_pending_activations_once(
        self,
        *,
        limit: int = 100,
        task_ids: set[uuid.UUID] | None = None,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Recover staged trigger runs from their durable asset-usage receipt.

        A missing receipt is observed across two grace windows before failing so
        the worker cannot race a still-open usage transaction. Every mutation is
        a row-lock CAS and never creates a replacement Workflow child.
        """

        from app.models.ai_asset import AIAssetUsageEvent

        counts = {"activated": 0, "failed": 0}
        if task_ids is not None:
            selected_ids = {uuid.UUID(str(value)) for value in task_ids}
            if not selected_ids:
                return counts
        else:
            selected_ids = None
        requested_limit = max(1, min(int(limit), 500))
        scan_limit = min(500, max(100, requested_limit * 10))
        current = now or datetime.now(UTC)
        factory = self._session_factory or async_session
        async with factory() as locator_db:
            async with enter_rls_bypass(
                locator_db,
                reason="workflow activation-pending recovery locator scan",
            ) as bypass_db:
                locator_stmt = (
                    select(RuntimeTask.id, RuntimeTask.tenant_id)
                    .where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.status == "suspended",
                        RuntimeTask.claimed_by.is_(None),
                        RuntimeTask.metadata_json["activation_pending"].astext == "true",
                    )
                    .order_by(
                        RuntimeTask.metadata_json["activation_repair_deferred_at"].astext.asc().nullsfirst(),
                        RuntimeTask.created_at.asc(),
                        RuntimeTask.id.asc(),
                    )
                    .limit(scan_limit)
                )
                if selected_ids is not None:
                    locator_stmt = locator_stmt.where(RuntimeTask.id.in_(selected_ids))
                locator_rows = (await bypass_db.execute(locator_stmt)).all()
        for run_id, tenant_id in locator_rows:
            if counts["activated"] + counts["failed"] >= requested_limit:
                break
            should_fail = False
            async with self._session(tenant_id) as session:
                task = (
                    await session.execute(
                        select(RuntimeTask)
                        .where(
                            RuntimeTask.id == run_id,
                            RuntimeTask.tenant_id == tenant_id,
                            RuntimeTask.task_type == "workflow",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if task is None or task.status != "suspended" or task.claimed_by is not None:
                    continue
                metadata = dict(task.metadata_json or {})
                if not metadata.get("activation_pending"):
                    continue
                receipt = (
                    await session.execute(
                        select(AIAssetUsageEvent.id).where(
                            AIAssetUsageEvent.tenant_id == tenant_id,
                            AIAssetUsageEvent.usage_kind == "workflow_run",
                            AIAssetUsageEvent.idempotency_key == f"workflow-run:{run_id}",
                            AIAssetUsageEvent.runtime_task_id == str(run_id),
                        )
                    )
                ).scalar_one_or_none()
                if receipt is not None:
                    metadata.pop("activation_pending", None)
                    metadata.pop("activation_pending_since", None)
                    metadata.pop("activation_evidence_missing_observed_at", None)
                    metadata.pop("activation_repair_deferred_at", None)
                    metadata["usage_evidence_committed"] = True
                    metadata["usage_evidence_receipt_id"] = str(receipt)
                    task.metadata_json = metadata
                    task.status = "pending"
                    task.result_summary = "Workflow usage evidence recovered; queued for worker claim."
                    counts["activated"] += 1
                    continue

                observed_at_raw = metadata.get("activation_evidence_missing_observed_at")
                try:
                    observed_at = datetime.fromisoformat(str(observed_at_raw)) if observed_at_raw else None
                except ValueError:
                    observed_at = None
                created_at = task.created_at or current
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                age_seconds = max(0.0, (current - created_at).total_seconds())
                missing_seconds = max(0.0, (current - observed_at).total_seconds()) if observed_at else 0.0
                metadata["activation_repair_deferred_at"] = current.isoformat()
                if age_seconds >= _ACTIVATION_EVIDENCE_GRACE_SECONDS and (
                    observed_at is None or missing_seconds < _ACTIVATION_EVIDENCE_GRACE_SECONDS
                ):
                    metadata["activation_evidence_missing_observed_at"] = current.isoformat()
                elif observed_at is not None and missing_seconds >= _ACTIVATION_EVIDENCE_GRACE_SECONDS:
                    should_fail = True
                task.metadata_json = metadata
            if should_fail and await self.fail_staged_run(
                run_id,
                tenant_id=tenant_id,
                reason="usage_evidence_missing_after_recovery_grace",
            ):
                counts["failed"] += 1
        return counts

    async def fail_staged_run(
        self,
        run_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        reason: str,
    ) -> bool:
        """Terminalize an unclaimable run when its usage authority fails."""

        budget_uuid: uuid.UUID | None = None
        budget_reservation_key: str | None = None
        async with self._session(tenant_id) as session:
            task = (
                await session.execute(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.id == run_id,
                        RuntimeTask.tenant_id == tenant_id,
                        RuntimeTask.task_type == "workflow",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None:
                return False
            if task.status == "failed" and (task.metadata_json or {}).get("activation_failure") == reason:
                return True
            if task.status != "suspended" or task.claimed_by is not None:
                return False
            metadata = dict(task.metadata_json or {})
            metadata.pop("activation_pending", None)
            metadata.pop("activation_pending_since", None)
            metadata.pop("activation_evidence_missing_observed_at", None)
            metadata["activation_failure"] = reason
            task.metadata_json = metadata
            task.status = "failed"
            task.result_summary = f"Workflow activation failed before execution: {reason}"
            task.completed_at = datetime.now(UTC)
            budget_uuid = task.budget_run_id
            budget_reservation_key = task.budget_reservation_key

        if budget_uuid is not None and budget_reservation_key:
            reservation = RuntimeBudgetReservation(
                budget_run_id=budget_uuid,
                reservation_key=budget_reservation_key,
                background_tasks=1,
                runtime_task_id=run_id,
                metadata={"work_type": "workflow", "workflow_run_id": str(run_id)},
            )
            await ExecutionAdmission(RuntimeBudgetService(session_factory=self._session_factory)).settle(
                ExecutionAdmissionDecision(
                    status="admitted",
                    reservation=reservation,
                    budget_run_id=budget_uuid,
                ),
                actual_background_tasks=0,
                reason="workflow_activation_failed",
                runtime_task_id=run_id,
            )
        return True

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

    async def _inspect_leaf_recovery_manifest(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        run_id: uuid.UUID,
        step_id: str,
        leaf_id: str | None,
    ) -> tuple[WorkflowRecoveryManifestAssessment, dict[str, Any], str]:
        identity = workflow_leaf_recovery_identity(run_id, step_id, leaf_id)
        inspection = await asyncio.to_thread(
            inspect_recovery_manifest_checkpoint,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=identity.session_id,
            runtime_task_id=identity.runtime_task_id,
            data_root=self._recovery_data_root,
        )
        inspection_state = str((inspection or {}).get("state") or "missing")
        manifest: RecoveryManifest | None = None
        if inspection_state == "valid":
            manifest = RecoveryManifest(
                session_id=identity.session_id,
                agent_id=str(agent_id),
                tenant_id=str(tenant_id),
                runtime_task_id=identity.runtime_task_id,
                claim_version=(inspection or {}).get("expected_claim_version"),
                claim_worker_id=(inspection or {}).get("expected_claim_worker_id"),
                checkpoint_seq=(inspection or {}).get("expected_checkpoint_seq"),
                pending_tool_frames=[
                    dict(frame)
                    for frame in (inspection or {}).get("pending_tool_frames", [])
                    if isinstance(frame, dict)
                ],
                recent_tool_outcomes=[
                    dict(outcome)
                    for outcome in (inspection or {}).get("recent_tool_outcomes", [])
                    if isinstance(outcome, dict)
                ],
                recent_writes=[str(path) for path in (inspection or {}).get("recent_writes", [])],
                current_turn_writes=[str(path) for path in (inspection or {}).get("current_turn_writes", [])],
                recovery_reconciliation_blocked=bool((inspection or {}).get("recovery_reconciliation_blocked")),
                reconciliation_resolution=dict((inspection or {}).get("reconciliation_resolution") or {}),
            )
        assessment = assess_workflow_recovery_manifest(
            manifest,
            canonical_path_present=inspection_state != "missing",
        )
        target: dict[str, Any] = {
            "agent_id": str(agent_id),
            "session_id": identity.session_id,
            "runtime_task_id": identity.runtime_task_id,
            "source": "workflow_leaf",
            "workflow_step_id": identity.step_id,
            "workflow_leaf_id": identity.leaf_id,
            **reviewed_recovery_manifest_evidence(inspection),
        }
        receipt = (inspection or {}).get("receipt") if isinstance(inspection, dict) else None
        if isinstance(receipt, dict):
            if receipt.get("ref"):
                target["expected_manifest_ref"] = receipt["ref"]
            if receipt.get("sha256"):
                target["expected_sha256"] = receipt["sha256"]
        if inspection_state == "valid":
            if (inspection or {}).get("expected_checkpoint_seq") is not None:
                target["expected_checkpoint_seq"] = (inspection or {})["expected_checkpoint_seq"]
            if (inspection or {}).get("expected_claim_version") is not None:
                target["expected_claim_version"] = (inspection or {})["expected_claim_version"]
            if (inspection or {}).get("expected_claim_worker_id") is not None:
                target["expected_claim_worker_id"] = (inspection or {})["expected_claim_worker_id"]
        return assessment, target, inspection_state

    async def _quarantine_unsafe_inflight_recovery(
        self,
        *,
        loaded: LoadedWorkflowRun,
        compiled: CompiledWorkflow,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        definition_hash: str | None,
    ) -> WorkflowRunOutcome | None:
        replayable_steps = {row.step_id: row for row in loaded.steps if row.status not in {"done", "skipped"}}
        if not replayable_steps:
            return None

        loaded_metadata = dict(loaded.task.metadata_json or {})
        aggregation_pending = "workflow_fanout_evidence_aggregation_pending" in {
            str(reason).strip()
            for reason in loaded_metadata.get("recovery_evidence_incomplete_reasons", [])
            if str(reason).strip()
        }
        aggregation_incomplete_reasons: set[str] = set()
        incidents: dict[tuple[str, str], dict[str, Any]] = {}
        external_step_ids = {
            step.id for step in compiled.definition.steps if step.effects in ("external", "irreversible")
        }
        leaf_rows_by_step: dict[str, list[Any]] = {}
        for leaf_row in loaded.leaf_calls:
            if leaf_row.status != "done":
                leaf_rows_by_step.setdefault(leaf_row.step_id, []).append(leaf_row)

        for step in compiled.definition.steps:
            step_row = replayable_steps.get(step.id)
            if step_row is None:
                continue
            replayable_leaf_rows = sorted(leaf_rows_by_step.get(step.id, []), key=lambda row: row.leaf_id)
            candidate_leaf_ids: list[str | None]
            if isinstance(step, AgentStep):
                candidate_leaf_ids = [None]
            elif isinstance(step, FanoutStep):
                candidate_leaf_ids = [row.leaf_id for row in replayable_leaf_rows]
            else:
                candidate_leaf_ids = []
            for candidate_leaf_id in candidate_leaf_ids:
                assessment = WorkflowRecoveryManifestAssessment(requires_reconciliation=False)
                target: dict[str, Any] | None = None
                inspection_state = "missing"
                if loaded.task.parent_agent_id is not None:
                    assessment, target, inspection_state = await self._inspect_leaf_recovery_manifest(
                        tenant_id=tenant_id,
                        agent_id=loaded.task.parent_agent_id,
                        run_id=run_id,
                        step_id=step.id,
                        leaf_id=candidate_leaf_id,
                    )
                elif aggregation_pending:
                    aggregation_incomplete_reasons.add("workflow_leaf_manifest_agent_authority_missing")
                if aggregation_pending and inspection_state != "valid":
                    leaf_key = candidate_leaf_id or "singleton"
                    aggregation_incomplete_reasons.add(
                        f"workflow_leaf_manifest_{inspection_state}:{step.id}:{leaf_key}"
                    )
                declared_side_effect_unknown = bool(
                    step.id in external_step_ids
                    and step_row.status in {"running", "failed", "unknown_requires_reconciliation"}
                    and not assessment.operator_retry_authorized
                )
                if not assessment.requires_reconciliation and not declared_side_effect_unknown:
                    continue
                leaf_key = candidate_leaf_id or "singleton"
                incident_reason = assessment.reason or "workflow_declared_side_effect_in_flight"
                frames: list[dict[str, Any]] = []
                for index, raw_frame in enumerate(assessment.unsafe_frames):
                    if not isinstance(raw_frame, dict):
                        continue
                    raw_call_id = str(raw_frame.get("tool_call_id") or "").strip()
                    if not raw_call_id or raw_call_id.startswith("completed-"):
                        raw_call_id = f"workflow:{step.id}:{leaf_key}:{raw_call_id or index}"
                    frames.append(
                        {
                            "runtime_task_id": str(run_id),
                            "tool_call_id": raw_call_id,
                            "tool_name": str(raw_frame.get("tool_name") or "unknown_workflow_tool"),
                            "status": "needs_reconciliation",
                            "event_type": "workflow_leaf_recovery_reconciliation_required",
                            "reason": incident_reason,
                            "workflow_step_id": step.id,
                            "workflow_leaf_id": leaf_key,
                        }
                    )
                if declared_side_effect_unknown and not frames:
                    frames.append(
                        {
                            "runtime_task_id": str(run_id),
                            "tool_call_id": f"workflow:{step.id}:{leaf_key}:declared-side-effect",
                            "tool_name": "workflow_declared_side_effect",
                            "status": "needs_reconciliation",
                            "event_type": "workflow_leaf_recovery_reconciliation_required",
                            "reason": incident_reason,
                            "workflow_step_id": step.id,
                            "workflow_leaf_id": leaf_key,
                        }
                    )
                incidents[(step.id, leaf_key)] = {
                    "step_id": step.id,
                    "leaf_id": leaf_key,
                    "reason": incident_reason,
                    "target": target,
                    "frames": frames,
                    "unsafe_tools": sorted({str(frame["tool_name"]) for frame in frames}),
                }

        if not incidents and not aggregation_pending:
            return None

        if not incidents:
            async with self._session(tenant_id) as session:
                task = (
                    await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id).with_for_update())
                ).scalar_one()
                metadata = dict(task.metadata_json or {})
                incomplete_reasons = {
                    str(reason).strip()
                    for reason in metadata.get("recovery_evidence_incomplete_reasons", [])
                    if str(reason).strip()
                }
                incomplete_reasons.discard("workflow_fanout_evidence_aggregation_pending")
                incomplete_reasons.update(aggregation_incomplete_reasons)
                if incomplete_reasons:
                    metadata["recovery_evidence_incomplete_reasons"] = sorted(incomplete_reasons)
                    metadata["recovery_evidence_status"] = "incomplete"
                else:
                    metadata.pop("recovery_evidence_incomplete_reasons", None)
                    metadata["recovery_evidence_status"] = "ready"
                metadata["reconciliation_retry_allowed"] = False
                task.metadata_json = metadata
            return WorkflowRunOutcome(
                status="suspended",
                reason="workflow live recovery evidence aggregation completed without replayable incidents",
            )

        ordered_incidents = [incidents[key] for key in sorted(incidents)]
        affected_step_ids = sorted({incident["step_id"] for incident in ordered_incidents})
        affected_leaf_keys = {
            (incident["step_id"], incident["leaf_id"])
            for incident in ordered_incidents
            if incident["leaf_id"] != "singleton"
        }
        affected_recovery_leaf_keys = {
            (str(incident["step_id"]), str(incident["leaf_id"])) for incident in ordered_incidents
        }
        new_targets = [dict(incident["target"]) for incident in ordered_incidents if incident["target"]]
        new_frames = [
            dict(frame)
            for incident in ordered_incidents
            for frame in incident.get("frames", [])
            if isinstance(frame, dict)
        ]
        reason = (
            str(ordered_incidents[0]["reason"])
            if len(ordered_incidents) == 1
            else "workflow_multiple_recovery_incidents"
        )
        retry_allowed = all(
            str(incident["reason"])
            in {
                "workflow_pending_tool_not_replay_safe",
                "workflow_declared_side_effect_in_flight",
            }
            for incident in ordered_incidents
        )

        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id).with_for_update())
            ).scalar_one()
            metadata = dict(task.metadata_json or {})
            step_rows = (
                (
                    await session.execute(
                        select(WorkflowStep)
                        .where(WorkflowStep.run_id == run_id, WorkflowStep.step_id.in_(affected_step_ids))
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for step_row in step_rows:
                step_row.status = "unknown_requires_reconciliation"
            if affected_leaf_keys:
                leaf_rows = (
                    (
                        await session.execute(
                            select(WorkflowLeafCall).where(WorkflowLeafCall.run_id == run_id).with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                for leaf_row in leaf_rows:
                    if (leaf_row.step_id, leaf_row.leaf_id) in affected_leaf_keys:
                        leaf_row.status = "needs_reconciliation"
            existing_targets = [
                dict(target) for target in metadata.get("recovery_resolution_targets", []) if isinstance(target, dict)
            ]
            merged_targets = _merge_workflow_recovery_targets(existing_targets, new_targets)
            existing_frames = [
                dict(frame) for frame in metadata.get("recovery_tool_frames", []) if isinstance(frame, dict)
            ]
            for frame in existing_frames:
                frame["runtime_task_id"] = str(frame.get("runtime_task_id") or run_id)
            merged_frames = _merge_workflow_recovery_frames(
                existing_frames,
                new_frames,
                replaced_leaf_keys=affected_recovery_leaf_keys,
            )
            incomplete_reasons = {
                str(value).strip()
                for value in metadata.get("recovery_evidence_incomplete_reasons", [])
                if str(value).strip()
            }
            if aggregation_pending:
                incomplete_reasons.discard("workflow_fanout_evidence_aggregation_pending")
                incomplete_reasons.update(aggregation_incomplete_reasons)
            retry_allowed = retry_allowed and not incomplete_reasons
            metadata.update(
                {
                    "needs_reconciliation": affected_step_ids,
                    "reconciliation_status": "open",
                    "reconciliation_reason": reason,
                    "reconciliation_retry_allowed": retry_allowed,
                    "side_effect_risk": "unknown_prior_side_effect",
                    "workflow_recovery_incidents": [
                        {
                            "step_id": incident["step_id"],
                            "leaf_id": incident["leaf_id"],
                            "reason": incident["reason"],
                            "unsafe_tools": incident["unsafe_tools"],
                        }
                        for incident in ordered_incidents
                    ],
                    "recovery_resolution_targets": merged_targets,
                    "recovery_tool_frames": merged_frames,
                }
            )
            if incomplete_reasons:
                metadata["recovery_evidence_incomplete_reasons"] = sorted(incomplete_reasons)
                metadata["recovery_evidence_status"] = "incomplete"
            else:
                metadata.pop("recovery_evidence_incomplete_reasons", None)
                metadata["recovery_evidence_status"] = "ready"
            task.status = "needs_reconciliation"
            task.completed_at = None
            task.result_summary = "Workflow recovery stopped before leaf replay; operator reconciliation is required."
            task.metadata_json = metadata

        await self._audit(
            "workflow_run_needs_reconciliation",
            tenant_id=tenant_id,
            run_id=run_id,
            definition_hash=definition_hash,
            extra={
                "steps": affected_step_ids,
                "incidents": [
                    {
                        "step_id": incident["step_id"],
                        "leaf_id": incident["leaf_id"],
                        "reason": incident["reason"],
                    }
                    for incident in ordered_incidents
                ],
            },
        )
        return WorkflowRunOutcome(
            status="suspended",
            reason=(f"workflow leaf recovery requires reconciliation before replay: {affected_step_ids}"),
        )

    async def _finalize_live_reconciliation_evidence(
        self,
        *,
        compiled: CompiledWorkflow,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """Close the fanout evidence barrier only after every in-flight leaf settles."""

        loaded = await self.load_run(run_id, tenant_id=tenant_id)
        if loaded is None or loaded.task.status != "needs_reconciliation":
            return
        incomplete_reasons = {
            str(reason).strip()
            for reason in (loaded.task.metadata_json or {}).get("recovery_evidence_incomplete_reasons", [])
            if str(reason).strip()
        }
        if "workflow_fanout_evidence_aggregation_pending" not in incomplete_reasons:
            return
        await self._quarantine_unsafe_inflight_recovery(
            loaded=loaded,
            compiled=compiled,
            run_id=run_id,
            tenant_id=tenant_id,
            definition_hash=(loaded.task.metadata_json or {}).get("definition_hash"),
        )

    async def repair_unsettled_quota_reservations_once(
        self,
        *,
        limit: int = 100,
        task_ids: set[uuid.UUID] | None = None,
    ) -> dict[str, int]:
        """Converge quota receipts left by a dead Workflow worker.

        The run advisory lease is the execution fence: a live worker keeps its
        receipt untouched. Reserved means the executor boundary was never
        crossed and is safely released; executing is an unknown outcome and is
        promoted to operator reconciliation without guessing token usage.
        """

        summary = {"settled_reserved": 0, "quarantined_executing": 0}
        if task_ids is not None:
            selected_ids = {uuid.UUID(str(value)) for value in task_ids}
            if not selected_ids:
                return summary
        else:
            selected_ids = None
        requested_limit = max(1, min(int(limit), 500))
        scan_limit = requested_limit
        factory = self._session_factory or async_session
        async with factory() as locator_db:
            async with enter_rls_bypass(
                locator_db,
                reason="workflow unsettled quota reservation repair locator scan",
            ) as bypass_db:
                locator_stmt = (
                    select(
                        WorkflowQuotaReservation.id,
                        WorkflowQuotaReservation.run_id,
                        WorkflowQuotaReservation.tenant_id,
                        WorkflowQuotaReservation.state,
                        WorkflowQuotaReservation.logical_key,
                    )
                    .where(WorkflowQuotaReservation.state.in_(("reserved", "executing")))
                    .order_by(
                        WorkflowQuotaReservation.repair_deferred_at.asc().nullsfirst(),
                        WorkflowQuotaReservation.created_at.asc(),
                        WorkflowQuotaReservation.id.asc(),
                    )
                    .limit(scan_limit)
                )
                if selected_ids is not None:
                    locator_stmt = locator_stmt.where(WorkflowQuotaReservation.run_id.in_(selected_ids))
                locator_rows = (await bypass_db.execute(locator_stmt)).all()
        handled = 0
        for reservation_id, run_id, tenant_id, state, logical_key in locator_rows:
            if handled >= requested_limit:
                break
            lease = await self._lease_manager.try_acquire(run_id)
            if lease is None:
                async with self._session(tenant_id) as session:
                    busy_reservation = (
                        await session.execute(
                            select(WorkflowQuotaReservation)
                            .where(
                                WorkflowQuotaReservation.id == reservation_id,
                                WorkflowQuotaReservation.tenant_id == tenant_id,
                                WorkflowQuotaReservation.state.in_(("reserved", "executing")),
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if busy_reservation is not None:
                        busy_reservation.repair_deferred_at = datetime.now(UTC)
                continue
            try:
                quota = PGQuotaReserver(
                    self._session_factory,
                    tenant_id,
                    estimate=int(get_settings().WORKFLOW_LEAF_TOKEN_ESTIMATE),
                    recovery_data_root=self._recovery_data_root,
                )
                if state == "reserved":
                    await quota.settle(str(run_id), 0, reservation_key=str(logical_key))
                    summary["settled_reserved"] += 1
                else:
                    await quota.mark_execution_unknown(
                        str(run_id),
                        reservation_key=str(logical_key),
                        error="workflow worker disappeared after executor dispatch",
                    )
                    summary["quarantined_executing"] += 1
                handled += 1
            except (RuntimeError, WorkflowAdmissionError):
                logger.exception("[Workflow] unsettled quota reservation repair failed for run %s", run_id)
            finally:
                await lease.release()
        return summary

    async def repair_pending_live_reconciliation_evidence(self, *, limit: int = 100) -> list[uuid.UUID]:
        """Recover a fanout evidence barrier left behind by a dead worker.

        The first live incident moves the run out of the normal claim queue, so
        the shared maintenance tick must explicitly finish this byte-bound
        manifest aggregation.  A per-run advisory lease serializes the repair
        with a still-alive Workflow worker.
        """

        aggregation_marker = "workflow_fanout_evidence_aggregation_pending"
        requested_limit = max(1, min(int(limit), 500))
        # Over-fetch a bounded page. Lease-busy rows are durably rotated below,
        # so every restart makes progress without an in-memory cursor.
        scan_limit = min(500, max(100, requested_limit * 10))
        factory = self._session_factory or async_session
        async with factory() as locator_db:
            async with enter_rls_bypass(
                locator_db,
                reason="workflow fanout evidence aggregation repair locator scan",
            ) as bypass_db:
                locator_rows = (
                    await bypass_db.execute(
                        select(RuntimeTask.id, RuntimeTask.tenant_id)
                        .where(
                            RuntimeTask.status == "needs_reconciliation",
                            RuntimeTask.task_type == "workflow",
                            cast(RuntimeTask.metadata_json, JSONB).contains(
                                {"recovery_evidence_incomplete_reasons": [aggregation_marker]}
                            ),
                        )
                        .order_by(
                            RuntimeTask.metadata_json["workflow_evidence_repair_deferred_at"].astext.asc().nullsfirst(),
                            RuntimeTask.created_at.asc(),
                            RuntimeTask.id.asc(),
                        )
                        .limit(scan_limit)
                    )
                ).all()
        repaired: list[uuid.UUID] = []
        for run_id, tenant_id in locator_rows:
            if len(repaired) >= requested_limit:
                break
            lease = await self._lease_manager.try_acquire(run_id)
            if lease is None:
                async with self._session(tenant_id) as session:
                    busy_task = (
                        await session.execute(
                            select(RuntimeTask)
                            .where(
                                RuntimeTask.id == run_id,
                                RuntimeTask.tenant_id == tenant_id,
                                RuntimeTask.status == "needs_reconciliation",
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if busy_task is not None:
                        busy_metadata = dict(busy_task.metadata_json or {})
                        busy_reasons = set(busy_metadata.get("recovery_evidence_incomplete_reasons") or [])
                        if aggregation_marker in busy_reasons:
                            busy_metadata["workflow_evidence_repair_deferred_at"] = datetime.now(UTC).isoformat()
                            busy_task.metadata_json = busy_metadata
                continue
            try:
                loaded = await self.load_run(run_id, tenant_id=tenant_id)
                if loaded is None:
                    continue
                loaded_reasons = {
                    str(reason).strip()
                    for reason in (loaded.task.metadata_json or {}).get("recovery_evidence_incomplete_reasons", [])
                    if str(reason).strip()
                }
                if aggregation_marker not in loaded_reasons:
                    continue
                definition_data = (loaded.task.metadata_json or {}).get("definition_json")
                if not isinstance(definition_data, dict):
                    async with self._session(tenant_id) as session:
                        poisoned = (
                            await session.execute(
                                select(RuntimeTask)
                                .where(
                                    RuntimeTask.id == run_id,
                                    RuntimeTask.tenant_id == tenant_id,
                                    RuntimeTask.status == "needs_reconciliation",
                                )
                                .with_for_update()
                            )
                        ).scalar_one_or_none()
                        if poisoned is not None:
                            metadata = dict(poisoned.metadata_json or {})
                            reasons = {
                                str(reason).strip()
                                for reason in metadata.get("recovery_evidence_incomplete_reasons", [])
                                if str(reason).strip() and str(reason).strip() != aggregation_marker
                            }
                            session_id = str(poisoned.parent_session_id or poisoned.root_session_id or "").strip()
                            if poisoned.parent_agent_id is not None and session_id:
                                targets = [
                                    dict(item)
                                    for item in metadata.get("recovery_resolution_targets", [])
                                    if isinstance(item, dict)
                                ]
                                target = {
                                    "agent_id": str(poisoned.parent_agent_id),
                                    "session_id": session_id,
                                    "runtime_task_id": str(poisoned.id),
                                    "source": "workflow_evidence_dead_letter",
                                }
                                inspection = await asyncio.to_thread(
                                    inspect_recovery_manifest_checkpoint,
                                    agent_id=poisoned.parent_agent_id,
                                    tenant_id=tenant_id,
                                    session_id=session_id,
                                    runtime_task_id=poisoned.id,
                                    data_root=self._recovery_data_root,
                                )
                                target.update(reviewed_recovery_manifest_evidence(inspection))
                                targets = _merge_workflow_recovery_targets(targets, [target])
                                metadata["recovery_resolution_targets"] = targets
                                frames = [
                                    dict(item)
                                    for item in metadata.get("recovery_tool_frames", [])
                                    if isinstance(item, dict)
                                ]
                                frame_id = f"workflow-evidence:{poisoned.id}"
                                if not any(str(item.get("tool_call_id") or "") == frame_id for item in frames):
                                    frames.append(
                                        {
                                            "runtime_task_id": str(poisoned.id),
                                            "tool_call_id": frame_id,
                                            "tool_name": "workflow_fanout_evidence_aggregation",
                                            "status": "needs_reconciliation",
                                            "event_type": "archived_definition_missing",
                                            "reason": "workflow evidence cannot be reconstructed without definition_json",
                                        }
                                    )
                                metadata["recovery_tool_frames"] = frames
                            else:
                                reasons.add("workflow_reconciliation_authority_missing")
                            metadata["recovery_evidence_incomplete_reasons"] = sorted(reasons)
                            metadata["recovery_evidence_status"] = "ready" if not reasons else "incomplete"
                            metadata["reconciliation_reason"] = "workflow_fanout_evidence_dead_letter"
                            metadata["reconciliation_retry_allowed"] = False
                            metadata["needs_reconciliation"] = True
                            metadata["workflow_fanout_evidence_dead_letter"] = {
                                "reason": "archived_definition_missing",
                                "dead_lettered_at": datetime.now(UTC).isoformat(),
                                "operator_action_required": True,
                            }
                            poisoned.metadata_json = metadata
                    logger.error(
                        "[Workflow] run %s dead-lettered evidence repair without archived definition",
                        run_id,
                    )
                    continue
                await self._finalize_live_reconciliation_evidence(
                    compiled=compile_workflow(definition_data),
                    run_id=run_id,
                    tenant_id=tenant_id,
                )
                refreshed = await self.load_run(run_id, tenant_id=tenant_id)
                refreshed_reasons = {
                    str(reason).strip()
                    for reason in ((refreshed.task.metadata_json or {}) if refreshed else {}).get(
                        "recovery_evidence_incomplete_reasons", []
                    )
                    if str(reason).strip()
                }
                if aggregation_marker not in refreshed_reasons:
                    repaired.append(run_id)
            finally:
                await lease.release()
        return repaired

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
        assert_runtime_task_fence(loaded.task)
        metadata = loaded.task.metadata_json or {}
        if loaded.task.status == "needs_reconciliation":
            return WorkflowRunOutcome(
                status="suspended",
                reason="workflow run is blocked on operator reconciliation",
            )
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

        quarantine_outcome = await self._quarantine_unsafe_inflight_recovery(
            loaded=loaded,
            compiled=compiled,
            run_id=run_id,
            tenant_id=tenant_id,
            definition_hash=archived_hash,
        )
        if quarantine_outcome is not None:
            return quarantine_outcome

        async with self._session(tenant_id) as session:
            task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
            assert_runtime_task_fence(task)
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

        from app.models.workflow import WorkflowDefinitionRecord, WorkflowPromotionProposal

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
                        select(WorkflowDefinitionRecord.id, WorkflowDefinitionRecord.promoted_from_run_id)
                        .where(
                            WorkflowDefinitionRecord.promoted_from_run_id.in_(run_ids),
                            WorkflowDefinitionRecord.promotion_proposal_id.is_not(None),
                            WorkflowDefinitionRecord.status == "active",
                        )
                        .join(
                            WorkflowPromotionProposal,
                            WorkflowPromotionProposal.id == WorkflowDefinitionRecord.promotion_proposal_id,
                        )
                        .where(
                            WorkflowPromotionProposal.status == "approved",
                            WorkflowPromotionProposal.run_id == WorkflowDefinitionRecord.promoted_from_run_id,
                            WorkflowPromotionProposal.definition_hash == WorkflowDefinitionRecord.definition_hash,
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

    # ── startup requeue/resume ─────────────────────────────────────

    async def _scheduled_wait_is_due(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        metadata: dict[str, Any],
        now: datetime,
    ) -> bool:
        if metadata.get("waiting_for_signal") or metadata.get("needs_reconciliation"):
            return False
        resume_at_raw = metadata.get("resume_at")
        resume_step_id = str(metadata.get("resume_step_id") or "").strip()
        if not resume_at_raw or not resume_step_id:
            return False
        try:
            resume_at = datetime.fromisoformat(str(resume_at_raw))
        except ValueError:
            logger.warning("[Workflow] run %s has malformed resume_at %r", run_id, resume_at_raw)
            return False
        if resume_at.tzinfo is None:
            resume_at = resume_at.replace(tzinfo=UTC)
        if resume_at > now:
            return False
        async with self._session(tenant_id) as session:
            step = (
                await session.execute(
                    select(WorkflowStep).where(
                        WorkflowStep.run_id == run_id,
                        WorkflowStep.step_id == resume_step_id,
                    )
                )
            ).scalar_one_or_none()
        return bool(step is not None and step.status == "suspended" and step.step_type == "wait_until_step")

    async def requeue_run_for_worker(
        self,
        run_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        reason: str,
    ) -> WorkflowRunOutcome | None:
        """Move a dormant workflow back behind the shared RuntimeTask claim fence."""

        lease = await self._lease_manager.try_acquire(run_id)
        if lease is None:
            return None
        try:
            async with self._session(tenant_id) as session:
                task = (
                    await session.execute(
                        select(RuntimeTask)
                        .where(
                            RuntimeTask.id == run_id,
                            RuntimeTask.tenant_id == tenant_id,
                            RuntimeTask.task_type == "workflow",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if task is None or task.status not in {"running", "suspended", "resumable"}:
                    return None
                metadata = dict(task.metadata_json or {})
                if metadata.get("needs_reconciliation"):
                    return None
                if task.status == "running" and task.claim_expires_at is not None:
                    expires_at = task.claim_expires_at
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=UTC)
                    if expires_at > datetime.now(UTC):
                        return None

                task.status = "resumable"
                task.claimed_by = None
                task.claim_expires_at = None
                metadata.update(
                    {
                        "recovery_state": "queued_for_claim",
                        "workflow_requeue_reason": str(reason or "workflow_daemon_requeue"),
                        "workflow_requeued_at": datetime.now(UTC).isoformat(),
                    }
                )
                task.metadata_json = metadata
        finally:
            await lease.release()

        try:
            from app.services.runtime_task_worker import notify_runtime_task_worker

            await notify_runtime_task_worker(reason=str(reason or "workflow_daemon_requeue"), runtime_task_id=run_id)
        except Exception as exc:
            logger.warning("[Workflow] runtime worker requeue wakeup failed for %s: %s", run_id, exc)
        return WorkflowRunOutcome(
            status="suspended",
            reason=f"workflow run requeued behind shared RuntimeTask claim: {reason}",
        )

    async def requeue_pending_runs(self) -> list[uuid.UUID]:
        """Requeue stale running and due time-suspended runs; never execute leaves here."""

        records = await list_active_runtime_task_records(
            statuses=_RESUMABLE_STATUSES,
            task_types=("workflow",),
            limit=None,
            session_factory=self._session_factory,
        )
        requeued: list[uuid.UUID] = []
        now = datetime.now(UTC)
        for record in records:
            metadata = dict(record.get("metadata") or {})
            if metadata.get("needs_reconciliation"):
                continue
            status = str(record.get("status") or "")
            tenant_value = metadata.get("tenant_id") or record.get("tenant_id")
            if not tenant_value:
                logger.warning("[Workflow] run %s has no tenant mirror; skipping worker requeue", record.get("task_id"))
                continue
            run_id = uuid.UUID(str(record["task_id"]))
            tenant_id = uuid.UUID(str(tenant_value))
            reason = "workflow_expired_claim_requeue"
            if status == "suspended":
                if not await self._scheduled_wait_is_due(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    metadata=metadata,
                    now=now,
                ):
                    continue
                reason = "workflow_scheduled_resume_due"
            outcome = await self.requeue_run_for_worker(
                run_id,
                tenant_id=tenant_id,
                reason=reason,
            )
            if outcome is not None:
                requeued.append(run_id)
        return requeued

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
            tenant_id = uuid.UUID(str(tenant_value))
            if run_status == "suspended":
                if not await self._scheduled_wait_is_due(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    metadata=dict(metadata),
                    now=datetime.now(UTC),
                ):
                    continue
            try:
                outcome = await self.resume_run(run_id, tenant_id=tenant_id, leaf_executor=leaf_executor)
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
            await write_audit_log(
                action,
                details=details,
                agent_id=agent_id,
                session_factory=self._session_factory,
            )
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

    async def _enqueue_parent_task_notification_side_effect(
        self,
        *,
        session: AsyncSession,
        task: RuntimeTask,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        status: str,
        agent_id: uuid.UUID | None,
        parent_session_id: str | uuid.UUID | None,
        summary: str,
    ) -> tuple[bool, dict[str, Any]]:
        metadata = dict(task.metadata_json or {})
        if agent_id is None or not parent_session_id:
            return False, metadata
        parent_session_uuid = uuid.UUID(str(parent_session_id))
        parent_session = (
            await session.execute(
                select(ChatSession).where(
                    ChatSession.id == parent_session_uuid,
                    ChatSession.agent_id == agent_id,
                    ChatSession.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        owner_id = _uuid_or_none(metadata.get("user_id")) or getattr(parent_session, "user_id", None)
        if parent_session is None or owner_id is None:
            return False, metadata
        outbox_id = await enqueue_completion_notification(
            session,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="workflow",
                source_run_id=str(run_id),
                parent_session_id=parent_session_uuid,
                parent_agent_id=agent_id,
                parent_user_id=owner_id,
                terminal_status=status,
                task_type="workflow",
                summary=summary,
                delivery_mode="parent_continuation",
                artifacts=list(metadata.get("artifacts") or []),
                metadata={
                    "workflow_run_id": str(run_id),
                    "parent_agent_id": str(agent_id),
                    "parent_session_id": str(parent_session_uuid),
                    "workflow_session_state": status,
                    **({"budget_run_id": str(metadata["budget_run_id"])} if metadata.get("budget_run_id") else {}),
                },
            ),
        )
        metadata["completion_outbox_id"] = str(outbox_id)
        reply_target = (
            metadata.get("delivery_target_json") or metadata.get("reply_target") or metadata.get("delivery_target")
        )
        if isinstance(reply_target, dict) and str(reply_target.get("channel") or "").lower() not in {"", "web"}:
            from app.models.channel_config import ChannelConfig
            from app.services.channel_delivery_outbox import ChannelDeliveryIntent, enqueue_channel_delivery

            channel = str(reply_target["channel"]).strip().lower()
            config = (
                await session.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.tenant_id == tenant_id,
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == ("microsoft_teams" if channel == "teams" else channel),
                    )
                )
            ).scalar_one_or_none()
            channel_outbox_id = await enqueue_channel_delivery(
                session,
                ChannelDeliveryIntent(
                    tenant_id=tenant_id,
                    runtime_task_id=run_id,
                    agent_id=agent_id,
                    session_id=parent_session_uuid,
                    user_id=owner_id,
                    channel_config_id=getattr(config, "id", None),
                    delivery_target=reply_target,
                    text=summary,
                    terminal_status=status,
                    metadata={
                        "source": "workflow_runtime",
                        "workflow_run_id": str(run_id),
                    },
                ),
            )
            metadata["channel_delivery_outbox_id"] = str(channel_outbox_id)
        task.metadata_json = metadata
        await session.flush()
        return True, metadata

    async def _claim_parent_task_notification_side_effect(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        status: str,
        agent_id: uuid.UUID | None,
        parent_session_id: str | uuid.UUID | None,
        summary: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Idempotent fallback for legacy terminal transactions."""

        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id).with_for_update())
            ).scalar_one()
            claimed, metadata = await self._enqueue_parent_task_notification_side_effect(
                session=session,
                task=task,
                run_id=run_id,
                tenant_id=tenant_id,
                status=status,
                agent_id=agent_id,
                parent_session_id=parent_session_id,
                summary=summary,
            )
            await session.flush()
            return claimed, metadata

    async def _claim_completion_side_effects(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        status: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Atomically persist the retryable completion-signal intent."""

        from app.services.workflow_completion_outbox import (
            WorkflowCompletionIntent,
            enqueue_workflow_completion,
        )

        async with self._session(tenant_id) as session:
            task = (
                await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id).with_for_update())
            ).scalar_one()
            metadata = dict(task.metadata_json or {})
            if task.parent_agent_id is None:
                return False, metadata
            from app.models.agent import Agent

            signal_agent = (
                await session.execute(
                    select(Agent).where(
                        Agent.id == task.parent_agent_id,
                        Agent.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if signal_agent is None:
                metadata["completion_side_effects"] = {
                    "idempotency_key": f"workflow_completed:{run_id}:{status}",
                    "status": status,
                    "delivery_status": "skipped_invalid_agent_authority",
                    "signal_type": "workflow_completed",
                }
                task.metadata_json = metadata
                return False, metadata
            outbox_id = await enqueue_workflow_completion(
                session,
                WorkflowCompletionIntent(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    agent_id=task.parent_agent_id,
                    terminal_status=status,
                ),
            )
            metadata["completion_side_effects"] = {
                "idempotency_key": f"workflow_completed:{run_id}:{status}",
                "status": status,
                "delivery_status": "queued",
                "signal_type": "workflow_completed",
                "outbox_id": str(outbox_id),
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
        quota = PGQuotaReserver(
            self._session_factory,
            tenant_id,
            estimate=get_settings().WORKFLOW_LEAF_TOKEN_ESTIMATE,
            recovery_data_root=self._recovery_data_root,
        )

        service = self

        class _MetadataWaitScheduler:
            async def schedule_resume(self, rid: str, *, step_id: str, resume_at) -> None:
                await service._record_resume_at(
                    uuid.UUID(rid),
                    tenant_id,
                    step_id=step_id,
                    resume_at=resume_at,
                )

            async def clear_resume(self, rid: str, *, step_id: str) -> None:
                await service._clear_resume_at(uuid.UUID(rid), tenant_id, step_id=step_id)

        wait_scheduler = _MetadataWaitScheduler()

        class _MetadataSignalWaitRegistrar:
            async def register_wait(self, rid: str, *, step_id: str, signal_type: str) -> None:
                async with service._session(tenant_id) as session:
                    task = (
                        await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(rid)))
                    ).scalar_one()
                    assert_runtime_task_fence(task)
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
                task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one_or_none()
            if task is None:
                return False
            assert_runtime_task_fence(task)
            return task.status == "running"

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
            try:
                await self._finalize_live_reconciliation_evidence(
                    compiled=compiled,
                    run_id=run_id,
                    tenant_id=tenant_id,
                )
            except Exception as aggregation_exc:  # noqa: BLE001 - pending marker remains fail-closed
                logger.error(
                    "[Workflow] live reconciliation evidence aggregation failed for run %s: %s",
                    run_id,
                    aggregation_exc,
                )
            # Engine/leaf raised out of contract (e.g. process-crash
            # simulation in tests): leave the run 'running' so the startup
            # scan can pick it up, then surface the error.
            raise

        try:
            await self._finalize_live_reconciliation_evidence(
                compiled=compiled,
                run_id=run_id,
                tenant_id=tenant_id,
            )
        except Exception as aggregation_exc:  # noqa: BLE001 - pending marker remains fail-closed
            logger.error(
                "[Workflow] live reconciliation evidence aggregation failed for run %s: %s",
                run_id,
                aggregation_exc,
            )

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
            assert_runtime_task_fence(task)
            if outcome.status == "killed" and task.status == "suspended":
                # The stop flag the engine saw was an admin force-suspend, not a
                # kill: report it truthfully and keep the operator's state.
                outcome = WorkflowRunOutcome(
                    status="suspended",
                    reason="force-suspended by operator; stopped at step boundary",
                    outputs=outcome.outputs,
                )
            elif task.status == "killed" and outcome.status != "killed":
                outcome = WorkflowRunOutcome(
                    status="killed",
                    reason="workflow killed before terminal journal commit",
                    outputs=outcome.outputs,
                )
            elif task.status not in {"running", "killed"}:
                outcome = WorkflowRunOutcome(
                    status="suspended",
                    reason=f"workflow execution stopped because RuntimeTask moved to {task.status}",
                    outputs=outcome.outputs,
                )
            if task.status == "running":
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
            if outcome.status in {"completed", "failed", "killed"}:
                completion_summary = self._completion_task_summary(
                    run_id=run_id,
                    status=outcome.status,
                    reason=outcome.reason,
                    outputs=outcome.outputs,
                )
                task.result_summary = completion_summary
                task.completed_at = datetime.now(UTC)
                _claimed, task_metadata = await self._enqueue_parent_task_notification_side_effect(
                    session=session,
                    task=task,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    status=outcome.status,
                    agent_id=agent_for_signal,
                    parent_session_id=task_metadata.get("parent_session_id") or task.parent_session_id,
                    summary=completion_summary,
                )

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
        if outcome.status == "completed":
            _queued, task_metadata = await self._claim_completion_side_effects(
                run_id=run_id,
                tenant_id=tenant_id,
                status=outcome.status,
            )
        if outcome.status in {"completed", "failed", "killed"}:
            budget_uuid = _uuid_or_none(task_metadata.get("budget_run_id"))
            reservation_key = str(task_metadata.get("budget_reservation_key") or "").strip()
            if budget_uuid is not None and reservation_key:
                reservation = RuntimeBudgetReservation(
                    budget_run_id=budget_uuid,
                    reservation_key=reservation_key,
                    background_tasks=1,
                    runtime_task_id=run_id,
                    metadata={"work_type": "workflow", "workflow_run_id": str(run_id)},
                )
                await ExecutionAdmission(RuntimeBudgetService(session_factory=self._session_factory)).settle(
                    ExecutionAdmissionDecision(
                        status="admitted",
                        reservation=reservation,
                        budget_run_id=budget_uuid,
                    ),
                    actual_background_tasks=1,
                    reason=f"workflow_{outcome.status}",
                    runtime_task_id=run_id,
                )
                async with self._session(tenant_id) as session:
                    settled_task = (
                        await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))
                    ).scalar_one()
                    settled_task.budget_admission_status = "settled"
        return outcome
