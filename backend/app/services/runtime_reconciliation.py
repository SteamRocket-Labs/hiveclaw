"""Admin-facing RuntimeTask reconciliation helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from app.services.session_tool_runtime import (
    acknowledge_unresolved_tool_effects,
    list_unresolved_tool_effects,
    tool_effect_reconciliation_summary,
    unresolved_tool_effect_predicates,
)

RECONCILIATION_STATUS = "needs_reconciliation"
RESOLVED_RUNTIME_TASK_STATUS = "completed"
AMBIGUOUS_PROVIDER_SEND_REASON = "ambiguous_provider_send"
AMBIGUOUS_PROVIDER_SEND_PROJECTION_REPAIR_SOURCE = "runtime_reconciliation.ambiguous_provider_send_projection_repair"
_ACTIONS = {"mark_resolved", "archive", "retry", "acknowledge_tool_effect"}
_TERMINAL_ACTIONS = ("mark_resolved", "archive")


class RuntimeReconciliationNotFound(LookupError):
    pass


class RuntimeReconciliationConflict(RuntimeError):
    pass


def _coerce_uuid(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _dt(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _metadata(task: RuntimeTask) -> dict[str, Any]:
    return dict(getattr(task, "metadata_json", None) or {})


def runtime_reconciliation_view(
    task: RuntimeTask,
    *,
    tool_effects: list[SessionToolInvocation] | tuple[SessionToolInvocation, ...] = (),
) -> dict[str, Any]:
    metadata = _metadata(task)
    tool_effect = tool_effect_reconciliation_summary(tool_effects)
    tool_effect_required = tool_effect is not None
    retry_allowed = False if tool_effect_required else bool(metadata.get("reconciliation_retry_allowed"))
    if tool_effect_required:
        supported_actions = ["acknowledge_tool_effect"]
    elif task.status == RECONCILIATION_STATUS:
        supported_actions = ["mark_resolved", "archive"]
        if retry_allowed:
            supported_actions.append("retry")
    else:
        supported_actions = []
    return {
        "task_id": str(task.id),
        "tenant_id": str(task.tenant_id) if task.tenant_id else None,
        "task_type": task.task_type,
        "status": task.status,
        "parent_agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else None,
        "child_agent_name": task.child_agent_name,
        "trace_id": task.trace_id,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "reason": (
            metadata.get("reconciliation_reason")
            or metadata.get("restart_resume_blocker")
            or ("tool_effect_outcome_unknown" if tool_effect_required else None)
        ),
        "side_effect_risk": metadata.get("side_effect_risk")
        or ("effect_outcome_unknown" if tool_effect_required else None),
        "retry_allowed": retry_allowed,
        "tool_effect_reconciliation_required": tool_effect_required,
        "unsettled_tool_effect_count": int((tool_effect or {}).get("unsettled_count") or 0),
        "supported_actions": supported_actions,
        "result_summary": task.result_summary,
        "metadata": metadata,
        "created_at": _dt(task.created_at),
        "started_at": _dt(task.started_at),
        "completed_at": _dt(task.completed_at),
    }


async def list_runtime_reconciliation_tasks(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str = RECONCILIATION_STATUS,
    limit: int = 50,
    agent_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    unresolved_effect = exists(
        select(SessionToolInvocation.id).where(
            *unresolved_tool_effect_predicates(
                tenant_id=tenant_id,
                run_id=RuntimeTask.id,
            )
        )
    )
    status_filter = (
        or_(
            RuntimeTask.status == RECONCILIATION_STATUS,
            (RuntimeTask.status == "failed") & unresolved_effect,
        )
        if status == RECONCILIATION_STATUS
        else RuntimeTask.status == status
    )
    stmt = (
        select(RuntimeTask)
        .where(RuntimeTask.tenant_id == tenant_id, status_filter)
        # Unknown effects are the only queue rows that block all fresh work in
        # their Session. Keep them ahead of ordinary reconciliation rows so a
        # busy tenant cannot starve an old fail-closed hold behind ``limit``.
        .order_by(unresolved_effect.desc(), RuntimeTask.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
    )
    if agent_id is not None:
        stmt = stmt.where(RuntimeTask.parent_agent_id == agent_id)
    result = await db.execute(stmt)
    tasks = list(result.scalars().all())
    effects = await list_unresolved_tool_effects(
        db,
        tenant_id=tenant_id,
        run_ids=[task.id for task in tasks],
        terminal_tasks_only=True,
        limit=None,
    )
    effects_by_run: dict[uuid.UUID, list[SessionToolInvocation]] = {}
    for effect in effects:
        effects_by_run.setdefault(effect.run_id, []).append(effect)
    return [runtime_reconciliation_view(task, tool_effects=effects_by_run.get(task.id, [])) for task in tasks]


async def get_runtime_reconciliation_task(
    db: AsyncSession,
    *,
    task_id: str | uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict[str, Any] | None:
    try:
        runtime_task_id = _coerce_uuid(task_id)
    except ValueError:
        return None
    result = await db.execute(
        select(RuntimeTask).where(RuntimeTask.id == runtime_task_id, RuntimeTask.tenant_id == tenant_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None
    effects = await list_unresolved_tool_effects(
        db,
        tenant_id=tenant_id,
        run_ids=(task.id,),
        terminal_tasks_only=True,
        limit=None,
    )
    return runtime_reconciliation_view(task, tool_effects=effects)


def _append_history(
    metadata: dict[str, Any],
    *,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    updated = dict(metadata)
    history = [item for item in updated.get("reconciliation_history", []) if isinstance(item, dict)]
    history.append(
        {
            "schema": "runtime_reconciliation_action.v1",
            "action": action,
            "reason": reason,
            "actor_user_id": str(actor_user_id),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    updated["reconciliation_history"] = history[-50:]
    return updated


def _stage_reconciliation_audit(
    db: AsyncSession,
    task: RuntimeTask,
    *,
    tenant_id: uuid.UUID,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
    previous_status: str,
) -> None:
    db.add(
        AuditLog(
            action=f"runtime_reconciliation.{action}",
            details={
                "runtime_task_id": str(task.id),
                "task_type": task.task_type,
                "previous_status": previous_status,
                "resulting_status": task.status,
                "reconciliation_status": _metadata(task).get("reconciliation_status"),
                "reason": reason,
            },
            agent_id=task.parent_agent_id,
            user_id=actor_user_id,
            tenant_id=tenant_id,
        )
    )


async def apply_runtime_reconciliation_action(
    db: AsyncSession,
    *,
    task_id: str | uuid.UUID,
    tenant_id: uuid.UUID,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    normalized_action = str(action or "").strip()
    if normalized_action not in _ACTIONS:
        raise ValueError(f"Unsupported reconciliation action: {action!r}")
    requested_reason = str(reason or "").strip()
    if normalized_action == "acknowledge_tool_effect" and not requested_reason:
        raise ValueError("tool effect acknowledgement reason is required")
    runtime_task_id = _coerce_uuid(task_id)
    result = await db.execute(
        select(RuntimeTask)
        .where(RuntimeTask.id == runtime_task_id, RuntimeTask.tenant_id == tenant_id)
        .with_for_update()
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise RuntimeReconciliationNotFound("Runtime reconciliation task not found")
    unresolved_effects = await list_unresolved_tool_effects(
        db,
        tenant_id=tenant_id,
        run_ids=(task.id,),
        terminal_tasks_only=True,
        for_update=True,
        limit=None,
    )
    if normalized_action == "acknowledge_tool_effect":
        if not unresolved_effects:
            raise RuntimeReconciliationConflict("RuntimeTask has no unresolved tool effect to acknowledge")
        if task.status not in {RECONCILIATION_STATUS, "failed"}:
            raise RuntimeReconciliationConflict(
                f"RuntimeTask tool effect hold is not actionable from status={task.status!r}"
            )
    elif unresolved_effects:
        raise RuntimeReconciliationConflict(
            "Unknown tool effect must be acknowledged before task resolution; automatic replay is forbidden"
        )
    elif task.status != RECONCILIATION_STATUS:
        # Established admin contract: once the task leaves needs_reconciliation
        # every further action conflicts; there is no replay-success path.
        raise RuntimeReconciliationConflict(
            f"RuntimeTask is no longer awaiting reconciliation (status={task.status!r})"
        )

    metadata = _metadata(task)
    normalized_reason = requested_reason or normalized_action
    previous_status = task.status

    now = datetime.now(timezone.utc)
    terminal_action = normalized_action in _TERMINAL_ACTIONS
    if normalized_action == "acknowledge_tool_effect":
        session_ids = {row.session_id for row in unresolved_effects}
        if len(session_ids) != 1 or task.parent_agent_id is None:
            raise RuntimeReconciliationConflict("RuntimeTask tool effect authority is incomplete")
        await acknowledge_unresolved_tool_effects(
            db,
            tenant_id=tenant_id,
            agent_id=task.parent_agent_id,
            session_id=next(iter(session_ids)),
            run_id=task.id,
            actor_user_id=actor_user_id,
            reason=normalized_reason,
        )
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = "tool_effect_acknowledged"
        metadata["reconciliation_retry_allowed"] = False
        metadata["tool_effect_acknowledged_at"] = now.isoformat()
        metadata["tool_effect_acknowledged_by"] = str(actor_user_id)
        if task.status == RECONCILIATION_STATUS:
            # The old provider round cannot be resumed without inventing a
            # tool result. Stop it explicitly after the operator has inspected
            # the effect; a later user turn is a new run, never a replay.
            task.status = "killed"
            task.completed_at = task.completed_at or now
            task.result_summary = f"Stopped after tool-effect reconciliation: {normalized_reason}"
            terminal_action = True
    elif normalized_action == "retry":
        if not bool(metadata.get("reconciliation_retry_allowed")):
            raise RuntimeReconciliationConflict("RuntimeTask is not marked retryable after reconciliation")
        task.status = "pending"
        task.started_at = None
        task.completed_at = None
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = "retry_requested"
        metadata["reconciliation_retry_requested_at"] = now.isoformat()
    elif normalized_action == "archive":
        task.status = "killed"
        task.completed_at = task.completed_at or now
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = "archived"
        metadata["reconciliation_archived_at"] = now.isoformat()
        task.result_summary = task.result_summary or f"Archived after reconciliation: {normalized_reason}"
    else:
        task.status = RESOLVED_RUNTIME_TASK_STATUS
        task.completed_at = task.completed_at or now
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = "resolved"
        metadata["reconciliation_resolved_at"] = now.isoformat()
        task.result_summary = f"Reconciliation resolved: {normalized_reason}"

    metadata = _append_history(
        metadata,
        action=normalized_action,
        reason=normalized_reason,
        actor_user_id=actor_user_id,
    )
    task.metadata_json = metadata
    _stage_reconciliation_audit(
        db,
        task,
        tenant_id=tenant_id,
        action=normalized_action,
        reason=normalized_reason,
        actor_user_id=actor_user_id,
        previous_status=previous_status,
    )
    if terminal_action:
        # Operator terminal actions share the one mechanical settlement
        # boundary: terminal fence, root item transition, and pending control
        # settlement commit with the operator's semantic decision.
        from app.services.runtime_terminal_settlement import settle_runtime_task_terminal

        await settle_runtime_task_terminal(
            db,
            task,
            terminal_source=f"runtime_reconciliation.{normalized_action}",
            root_reason_code=f"runtime_reconciliation_terminal:{normalized_action}",
        )
    await db.flush()
    remaining_effects = await list_unresolved_tool_effects(
        db,
        tenant_id=tenant_id,
        run_ids=(task.id,),
        terminal_tasks_only=True,
        limit=None,
    )
    return runtime_reconciliation_view(task, tool_effects=remaining_effects)


async def repair_ambiguous_provider_send_terminal_projections(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    limit: int = 100,
) -> dict[str, Any]:
    """RC-10A recovery lane B: repair only the missing mechanical projection.

    Candidates are exact-code rows whose projection is actually incomplete:
    status=needs_reconciliation AND
    metadata.session_v2_reconciliation.reason == ambiguous_provider_send AND
    (terminal fence missing OR committed status not exactly needs_reconciliation
    OR commit source missing OR root item not yet settled). A complete
    projection requires every one of those mechanical terminal facts;
    incompleteness is filtered in SQL before the limit so a backlog of
    already-complete rows can never starve a later drift, and ``examined``
    counts the claimed candidates. The repair NEVER changes RuntimeTask
    status: the semantic resolve/archive decision stays with the operator.
    """

    from sqlalchemy import exists, or_

    from app.models.runtime_root_item import RuntimeRootItem
    from app.services.runtime_root_ledger import RUNTIME_ROOT_TERMINAL_STATES
    from app.services.runtime_terminal_settlement import settle_runtime_task_terminal

    reason_filter = RuntimeTask.metadata_json["session_v2_reconciliation"]["reason"].as_string()
    settled_root_states = {"needs_reconciliation"} | RUNTIME_ROOT_TERMINAL_STATES
    fence_missing = func.coalesce(RuntimeTask.metadata_json["terminal_execution_fence_ref"].as_string(), "") == ""
    committed_status_incomplete = (
        func.coalesce(RuntimeTask.metadata_json["terminal_committed_status"].as_string(), "") != RECONCILIATION_STATUS
    )
    commit_source_missing = func.coalesce(RuntimeTask.metadata_json["terminal_commit_source"].as_string(), "") == ""
    root_unsettled = exists(
        select(RuntimeRootItem.id).where(
            RuntimeRootItem.runtime_task_id == RuntimeTask.id,
            RuntimeRootItem.state.not_in(settled_root_states),
        )
    )
    rows = list(
        (
            await db.execute(
                select(RuntimeTask)
                .where(
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.status == RECONCILIATION_STATUS,
                    reason_filter == AMBIGUOUS_PROVIDER_SEND_REASON,
                    or_(fence_missing, committed_status_incomplete, commit_source_missing, root_unsettled),
                )
                .order_by(RuntimeTask.created_at, RuntimeTask.id)
                .limit(max(1, min(int(limit), 500)))
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )

    repaired: list[str] = []
    examined = 0
    for task in rows:
        examined += 1
        metadata = _metadata(task)
        fence_present = bool(str(metadata.get("terminal_execution_fence_ref") or ""))
        committed_status_complete = str(metadata.get("terminal_committed_status") or "") == RECONCILIATION_STATUS
        commit_source_present = bool(str(metadata.get("terminal_commit_source") or ""))
        root_state = await db.scalar(select(RuntimeRootItem.state).where(RuntimeRootItem.runtime_task_id == task.id))
        root_settled = (
            root_state is None or root_state == "needs_reconciliation" or (root_state in RUNTIME_ROOT_TERMINAL_STATES)
        )
        if fence_present and committed_status_complete and commit_source_present and root_settled:
            # Completed by a concurrent repair between claim and check.
            continue
        await settle_runtime_task_terminal(
            db,
            task,
            terminal_source=AMBIGUOUS_PROVIDER_SEND_PROJECTION_REPAIR_SOURCE,
            root_reason_code="ambiguous_provider_send_projection_repair",
        )
        repaired_metadata = _metadata(task)
        repaired_metadata["ambiguous_provider_send_projection_repaired_at"] = datetime.now(timezone.utc).isoformat()
        task.metadata_json = repaired_metadata
        db.add(
            AuditLog(
                action="runtime_reconciliation.projection_repair",
                details={
                    "runtime_task_id": str(task.id),
                    "task_type": task.task_type,
                    "previous_status": task.status,
                    "resulting_status": task.status,
                    "reconciliation_status": metadata.get("reconciliation_status"),
                    "reason": AMBIGUOUS_PROVIDER_SEND_REASON,
                },
                agent_id=task.parent_agent_id,
                user_id=actor_user_id,
                tenant_id=tenant_id,
            )
        )
        repaired.append(str(task.id))
    await db.flush()
    return {"examined": examined, "repaired_task_ids": repaired}
