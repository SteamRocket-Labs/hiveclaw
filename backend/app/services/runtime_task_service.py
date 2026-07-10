"""Persistence helpers for runtime delegation tasks."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.runtime.tenant_admission import raise_runtime_tenant_precondition
from app.services.runtime_tenant_admission import admit_agent_runtime_tenant
from app.services.tenant_resolver import resolve_tenant_for_agent, resolve_tenant_for_runtime_task


logger = logging.getLogger(__name__)


_RESTART_RESUMABLE_TASK_TYPES = (
    "workflow",
    "web_chat_turn",
    "team_member",
    "goal_continuation",
    "advanced_plan",
)
_TERMINAL_STATUSES = {"completed", "failed", "killed", "skipped", "needs_reconciliation"}
RUNTIME_RESTART_REPLAY_CONTRACT_SCHEMA = "runtime_restart_replay_contract.v1"
RUNTIME_RESTART_REPLAY_JOURNAL_SCHEMA = "runtime_restart_replay_journal.v1"
RUNTIME_RECONCILIATION_RETRY_CONTRACT_SCHEMA = "runtime_reconciliation_retry_contract.v1"


def _coerce_task_id(task_id: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(task_id, uuid.UUID):
        return task_id
    try:
        return uuid.UUID(str(task_id))
    except (ValueError, TypeError, AttributeError):
        return None


def _task_to_dict(task: RuntimeTask) -> dict[str, Any]:
    return {
        "task_id": task.id.hex,
        "task_type": task.task_type,
        "status": task.status,
        "tenant_id": str(task.tenant_id) if task.tenant_id else None,
        "parent_agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else None,
        "child_agent_name": task.child_agent_name,
        "prompt": task.prompt,
        "result": task.result_summary,
        "trace_id": task.trace_id,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "depth": task.depth,
        "budget_run_id": str(task.budget_run_id) if task.budget_run_id else None,
        "budget_reservation_key": task.budget_reservation_key,
        "budget_admission_status": task.budget_admission_status,
        "budget_terminal_reason": task.budget_terminal_reason,
        "claim_version": int(getattr(task, "claim_version", 0) or 0),
        "root_idempotency_key": getattr(task, "root_idempotency_key", None),
        "config_snapshot_hash": getattr(task, "config_snapshot_hash", None),
        "policy_snapshot_hash": getattr(task, "policy_snapshot_hash", None),
        "metadata": task.metadata_json or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def _group_runtime_task_locators(
    rows: list[Any],
    *,
    operation: str,
) -> tuple[list[uuid.UUID], dict[uuid.UUID, list[uuid.UUID]]]:
    """Keep cross-tenant scans metadata-only, then group safe tenant reads."""
    ordered_ids: list[uuid.UUID] = []
    ids_by_tenant: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row in rows:
        task_id, tenant_id = row
        normalized_task_id = _coerce_task_id(task_id)
        normalized_tenant_id = _coerce_task_id(tenant_id) if tenant_id else None
        if normalized_task_id is None:
            logger.error("Skipping malformed RuntimeTask locator during %s: %r", operation, task_id)
            continue
        if normalized_tenant_id is None:
            logger.error(
                "Skipping tenantless RuntimeTask %s during %s; repair its tenant_id before replay.",
                normalized_task_id,
                operation,
            )
            continue
        ordered_ids.append(normalized_task_id)
        ids_by_tenant.setdefault(normalized_tenant_id, []).append(normalized_task_id)
    return ordered_ids, ids_by_tenant


def _is_restart_resumable_runtime_task(task: RuntimeTask) -> bool:
    task_type = getattr(task, "task_type", None)
    if task_type in _RESTART_RESUMABLE_TASK_TYPES:
        return True

    metadata = dict(getattr(task, "metadata_json", None) or {})
    task_id = getattr(task, "id", None)
    task_id_text = task_id.hex if isinstance(task_id, uuid.UUID) else str(task_id or "")
    if task_type in {"trigger", "heartbeat"}:
        resume_flag = "resumable_trigger" if task_type == "trigger" else "resumable_heartbeat"
        return bool(
            metadata.get("resume_after_restart")
            and metadata.get(resume_flag)
            and has_restart_replay_contract(metadata, task_type=str(task_type), task_id=task_id_text)
        )
    return bool(
        metadata.get("resume_after_restart")
        and (metadata.get("resumable_delegation") or metadata.get("resumable_subagent"))
    )


def build_restart_replay_contract(
    *,
    task_type: str,
    task_id: str,
    side_effect_risk: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    normalized_task_type = str(task_type or "runtime_task").strip() or "runtime_task"
    normalized_task_id = str(task_id or "").strip()
    return {
        "schema": RUNTIME_RESTART_REPLAY_CONTRACT_SCHEMA,
        "idempotency_key": f"{normalized_task_type}:{normalized_task_id}:restart",
        "task_type": normalized_task_type,
        "task_id": normalized_task_id,
        "trace_id": str(trace_id or "").strip() or None,
        "session_id": str(session_id or "").strip() or None,
        "side_effect_risk": str(side_effect_risk or "unknown").strip() or "unknown",
        "mode": "durable_restart_replay",
        "requires_completion_journal": True,
    }


def has_restart_replay_contract(
    metadata: dict[str, Any] | None,
    *,
    task_type: str,
    task_id: str | None = None,
) -> bool:
    contract = (metadata or {}).get("restart_replay_contract")
    if not isinstance(contract, dict):
        return False
    if contract.get("schema") != RUNTIME_RESTART_REPLAY_CONTRACT_SCHEMA:
        return False
    if str(contract.get("task_type") or "") != str(task_type or ""):
        return False
    if task_id is not None and str(contract.get("task_id") or "") not in {"", str(task_id)}:
        return False
    return bool(str(contract.get("idempotency_key") or "").strip())


def build_restart_replay_journal_entry(
    *,
    task_type: str,
    task_id: str,
    side_effect_risk: str,
    phase: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    normalized_task_type = str(task_type or "runtime_task").strip() or "runtime_task"
    normalized_task_id = str(task_id or "").strip()
    normalized_phase = str(phase or "").strip() or "unknown"
    return {
        "schema": RUNTIME_RESTART_REPLAY_JOURNAL_SCHEMA,
        "idempotency_key": f"{normalized_task_type}:{normalized_task_id}:restart:{normalized_phase}",
        "task_type": normalized_task_type,
        "task_id": normalized_task_id,
        "phase": normalized_phase,
        "trace_id": str(trace_id or "").strip() or None,
        "session_id": str(session_id or "").strip() or None,
        "side_effect_risk": str(side_effect_risk or "unknown").strip(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def merge_restart_replay_journal(
    metadata: dict[str, Any] | None,
    entry: dict[str, Any],
    *,
    limit: int = 20,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    journal = [item for item in merged.get("restart_replay_journal", []) if isinstance(item, dict)]
    key = entry.get("idempotency_key")
    if key:
        journal = [item for item in journal if item.get("idempotency_key") != key]
    journal.append(entry)
    merged["restart_replay_journal"] = journal[-limit:]
    return merged


def has_mutating_restart_replay_journal(
    metadata: dict[str, Any] | None,
    *,
    task_type: str,
    task_id: str | None = None,
) -> bool:
    journal = (metadata or {}).get("restart_replay_journal")
    if not isinstance(journal, list):
        return False
    for entry in journal:
        if not isinstance(entry, dict):
            continue
        if entry.get("schema") != RUNTIME_RESTART_REPLAY_JOURNAL_SCHEMA:
            continue
        if str(entry.get("task_type") or "") != str(task_type or ""):
            continue
        if task_id is not None and str(entry.get("task_id") or "") not in {"", str(task_id)}:
            continue
        if str(entry.get("side_effect_risk") or "") != "mutating":
            continue
        if str(entry.get("phase") or "") == "spawn_intent_recorded":
            return True
    return False


def build_completion_journal_entry(
    *,
    task_type: str,
    task_id: str,
    status: str,
    trace_id: str | None = None,
    session_id: str | None = None,
    side_effect_risk: str = "unknown",
    summary: str | None = None,
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().lower()
    return {
        "schema": "runtime_completion_journal.v1",
        "idempotency_key": f"{task_type}:{task_id}:{normalized_status}",
        "task_type": str(task_type or "").strip() or "runtime_task",
        "task_id": str(task_id or "").strip(),
        "status": normalized_status,
        "trace_id": str(trace_id or "").strip() or None,
        "session_id": str(session_id or "").strip() or None,
        "side_effect_risk": str(side_effect_risk or "unknown").strip(),
        "summary": (summary or "").strip()[:1000],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def merge_completion_journal(
    metadata: dict[str, Any] | None,
    entry: dict[str, Any],
    *,
    limit: int = 20,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    journal = [item for item in merged.get("completion_journal", []) if isinstance(item, dict)]
    key = entry.get("idempotency_key")
    if key:
        journal = [item for item in journal if item.get("idempotency_key") != key]
    journal.append(entry)
    merged["completion_journal"] = journal[-limit:]
    return merged


def _build_restart_reconciliation_retry_contract(
    *,
    task_type: str,
    task_id: str,
    blocker: str,
) -> dict[str, Any] | None:
    normalized_task_type = str(task_type or "").strip()
    normalized_blocker = str(blocker or "").strip()
    if normalized_task_type != "subagent" or normalized_blocker != "non_idempotent_subagent_type":
        return None
    return {
        "schema": RUNTIME_RECONCILIATION_RETRY_CONTRACT_SCHEMA,
        "kind": "audited_subagent_restart_retry",
        "task_type": normalized_task_type,
        "task_id": str(task_id or "").strip(),
        "blocker": normalized_blocker,
        "requires_human_approval": True,
        "retry_mode": "restart_from_prompt",
        "side_effect_risk": "mutating",
    }


def has_restart_reconciliation_retry_contract(
    metadata: dict[str, Any] | None,
    *,
    task_type: str,
    task_id: str,
    blocker: str,
) -> bool:
    data = dict(metadata or {})
    if data.get("reconciliation_status") != "retry_requested":
        return False
    if not bool(data.get("reconciliation_retry_allowed")):
        return False
    contract = data.get("reconciliation_retry_contract")
    if not isinstance(contract, dict):
        return False
    return (
        contract.get("schema") == RUNTIME_RECONCILIATION_RETRY_CONTRACT_SCHEMA
        and contract.get("kind") == "audited_subagent_restart_retry"
        and str(contract.get("task_type") or "") == str(task_type or "")
        and str(contract.get("task_id") or "") == str(task_id or "")
        and str(contract.get("blocker") or "") == str(blocker or "")
        and contract.get("requires_human_approval") is True
        and str(contract.get("retry_mode") or "") == "restart_from_prompt"
    )


def build_restart_reconciliation_metadata(
    metadata: dict[str, Any] | None,
    *,
    task_type: str,
    task_id: str,
    blocker: str,
    summary: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Mark a non-idempotent restarted task as requiring side-effect reconciliation."""

    merged = dict(metadata or {})
    merged.update(
        {
            "resume_failed": True,
            "orphaned_by_restart": True,
            "needs_reconciliation": True,
            "reconciliation_reason": blocker,
            "restart_resume_blocker": blocker,
            "side_effect_risk": "mutating",
        }
    )
    retry_contract = _build_restart_reconciliation_retry_contract(
        task_type=task_type,
        task_id=task_id,
        blocker=blocker,
    )
    if retry_contract is not None:
        merged["reconciliation_retry_allowed"] = True
        merged["reconciliation_retry_contract"] = retry_contract
    else:
        merged.pop("reconciliation_retry_allowed", None)
        merged.pop("reconciliation_retry_contract", None)
    entry = build_completion_journal_entry(
        task_type=task_type,
        task_id=task_id,
        status="needs_reconciliation",
        trace_id=trace_id,
        session_id=session_id,
        side_effect_risk="mutating",
        summary=summary,
    )
    return merge_completion_journal(merged, entry)


