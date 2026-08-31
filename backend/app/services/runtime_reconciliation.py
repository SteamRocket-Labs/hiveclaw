"""Admin-facing RuntimeTask reconciliation helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.runtime_task import TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES, RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.models.session_v2 import SessionToolInvocation
from app.services.session_tool_runtime import (
    acknowledge_unresolved_tool_effects,
    list_unresolved_tool_effects,
    tool_effect_reconciliation_summary,
    unresolved_tool_effect_predicates,
)
from app.services.runtime_terminal_boundary_outbox import (
    TerminalBoundaryBindingError,
    terminal_boundary_binding_sha256,
)

RECONCILIATION_STATUS = "needs_reconciliation"
RESOLVED_RUNTIME_TASK_STATUS = "completed"
AMBIGUOUS_PROVIDER_SEND_REASON = "ambiguous_provider_send"
AMBIGUOUS_PROVIDER_SEND_PROJECTION_REPAIR_SOURCE = "runtime_reconciliation.ambiguous_provider_send_projection_repair"
_ACTIONS = {"mark_resolved", "archive", "retry", "acknowledge_tool_effect"}
_TERMINAL_ACTIONS = ("mark_resolved", "archive")
_TRIGGER_DISPOSITIONS = ("confirmed_success", "confirmed_failure", "release")


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


def _uuid_text(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _canonical_trigger_settlement_blocker(task: RuntimeTask) -> str | None:
    settlement = _metadata(task).get("trigger_settlement")
    if not isinstance(settlement, dict):
        return "canonical_trigger_settlement_missing"
    if (
        settlement.get("schema") != "trigger_runtime_settlement.v1"
        or str(settlement.get("runtime_task_id") or "") != str(task.id)
        or settlement.get("status") != RECONCILIATION_STATUS
        or settlement.get("outcome") != "hold"
    ):
        return "canonical_trigger_settlement_mismatch"
    trigger_ids = settlement.get("trigger_ids")
    trigger_outcomes = settlement.get("trigger_outcomes")
    if not isinstance(trigger_ids, list) or not isinstance(trigger_outcomes, dict):
        return "canonical_trigger_settlement_mismatch"
    normalized_ids = [str(value) for value in trigger_ids]
    normalized_outcomes = {str(key): str(value) for key, value in trigger_outcomes.items()}
    if (
        not normalized_ids
        or len(normalized_ids) != len(set(normalized_ids))
        or set(normalized_ids) != set(normalized_outcomes)
        or any(value not in {"success", "failure", "hold", "release"} for value in normalized_outcomes.values())
    ):
        return "canonical_trigger_settlement_mismatch"
    if "hold" not in normalized_outcomes.values():
        return "canonical_trigger_hold_missing"
    return None


def _trigger_disposition_readiness(
    task: RuntimeTask,
    terminal_projections: list[RuntimeTerminalBoundaryOutbox] | tuple[RuntimeTerminalBoundaryOutbox, ...],
) -> dict[str, Any] | None:
    if task.task_type != "trigger" or task.status != RECONCILIATION_STATUS:
        return None
    blocker = _canonical_trigger_settlement_blocker(task)
    if blocker is not None:
        return {
            "schema": "runtime_trigger_disposition_readiness.v1",
            "ready": False,
            "blocker": blocker,
            "terminal_projection_id": None,
        }
    projections = list(terminal_projections)
    projection_id = str(projections[0].id) if len(projections) == 1 else None
    if not projections:
        blocker = "terminal_projection_missing"
    elif len(projections) != 1:
        blocker = "terminal_projection_mismatch"
    elif projections[0].status != "delivered":
        blocker = "terminal_projection_pending"
    else:
        row = projections[0]
        binding = dict(row.binding_json or {})
        receipt = dict(row.delivery_receipt_json or {})
        from app.services.direct_invocation_terminal_boundary_processor import (
            direct_terminal_projection_payload,
        )
        from app.services.web_terminal_boundary_processor import _sha256

        try:
            stored_binding_matches = row.binding_sha256 == terminal_boundary_binding_sha256(binding)
        except TerminalBoundaryBindingError:
            stored_binding_matches = False
        expected_session_id = str(task.child_session_id or task.id).strip()
        blocker = (
            None
            if row.tenant_id == task.tenant_id
            and row.runtime_task_id == task.id
            and row.agent_id == task.parent_agent_id
            and row.session_id == expected_session_id
            and row.event_kind in {"runtime_terminal", "turn_abort"}
            and row.terminal_status == RECONCILIATION_STATUS
            and row.authority_ref == "runtime_task"
            and row.authority_id == str(task.id)
            and row.delivered_at is not None
            and stored_binding_matches
            and binding.get("tenant_id") == str(task.tenant_id)
            and binding.get("runtime_task_id") == str(task.id)
            and binding.get("agent_id") == str(task.parent_agent_id)
            and binding.get("session_id") == expected_session_id
            and binding.get("authority_ref") == "runtime_task"
            and binding.get("authority_id") == str(task.id)
            and binding.get("direct_projection_sha256") == _sha256(direct_terminal_projection_payload(task))
            and receipt.get("boundary_id") == str(row.id)
            else "terminal_projection_mismatch"
        )
    return {
        "schema": "runtime_trigger_disposition_readiness.v1",
        "ready": blocker is None,
        "blocker": blocker,
        "terminal_projection_id": projection_id,
    }


async def _load_trigger_terminal_projections(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tasks: list[RuntimeTask] | tuple[RuntimeTask, ...],
) -> dict[uuid.UUID, list[RuntimeTerminalBoundaryOutbox]]:
    task_ids = [task.id for task in tasks if task.task_type == "trigger" and task.status == RECONCILIATION_STATUS]
    if not task_ids:
        return {}
    rows = list(
        (
            await db.execute(
                select(RuntimeTerminalBoundaryOutbox).where(
                    RuntimeTerminalBoundaryOutbox.tenant_id == tenant_id,
                    RuntimeTerminalBoundaryOutbox.runtime_task_id.in_(task_ids),
                    RuntimeTerminalBoundaryOutbox.terminal_status == RECONCILIATION_STATUS,
                    RuntimeTerminalBoundaryOutbox.authority_ref == "runtime_task",
                )
            )
        )
        .scalars()
        .all()
    )
    by_task: dict[uuid.UUID, list[RuntimeTerminalBoundaryOutbox]] = {}
    for row in rows:
        by_task.setdefault(row.runtime_task_id, []).append(row)
    return by_task


def runtime_reconciliation_view(
    task: RuntimeTask,
    *,
    tool_effects: list[SessionToolInvocation] | tuple[SessionToolInvocation, ...] = (),
    terminal_projections: list[RuntimeTerminalBoundaryOutbox] | tuple[RuntimeTerminalBoundaryOutbox, ...] = (),
) -> dict[str, Any]:
    metadata = _metadata(task)
    tool_effect = tool_effect_reconciliation_summary(tool_effects)
    tool_effect_required = tool_effect is not None
    trigger_reconciliation = task.task_type == "trigger" and task.status == RECONCILIATION_STATUS
    retry_allowed = (
        False if tool_effect_required or trigger_reconciliation else bool(metadata.get("reconciliation_retry_allowed"))
    )
    if tool_effect_required:
        supported_actions = ["acknowledge_tool_effect"]
    elif task.status == RECONCILIATION_STATUS:
        supported_actions = ["mark_resolved", "archive"]
        if retry_allowed:
            supported_actions.append("retry")
    else:
        supported_actions = []
    settlement = metadata.get("trigger_settlement")
    settlement_audit_id = (
        _uuid_text(settlement.get("audit_log_id"))
        if isinstance(settlement, dict)
        and settlement.get("schema") == "trigger_runtime_settlement.v1"
        and str(settlement.get("runtime_task_id") or "") == str(task.id)
        else None
    )
    completion_outbox_id = _uuid_text(metadata.get("completion_outbox_id"))
    from app.services.trigger_artifacts import trigger_output_artifact_ref

    expected_artifact = trigger_output_artifact_ref(str(task.id)) if task.task_type == "trigger" else None
    output_artifact = metadata.get("output_artifact")
    if output_artifact != expected_artifact:
        output_artifact = None
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
        "supported_trigger_dispositions": list(_TRIGGER_DISPOSITIONS) if trigger_reconciliation else [],
        "trigger_disposition_readiness": _trigger_disposition_readiness(task, terminal_projections),
        "output_artifact": output_artifact,
        "completion_outbox_id": completion_outbox_id,
        "settlement_audit_ref": ({"kind": "audit_log", "id": settlement_audit_id} if settlement_audit_id else None),
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
    projections_by_task = await _load_trigger_terminal_projections(db, tenant_id=tenant_id, tasks=tasks)
    return [
        runtime_reconciliation_view(
            task,
            tool_effects=effects_by_run.get(task.id, []),
            terminal_projections=projections_by_task.get(task.id, []),
        )
        for task in tasks
    ]


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
    projections_by_task = await _load_trigger_terminal_projections(db, tenant_id=tenant_id, tasks=(task,))
    return runtime_reconciliation_view(
        task,
        tool_effects=effects,
        terminal_projections=projections_by_task.get(task.id, []),
    )


def _append_history(
    metadata: dict[str, Any],
    *,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
    trigger_disposition: str | None = None,
) -> dict[str, Any]:
    updated = dict(metadata)
    history = [item for item in updated.get("reconciliation_history", []) if isinstance(item, dict)]
    entry = {
        "schema": "runtime_reconciliation_action.v1",
        "action": action,
        "reason": reason,
        "actor_user_id": str(actor_user_id),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if trigger_disposition is not None:
        entry["trigger_disposition"] = trigger_disposition
    history.append(entry)
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
    trigger_disposition: str | None = None,
) -> None:
    details = {
        "runtime_task_id": str(task.id),
        "task_type": task.task_type,
        "previous_status": previous_status,
        "resulting_status": task.status,
        "reconciliation_status": _metadata(task).get("reconciliation_status"),
        "reason": reason,
    }
    if trigger_disposition is not None:
        details["trigger_disposition"] = trigger_disposition
        settlement = _metadata(task).get("trigger_settlement")
        if isinstance(settlement, dict):
            details["trigger_outcomes"] = dict(settlement.get("trigger_outcomes") or {})
    audit_kwargs: dict[str, Any] = {}
    if trigger_disposition is not None:
        audit_kwargs["id"] = uuid.uuid5(
            task.id,
            f"runtime-reconciliation:{action}:{trigger_disposition}",
        )
    db.add(
        AuditLog(
            **audit_kwargs,
            action=f"runtime_reconciliation.{action}",
            details=details,
            agent_id=task.parent_agent_id,
            user_id=actor_user_id,
            tenant_id=tenant_id,
        )
    )


async def _require_delivered_trigger_terminal_projection(
    db: AsyncSession,
    task: RuntimeTask,
) -> None:
    projections_by_task = await _load_trigger_terminal_projections(
        db,
        tenant_id=task.tenant_id,
        tasks=(task,),
    )
    readiness = _trigger_disposition_readiness(task, projections_by_task.get(task.id, []))
    if readiness is None or not readiness["ready"]:
        raise RuntimeReconciliationConflict(str((readiness or {}).get("blocker") or "terminal_projection_pending"))


async def _require_delivered_terminal_projection(db: AsyncSession, task: RuntimeTask) -> None:
    if task.terminal_boundary_generation is None or task.task_type not in TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES:
        return
    rows = list(
        (
            await db.execute(
                select(RuntimeTerminalBoundaryOutbox).where(
                    RuntimeTerminalBoundaryOutbox.tenant_id == task.tenant_id,
                    RuntimeTerminalBoundaryOutbox.runtime_task_id == task.id,
                    RuntimeTerminalBoundaryOutbox.terminal_status == task.status,
                )
            )
        ).scalars()
    )
    if len(rows) != 1:
        raise RuntimeReconciliationConflict("terminal_projection_missing")
    row = rows[0]
    binding = dict(row.binding_json or {})
    receipt = dict(row.delivery_receipt_json or {})
    try:
        binding_matches = row.binding_sha256 == terminal_boundary_binding_sha256(binding)
    except TerminalBoundaryBindingError:
        binding_matches = False
    if (
        row.status != "delivered"
        or row.delivered_at is None
        or not binding_matches
        or binding.get("tenant_id") != str(task.tenant_id)
        or binding.get("runtime_task_id") != str(task.id)
        or binding.get("authority_id") != str(row.authority_id)
        or receipt.get("boundary_id") != str(row.id)
    ):
        raise RuntimeReconciliationConflict("terminal_projection_pending")


async def apply_runtime_reconciliation_action(
    db: AsyncSession,
    *,
    task_id: str | uuid.UUID,
    tenant_id: uuid.UUID,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
    trigger_disposition: str | None = None,
) -> dict[str, Any]:
    normalized_action = str(action or "").strip()
    if normalized_action not in _ACTIONS:
        raise ValueError(f"Unsupported reconciliation action: {action!r}")
    requested_reason = str(reason or "").strip()
    if not requested_reason:
        raise ValueError("reconciliation evidence reason is required")
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

    normalized_trigger_disposition = str(trigger_disposition or "").strip() or None
    trigger_reconciliation = task.task_type == "trigger" and task.status == RECONCILIATION_STATUS
    if trigger_reconciliation:
        if normalized_action == "retry":
            raise RuntimeReconciliationConflict(
                "Trigger reconciliation retry requires a new RuntimeTask; replaying the ambiguous task is forbidden"
            )
        if normalized_trigger_disposition not in _TRIGGER_DISPOSITIONS:
            raise RuntimeReconciliationConflict(
                "trigger_disposition must be confirmed_success, confirmed_failure, or release"
            )
        if normalized_action == "acknowledge_tool_effect":
            # This is the one explicit atomic combination: acknowledge the
            # unresolved no-replay tool-effect hold and settle the trigger's
            # held intent under the same operator evidence. The unresolved
            # effect check above proves this action is not a semantic alias.
            pass
        elif normalized_trigger_disposition == "release":
            if normalized_action != "archive":
                raise RuntimeReconciliationConflict("trigger_disposition=release requires archive")
        elif normalized_action != "mark_resolved":
            raise RuntimeReconciliationConflict("confirmed trigger outcomes require action=mark_resolved")
        await _require_delivered_trigger_terminal_projection(db, task)
    elif normalized_trigger_disposition is not None:
        raise ValueError("trigger_disposition is only valid for a trigger awaiting reconciliation")
    elif task.status == RECONCILIATION_STATUS:
        await _require_delivered_terminal_projection(db, task)

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
        if task.status == RECONCILIATION_STATUS and not trigger_reconciliation:
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
    elif normalized_action == "archive" and not trigger_reconciliation:
        task.status = "killed"
        task.completed_at = task.completed_at or now
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = "archived"
        metadata["reconciliation_archived_at"] = now.isoformat()
        task.result_summary = task.result_summary or f"Archived after reconciliation: {normalized_reason}"
    elif not trigger_reconciliation:
        task.status = RESOLVED_RUNTIME_TASK_STATUS
        task.completed_at = task.completed_at or now
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = "resolved"
        metadata["reconciliation_resolved_at"] = now.isoformat()
        task.result_summary = f"Reconciliation resolved: {normalized_reason}"

    if trigger_reconciliation:
        task.metadata_json = metadata
        from app.services.runtime_task_service import settle_trigger_runtime_reconciliation

        try:
            trigger_status, settlement = await settle_trigger_runtime_reconciliation(
                db,
                task,
                disposition=str(normalized_trigger_disposition),
            )
        except ValueError as exc:
            raise RuntimeReconciliationConflict(str(exc)) from exc
        task.status = trigger_status
        task.completed_at = task.completed_at or now
        metadata = _metadata(task)
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = f"trigger_{normalized_trigger_disposition}"
        metadata["reconciliation_retry_allowed"] = False
        metadata["trigger_reconciliation_disposition"] = normalized_trigger_disposition
        metadata["trigger_reconciliation_resolved_at"] = now.isoformat()
        metadata["trigger_reconciliation_resolved_by"] = str(actor_user_id)
        metadata["trigger_settlement"] = settlement
        if settlement.get("failure_backoff"):
            metadata["failure_backoff"] = list(settlement["failure_backoff"])
        terminal_action = True

    metadata = _append_history(
        metadata,
        action=normalized_action,
        reason=normalized_reason,
        actor_user_id=actor_user_id,
        trigger_disposition=normalized_trigger_disposition,
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
        trigger_disposition=normalized_trigger_disposition,
    )
    if terminal_action:
        # Operator terminal actions share the one mechanical settlement
        # boundary: terminal fence, root item transition, and pending control
        # settlement commit with the operator's semantic decision.
        from app.services.runtime_terminal_settlement import settle_runtime_task_terminal

        terminal_source = (
            f"runtime_reconciliation.trigger.{normalized_trigger_disposition}"
            if trigger_reconciliation
            else f"runtime_reconciliation.{normalized_action}"
        )
        await settle_runtime_task_terminal(
            db,
            task,
            terminal_source=terminal_source,
            root_reason_code=(
                f"runtime_reconciliation_trigger:{normalized_trigger_disposition}"
                if trigger_reconciliation
                else f"runtime_reconciliation_terminal:{normalized_action}"
            ),
        )
        if (
            trigger_reconciliation
            and normalized_trigger_disposition == "confirmed_success"
            and isinstance(_metadata(task).get("response_complete_payload"), dict)
        ):
            # The delivered needs_reconciliation boundary sealed this turn as
            # an abort. A confirmed successful mixed batch with a canonical
            # response needs its own status-matching turn_stop projection.
            from app.services.direct_invocation_terminal_boundary_processor import (
                enqueue_direct_terminal_boundary_for_task,
            )

            task.terminal_boundary_enqueued_at = None
            await enqueue_direct_terminal_boundary_for_task(db, task)
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
    OR commit source missing OR root item not yet settled OR required terminal
    boundary not yet enqueued). A complete projection requires every one of
    those mechanical terminal facts;
    incompleteness is filtered in SQL before the limit so a backlog of
    already-complete rows can never starve a later drift, and ``examined``
    counts the claimed candidates. The repair NEVER changes RuntimeTask
    status: the semantic resolve/archive decision stays with the operator.
    """

    from sqlalchemy import exists, or_

    from app.models.runtime_root_item import RuntimeRootItem
    from app.services.runtime_root_ledger import RUNTIME_ROOT_TERMINAL_STATES
    from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal

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
    terminal_boundary_missing = RuntimeTask.terminal_boundary_generation.is_not(
        None
    ) & RuntimeTask.terminal_boundary_enqueued_at.is_(None)
    rows = list(
        (
            await db.execute(
                select(RuntimeTask)
                .where(
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.status == RECONCILIATION_STATUS,
                    reason_filter == AMBIGUOUS_PROVIDER_SEND_REASON,
                    or_(
                        fence_missing,
                        committed_status_incomplete,
                        commit_source_missing,
                        root_unsettled,
                        terminal_boundary_missing,
                    ),
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
        boundary_enqueued = task.terminal_boundary_generation is None or task.terminal_boundary_enqueued_at is not None
        if fence_present and committed_status_complete and commit_source_present and root_settled and boundary_enqueued:
            # Completed by a concurrent repair between claim and check.
            continue
        await settle_and_enqueue_runtime_task_terminal(
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
