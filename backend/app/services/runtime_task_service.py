"""Persistence helpers for runtime delegation tasks."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.runtime_task import RuntimeTask


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
    async with async_session() as db:
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

    async with async_session() as db:
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
            if status == "running" and task.started_at is None:
                task.started_at = now
            if status in {"completed", "failed", "killed"} and task.completed_at is None:
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

    async with async_session() as db:
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
    async with async_session() as db:
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
    async with async_session() as db:
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
    """Mark any persisted running tasks as failed after a worker restart.

    Async delegation currently executes in-process. After a backend restart,
    any DB records still marked as `running` can no longer make progress and
    should be surfaced honestly as failed instead of appearing alive forever.
    """
    excluded = {
        runtime_task_id
        for task_id in (exclude_task_ids or set())
        if (runtime_task_id := _coerce_task_id(task_id)) is not None
    }
    async with async_session() as db:
        try:
            result = await db.execute(select(RuntimeTask).where(RuntimeTask.status == "running"))
            tasks = result.scalars().all()
            if not tasks:
                return 0

            now = datetime.now(timezone.utc)
            updated = 0
            for task in tasks:
                if getattr(task, "id", None) in excluded:
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