async def create_runtime_task_record(
    *,
    task_id: str,
    task_type: str = "delegation",
    status: str = "pending",
    parent_agent_id: uuid.UUID | None = None,
    child_agent_id: uuid.UUID | None = None,
    child_agent_name: str | None = None,
    prompt: str | None = None,
    trace_id: str | None = None,
    parent_session_id: str | None = None,
    child_session_id: str | None = None,
    depth: int = 1,
    metadata_json: dict[str, Any] | None = None,
    budget_run_id: uuid.UUID | None = None,
    budget_reservation_key: str | None = None,
    budget_admission_status: str | None = None,
    budget_terminal_reason: str | None = None,
    root_idempotency_key: str | None = None,
    config_snapshot_hash: str | None = None,
    policy_snapshot_hash: str | None = None,
) -> str:
    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        raise ValueError(f"Invalid runtime task id: {task_id!r}")

    started_at = datetime.now(timezone.utc) if status == "running" else None
    # Stage-2b: runtime_tasks now carries a tenant_id (RLS). Derive it from the
    # parent agent so the INSERT lands inside the agent's tenant. A declared
    # parent agent whose tenant cannot be resolved is a blocked precondition,
    # not a valid NULL-tenant runtime task. No parent_agent still means an
    # orphan/backfill record and keeps the historical fail-closed None scope.
    if parent_agent_id is not None:
        admission = await admit_agent_runtime_tenant(
            parent_agent_id,
            source=f"runtime_task:{task_type}",
            tenant_resolver=resolve_tenant_for_agent,
        )
        if not admission.ok:
            raise_runtime_tenant_precondition(admission)
        tenant_id = admission.tenant_id
    else:
        tenant_id = None
    async with tenant_scoped_session(
        tenant_id,
        require_tenant=parent_agent_id is not None,
        source=f"runtime_task:{task_type}",
    ) as db:
        try:
            db.add(
                RuntimeTask(
                    id=runtime_task_id,
                    task_type=task_type,
                    status=status,
                    parent_agent_id=parent_agent_id,
                    child_agent_id=child_agent_id,
                    child_agent_name=child_agent_name,
                    prompt=prompt,
                    trace_id=trace_id,
                    parent_session_id=parent_session_id,
                    child_session_id=child_session_id,
                    depth=depth,
                    metadata_json=metadata_json,
                    started_at=started_at,
                    tenant_id=tenant_id,
                    budget_run_id=budget_run_id,
                    budget_reservation_key=budget_reservation_key,
                    budget_admission_status=budget_admission_status,
                    budget_terminal_reason=budget_terminal_reason,
                    root_idempotency_key=root_idempotency_key or f"{task_type}:{runtime_task_id}",
                    config_snapshot_hash=config_snapshot_hash,
                    policy_snapshot_hash=policy_snapshot_hash,
                )
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return runtime_task_id.hex


async def update_runtime_task_record(task_id: str, **fields: Any) -> bool:
    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        return False

    tenant_id = await resolve_tenant_for_runtime_task(runtime_task_id, session_factory=async_session)
    if tenant_id is None:
        return False
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="runtime_task_status_update",
    ) as db:
        try:
            result = await db.execute(select(RuntimeTask).where(RuntimeTask.id == runtime_task_id))
            task = result.scalar_one_or_none()
            if task is None:
                return False

            from app.services.runtime_task_fence import assert_runtime_task_fence

            assert_runtime_task_fence(task)

            for key, value in fields.items():
                if hasattr(task, key):
                    if key == "metadata_json" and isinstance(value, dict) and isinstance(task.metadata_json, dict):
                        merged = dict(task.metadata_json)
                        merged.update(value)
                        setattr(task, key, merged)
                        continue
                    setattr(task, key, value)

            now = datetime.now(timezone.utc)
            status = fields.get("status")
            if status in _TERMINAL_STATUSES:
                existing_metadata = dict(getattr(task, "metadata_json", None) or {})
                persisted_task_id = getattr(task, "id", None)
                persisted_task_id_text = (
                    persisted_task_id.hex if isinstance(persisted_task_id, uuid.UUID) else str(task_id)
                )
                entry = build_completion_journal_entry(
                    task_type=str(getattr(task, "task_type", None) or "runtime_task"),
                    task_id=persisted_task_id_text,
                    status=str(status),
                    trace_id=fields.get("trace_id") or getattr(task, "trace_id", None),
                    session_id=(
                        fields.get("child_session_id")
                        or fields.get("parent_session_id")
                        or getattr(task, "child_session_id", None)
                    ),
                    side_effect_risk=str(existing_metadata.get("side_effect_risk") or "unknown"),
                    summary=fields.get("result_summary") or getattr(task, "result_summary", None),
                )
                task.metadata_json = merge_completion_journal(task.metadata_json, entry)
            if status == "running" and task.started_at is None:
                task.started_at = now
            if status in _TERMINAL_STATUSES and task.completed_at is None:
                task.completed_at = now

            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return True


