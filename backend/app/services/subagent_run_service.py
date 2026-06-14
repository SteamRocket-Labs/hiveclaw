"""Durable run records for background subagents (Step 8).

A ``run_in_background=True`` spawn schedules an in-memory ``asyncio`` task and
emits a completion Signal when it finishes (``app/agents/subagent.py``). That
Signal is PG-durable, but the *worker* is not: if the process restarts mid-run,
no Signal is ever emitted and a parent polling ``check_subagent`` would wait
forever.

This module closes that loop by recording each background run as a
``RuntimeTask(task_type="subagent")``. Because "subagent" is NOT in
``_RESTART_RESUMABLE_TASK_TYPES``, the startup ``reconcile_orphaned_runtime_tasks``
sweep marks a crashed run ``failed`` (``orphaned_by_restart``) — so
``check_subagent`` always resolves to a terminal status and the parent's open
loop closes. Honesty: this is fail-closed recovery (a crashed worker is reported
failed), not mid-run resume — a non-idempotent worker cannot be safely
auto-replayed, so we surface the failure rather than risk duplicate side effects.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.agents.subagent import SubagentResult
from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.services.runtime_task_service import (
    create_runtime_task_record,
    get_runtime_task_record,
    update_runtime_task_record,
)
from app.services.tenant_resolver import resolve_tenant_for_agent

SUBAGENT_RUN_TASK_TYPE = "subagent"


async def start_subagent_run(
    *,
    parent_agent_id: uuid.UUID,
    spec_name: str,
    spec_type: str,
    task: str,
    parent_session_id: str | None = None,
    trace_id: str | None = None,
) -> str:
    """Create the durable ``running`` record for a background subagent. Returns
    the run id the parent later passes to ``check_subagent``."""
    run_id = str(uuid.uuid4())
    return await create_runtime_task_record(
        task_id=run_id,
        task_type=SUBAGENT_RUN_TASK_TYPE,
        status="running",
        parent_agent_id=parent_agent_id,
        child_agent_name=spec_name,
        prompt=task,
        trace_id=trace_id,
        parent_session_id=parent_session_id,
        metadata_json={"subagent_type": spec_type},
    )


def make_run_completer(run_id: str):
    """Return an ``on_complete(result)`` callback that writes the terminal status."""

    async def _complete(result: SubagentResult) -> None:
        await update_runtime_task_record(
            run_id,
            status="completed" if result.ok else "failed",
            result_summary=(result.content or result.error or "")[:8000],
            token_usage={"total_tokens": result.tokens_used},
        )

    return _complete


async def get_subagent_run(run_id: str, parent_agent_id: uuid.UUID) -> dict[str, Any] | None:
    """Read one background subagent run by id (terminal status + result).

    Ownership-scoped: returns None unless the run belongs to ``parent_agent_id``
    (``get_runtime_task_record`` is an unscoped by-PK read, so the caller must not
    be able to read another agent's run by guessing its id)."""
    record = await get_runtime_task_record(run_id)
    if record is None or record.get("task_type") != SUBAGENT_RUN_TASK_TYPE:
        return None
    if record.get("parent_agent_id") != str(parent_agent_id):
        return None
    return record


async def list_subagent_runs(parent_agent_id: uuid.UUID, *, limit: int = 20) -> list[dict[str, Any]]:
    """Recent background subagent runs spawned by this agent (newest first)."""
    tenant_id = await resolve_tenant_for_agent(parent_agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        rows = (
            (
                await db.execute(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.parent_agent_id == parent_agent_id,
                        RuntimeTask.task_type == SUBAGENT_RUN_TASK_TYPE,
                    )
                    .order_by(RuntimeTask.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "run_id": str(r.id),
            "name": r.child_agent_name,
            "type": (r.metadata_json or {}).get("subagent_type"),
            "status": r.status,
            "result_summary": r.result_summary,
            "orphaned_by_restart": bool((r.metadata_json or {}).get("orphaned_by_restart")),
        }
        for r in rows
    ]
