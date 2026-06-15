"""Persistence helpers for runtime delegation tasks."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.services.tenant_resolver import resolve_tenant_for_agent


_RESTART_RESUMABLE_TASK_TYPES = ("workflow", "web_chat_turn")
_TERMINAL_STATUSES = {"completed", "failed", "killed", "skipped"}


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
        "parent_agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else None,
        "child_agent_name": task.child_agent_name,
        "prompt": task.prompt,
        "result": task.result_summary,
        "trace_id": task.trace_id,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "depth": task.depth,
        "metadata": task.metadata_json or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def _is_restart_resumable_runtime_task(task: RuntimeTask) -> bool:
    task_type = getattr(task, "task_type", None)
    if task_type in _RESTART_RESUMABLE_TASK_TYPES:
        return True

    metadata = dict(getattr(task, "metadata_json", None) or {})
    return bool(
        metadata.get("resume_after_restart")
        and (metadata.get("resumable_delegation") or metadata.get("resumable_subagent"))
    )


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
) -> str:
    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        raise ValueError(f"Invalid runtime task id: {task_id!r}")

    started_at = datetime.now(timezone.utc) if status == "running" else None
    # Stage-2b: runtime_tasks now carries a tenant_id (RLS). Derive it from the
    # parent agent so the INSERT lands inside the agent's tenant — without it the
    # row is NULL and (post role-flip) globally visible. No parent_agent → None
    # (orphan, surfaced honestly the same way the backfill leaves orphans NULL).
    tenant_id = await resolve_tenant_for_agent(parent_agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        try:
            db.add(RuntimeTask(
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
            ))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return runtime_task_id.hex


async def update_runtime_task_record(task_id: str, **fields: Any) -> bool:
    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        return False

    # By-PK single-row update: scope the smallest sanctioned surface. A bare
    # session fail-closes post role-flip; an audited single-row BYPASS touches
    # exactly this runtime_task row (no cross-tenant scan).
    async with async_session() as db, enter_rls_bypass(db, reason="runtime-task status update"):
        try:
            result = await db.execute(select(RuntimeTask).where(RuntimeTask.id == runtime_task_id))
            task = result.scalar_one_or_none()
            if task is None:
                return False

            for key, value in fields.items():
                if hasattr(task, key):
                    if (
                        key == "metadata_json"
                        and isinstance(value, dict)
                        and isinstance(task.metadata_json, dict)
                    ):
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
                persisted_task_id_text = persisted_task_id.hex if isinstance(persisted_task_id, uuid.UUID) else str(task_id)
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
            if status in {"completed", "failed", "killed", "skipped"} and task.completed_at is None:
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

    # By-PK single-row read → audited single-row BYPASS (see update above).
    async with async_session() as db, enter_rls_bypass(db, reason="runtime-task status read"):
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
    limit: int = 50,
) -> list[dict[str, Any]]:
    # Restart-safe resume scan is intentionally cross-tenant (enumerate every
    # tenant's still-active tasks at startup) → audited BYPASS, not fail-closed.
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="restart-safe async-delegation resume scan"),
    ):
        try:
            stmt = (
                select(RuntimeTask)
                .where(RuntimeTask.status.in_(statuses))
                .order_by(RuntimeTask.created_at.asc())
                .limit(limit)
            )
            result = await db.execute(stmt)
            tasks = result.scalars().all()
        except Exception:
            await db.rollback()
            raise
    return [_task_to_dict(task) for task in tasks]


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
    # Startup reconcile sweeps every tenant's stuck "running" rows → audited BYPASS.
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="startup orphaned runtime-task reconcile"),
    ):
        try:
            stmt = select(RuntimeTask).where(
                RuntimeTask.status == "running",
                or_(
                    RuntimeTask.task_type.is_(None),
                    RuntimeTask.task_type.notin_(_RESTART_RESUMABLE_TASK_TYPES),
                ),
            )
            result = await db.execute(stmt)
            tasks = result.scalars().all()
            if not tasks:
                return 0

            now = datetime.now(timezone.utc)
            updated = 0
            for task in tasks:
                if getattr(task, "id", None) in excluded:
                    continue
                if _is_restart_resumable_runtime_task(task):
                    continue
                task.status = "failed"
                task.completed_at = now
                if not task.result_summary:
                    task.result_summary = "Task failed because the worker process restarted before completion."
                metadata = dict(getattr(task, "metadata_json", None) or {})
                metadata["orphaned_by_restart"] = True
                task.metadata_json = metadata
                updated += 1

            await db.commit()
            return updated
        except Exception:
            await db.rollback()
            raise