async def get_runtime_task_record(task_id: str) -> dict[str, Any] | None:
    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        return None

    tenant_id = await resolve_tenant_for_runtime_task(runtime_task_id, session_factory=async_session)
    if tenant_id is None:
        return None
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="runtime_task_status_read",
    ) as db:
        try:
            result = await db.execute(select(RuntimeTask).where(RuntimeTask.id == runtime_task_id))
            task = result.scalar_one_or_none()
            if task is None:
                return None
            return _task_to_dict(task)
        except Exception:
            await db.rollback()
            raise


async def list_runtime_task_records(
    *,
    parent_agent_id: uuid.UUID | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    # Listing a single agent's runtime tasks → scope to that agent's tenant.
    # When no parent_agent is given (cross-agent listing) the tenant is unknown;
    # resolve_tenant_for_agent(None) → None pins an empty GUC (fail-closed),
    # which is the correct safe default for an unscoped list.
    tenant_id = await resolve_tenant_for_agent(parent_agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        try:
            stmt = select(RuntimeTask).order_by(RuntimeTask.created_at.desc()).limit(limit)
            if parent_agent_id is not None:
                stmt = stmt.where(RuntimeTask.parent_agent_id == parent_agent_id)
            result = await db.execute(stmt)
            tasks = result.scalars().all()
        except Exception:
            await db.rollback()
            raise
    return [_task_to_dict(task) for task in tasks]


async def list_active_runtime_task_records(
    *,
    statuses: tuple[str, ...] = ("pending", "running"),
    task_types: tuple[str, ...] | None = None,
    oldest_started_first: bool = False,
    limit: int | None = 50,
    session_factory: Any | None = None,
) -> list[dict[str, Any]]:
    # The global startup pass may only locate task/tenant pairs. Full records
    # are re-read under each tenant's RLS scope before they reach consumers.
    factory = session_factory or async_session
    async with (
        factory() as db,
        enter_rls_bypass(db, reason="restart-safe async-delegation resume scan"),
    ):
        try:
            stmt = select(RuntimeTask.id, RuntimeTask.tenant_id).where(RuntimeTask.status.in_(statuses))
            if task_types:
                stmt = stmt.where(RuntimeTask.task_type.in_(task_types))
            if oldest_started_first:
                stmt = stmt.order_by(
                    RuntimeTask.started_at.asc().nulls_last(),
                    RuntimeTask.created_at.asc(),
                )
            else:
                stmt = stmt.order_by(RuntimeTask.created_at.asc())
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await db.execute(stmt)
            locator_rows = result.all()
        except Exception:
            await db.rollback()
            raise

    ordered_ids, ids_by_tenant = _group_runtime_task_locators(
        locator_rows,
        operation="restart-safe active-task scan",
    )
    tasks_by_id: dict[uuid.UUID, RuntimeTask] = {}
    for tenant_id, task_ids in ids_by_tenant.items():
        async with tenant_scoped_session(
            tenant_id,
            session_factory=factory,
            require_tenant=True,
            source="restart_safe_active_task_hydration",
        ) as db:
            hydration_filters = [
                RuntimeTask.id.in_(task_ids),
                RuntimeTask.status.in_(statuses),
            ]
            if task_types:
                hydration_filters.append(RuntimeTask.task_type.in_(task_types))
            result = await db.execute(select(RuntimeTask).where(*hydration_filters))
            tasks_by_id.update((task.id, task) for task in result.scalars().all())

    return [_task_to_dict(tasks_by_id[task_id]) for task_id in ordered_ids if task_id in tasks_by_id]


async def reconcile_orphaned_runtime_tasks(*, exclude_task_ids: set[str] | None = None) -> int:
    """Mark non-resumable persisted running tasks as failed after a worker restart.

    Some task types have restart pumps and must remain active until those pumps
    resume them. Plain in-process tasks have no such recovery path, so surfacing
    them as failed is more honest than leaving them alive forever.
    """
    excluded = {
        runtime_task_id
        for task_id in (exclude_task_ids or set())
        if (runtime_task_id := _coerce_task_id(task_id)) is not None
    }
    # Cross-tenant access is limited to the locator columns. Reconciliation
    # reads and mutations happen only after reopening rows under tenant RLS.
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="startup orphaned runtime-task reconcile"),
    ):
        try:
            stmt = select(RuntimeTask.id, RuntimeTask.tenant_id).where(
                RuntimeTask.status == "running",
                or_(
                    RuntimeTask.task_type.is_(None),
                    RuntimeTask.task_type.notin_(_RESTART_RESUMABLE_TASK_TYPES),
                ),
            )
            result = await db.execute(stmt)
            locator_rows = result.all()
        except Exception:
            await db.rollback()
            raise

    _, ids_by_tenant = _group_runtime_task_locators(
        locator_rows,
        operation="startup orphan reconciliation",
    )
    now = datetime.now(timezone.utc)
    updated = 0
    for tenant_id, located_task_ids in ids_by_tenant.items():
        task_ids = [task_id for task_id in located_task_ids if task_id not in excluded]
        if not task_ids:
            continue
        async with tenant_scoped_session(
            tenant_id,
            session_factory=async_session,
            require_tenant=True,
            source="startup_orphan_runtime_task_reconciliation",
        ) as db:
            result = await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.id.in_(task_ids),
                    RuntimeTask.status == "running",
                    or_(
                        RuntimeTask.task_type.is_(None),
                        RuntimeTask.task_type.notin_(_RESTART_RESUMABLE_TASK_TYPES),
                    ),
                )
            )
            for task in result.scalars().all():
                if getattr(task, "id", None) in excluded:
                    continue
                task_type = str(getattr(task, "task_type", None) or "runtime_task")
                metadata = dict(getattr(task, "metadata_json", None) or {})
                if _is_restart_resumable_runtime_task(task):
                    if task_type in _RESTART_RESUMABLE_TASK_TYPES:
                        continue
                    metadata.setdefault("restart_resume_blocker", "restart_resume_not_confirmed")
                if task_type in {"delegation", "subagent", "trigger", "heartbeat"}:
                    task.status = "needs_reconciliation"
                    blocker = str(metadata.get("restart_resume_blocker") or "non_idempotent_restart_orphan")
                    if not task.result_summary:
                        task.result_summary = (
                            "Task was interrupted by a worker restart and may have performed external side effects; "
                            "it requires reconciliation before retrying or marking complete."
                        )
                    task.metadata_json = build_restart_reconciliation_metadata(
                        metadata,
                        task_type=task_type,
                        task_id=getattr(task, "id", None).hex
                        if isinstance(getattr(task, "id", None), uuid.UUID)
                        else str(getattr(task, "id", "")),
                        blocker=blocker,
                        summary=task.result_summary,
                        trace_id=getattr(task, "trace_id", None),
                        session_id=getattr(task, "child_session_id", None) or getattr(task, "parent_session_id", None),
                    )
                    task.completed_at = now
                    updated += 1
                    continue
                task.status = "failed"
                task.completed_at = now
                if not task.result_summary:
                    task.result_summary = "Task failed because the worker process restarted before completion."
                metadata["orphaned_by_restart"] = True
                task.metadata_json = metadata
                updated += 1
    return updated
