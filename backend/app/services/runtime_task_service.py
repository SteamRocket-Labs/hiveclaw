"""Persistence helpers for runtime delegation tasks."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.audit import AuditLog
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.models.trigger import AgentTrigger
from app.runtime.tenant_admission import RuntimeTenantPreconditionError, raise_runtime_tenant_precondition
from app.services.runtime_tenant_admission import admit_agent_runtime_tenant
from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification
from app.services.runtime_budget_service import runtime_task_outer_budget_actuals
from app.services.tenant_resolver import resolve_tenant_for_agent, resolve_tenant_for_runtime_task


logger = logging.getLogger(__name__)


_RESTART_RESUMABLE_TASK_TYPES = (
    "workflow",
    "web_chat_turn",
    "team_member",
    "goal_continuation",
    "advanced_plan",
    "a2a_continuation",
    "approval_execution",
    "hr_provisioning",
    "dream",
)
_TERMINAL_STATUSES = {"completed", "failed", "killed", "skipped", "needs_reconciliation"}
_RUNTIME_BUDGET_DIMENSIONS = (
    "tokens",
    "cache_miss_tokens",
    "subagents",
    "team_sessions",
    "delegations",
    "background_tasks",
    "continuation_wakes",
    "provider_calls",
)
_RUNTIME_TASK_ROOT_STATE = {
    "pending": "queued",
    "queued": "queued",
    "running": "running",
    "resumable": "suspended",
    "suspended": "suspended",
    "waiting_approval": "waiting_approval",
    "waiting_budget_approval": "waiting_approval",
    "completed": "completed",
    "failed": "failed",
    "killed": "killed",
    "skipped": "skipped",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "needs_reconciliation": "needs_reconciliation",
}
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


def _runtime_task_root_state(status: str, *, override: str | None = None) -> str:
    if override:
        return override
    normalized = str(status or "").strip()
    root_state = _RUNTIME_TASK_ROOT_STATE.get(normalized)
    if root_state is None:
        raise ValueError(f"Unsupported RuntimeTask status for root ledger: {normalized!r}")
    return root_state


def _normalized_runtime_budget_actuals(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise TypeError("runtime_budget_actuals must be an object")
    return {dimension: max(0, int(value.get(dimension) or 0)) for dimension in _RUNTIME_BUDGET_DIMENSIONS}


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
        "root_user_id": str(getattr(task, "root_user_id", None)) if getattr(task, "root_user_id", None) else None,
        "root_session_id": getattr(task, "root_session_id", None),
        "delegation_chain": list(getattr(task, "delegation_chain_json", None) or []),
        "depth": task.depth,
        "budget_run_id": str(task.budget_run_id) if task.budget_run_id else None,
        "root_runtime_task_id": str(getattr(task, "root_runtime_task_id", None))
        if getattr(task, "root_runtime_task_id", None)
        else None,
        "budget_reservation_key": task.budget_reservation_key,
        "budget_admission_status": task.budget_admission_status,
        "budget_terminal_reason": task.budget_terminal_reason,
        "claimed_by": getattr(task, "claimed_by", None),
        "claim_expires_at": (task.claim_expires_at.isoformat() if getattr(task, "claim_expires_at", None) else None),
        "claim_version": int(getattr(task, "claim_version", 0) or 0),
        "root_idempotency_key": getattr(task, "root_idempotency_key", None),
        "config_snapshot_hash": getattr(task, "config_snapshot_hash", None),
        "policy_snapshot_hash": getattr(task, "policy_snapshot_hash", None),
        "metadata": task.metadata_json or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def _trigger_inflight_matches(config: dict[str, Any], runtime_task_id: uuid.UUID) -> bool:
    inflight = config.get("_fire_inflight")
    if not isinstance(inflight, dict):
        return False
    return _coerce_task_id(inflight.get("runtime_task_id")) == runtime_task_id


async def _settle_trigger_runtime_task(
    db: Any,
    task: RuntimeTask,
    *,
    status: str,
    reconciliation_outcome: str | None = None,
) -> dict[str, Any] | None:
    """Settle exact trigger fire intents in the RuntimeTask transaction."""

    metadata = dict(task.metadata_json or {})
    existing = metadata.get("trigger_settlement")
    reconciling_hold = reconciliation_outcome is not None
    if (
        isinstance(existing, dict)
        and str(existing.get("runtime_task_id") or "") == str(task.id)
        and not reconciling_hold
    ):
        return existing
    prior_outcomes: dict[str, str] = {}
    if reconciling_hold:
        if reconciliation_outcome not in {"success", "failure", "release"}:
            raise ValueError(f"Unsupported trigger reconciliation outcome: {reconciliation_outcome!r}")
        if not isinstance(existing, dict) or str(existing.get("runtime_task_id") or "") != str(task.id):
            raise ValueError("trigger reconciliation requires the exact prior settlement receipt")
        raw_prior_outcomes = existing.get("trigger_outcomes")
        if not isinstance(raw_prior_outcomes, dict):
            raise ValueError("trigger reconciliation prior settlement outcomes are missing")
        prior_outcomes = {str(key): str(value) for key, value in raw_prior_outcomes.items()}
        if not prior_outcomes or any(
            value not in {"success", "failure", "hold", "release"} for value in prior_outcomes.values()
        ):
            raise ValueError("trigger reconciliation prior settlement outcomes are invalid")
        held_ids = [
            trigger_id
            for value, prior_outcome in prior_outcomes.items()
            if prior_outcome == "hold" and (trigger_id := _coerce_task_id(value)) is not None
        ]
        if not held_ids or len(held_ids) != sum(value == "hold" for value in prior_outcomes.values()):
            raise ValueError("trigger reconciliation has no exact held trigger settlement")
        trigger_ids = held_ids
    else:
        trigger_ids = [
            trigger_id
            for value in metadata.get("trigger_ids") or []
            if (trigger_id := _coerce_task_id(value)) is not None
        ]
    if not trigger_ids or task.parent_agent_id is None:
        return None

    rows = list(
        (
            await db.execute(
                select(AgentTrigger)
                .where(
                    AgentTrigger.id.in_(trigger_ids),
                    AgentTrigger.agent_id == task.parent_agent_id,
                )
                .order_by(AgentTrigger.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if reconciling_hold:
        rows_by_id = {str(trigger.id): trigger for trigger in rows}
        held_id_text = {str(trigger_id) for trigger_id in trigger_ids}
        if set(rows_by_id) != held_id_text or any(
            not _trigger_inflight_matches(dict(trigger.config or {}), task.id) for trigger in rows
        ):
            raise ValueError("trigger reconciliation hold no longer matches canonical inflight state")
    now = datetime.now(timezone.utc)
    skip_reason = str(metadata.get("skip_reason") or "").strip()
    outcome = reconciliation_outcome or (
        "success"
        if status == "completed" or (status == "skipped" and skip_reason == "workflow_ref_handled")
        else "failure"
        if status in {"failed", "killed"}
        else "hold"
        if status == "needs_reconciliation"
        else "release"
    )
    overrides = metadata.get("trigger_settlement_overrides")
    overrides = {} if reconciling_hold else (dict(overrides) if isinstance(overrides, dict) else {})
    settled: list[str] = list(prior_outcomes)
    settled_outcomes: dict[str, str] = dict(prior_outcomes)
    failure_backoff = (
        [dict(item) for item in existing.get("failure_backoff", []) if isinstance(item, dict)]
        if reconciling_hold and isinstance(existing, dict)
        else []
    )
    newly_settled_outcomes: dict[str, str] = {}
    for trigger in rows:
        config = dict(trigger.config or {})
        if not _trigger_inflight_matches(config, task.id):
            continue
        trigger_outcome = str(overrides.get(str(trigger.id)) or outcome)
        if trigger_outcome not in {"success", "failure", "hold", "release"}:
            raise ValueError(f"Unsupported trigger settlement outcome: {trigger_outcome!r}")
        if trigger_outcome == "hold":
            inflight = dict(config["_fire_inflight"])
            inflight["hold"] = True
            inflight["hold_reason"] = str(
                metadata.get("reconciliation_reason") or metadata.get("restart_resume_blocker") or status
            )
            config["_fire_inflight"] = inflight
            trigger.config = config
        elif trigger_outcome == "success":
            from app.services.trigger_failure_policy import reset_trigger_failure_policy

            reset_trigger_failure_policy(trigger)
            config = dict(trigger.config or {})
            config.pop("_fire_inflight", None)
            if trigger.type == "webhook":
                config["_webhook_pending"] = False
                config["_webhook_payload"] = None
            trigger.config = config
            trigger.last_fired_at = now
            trigger.fire_count = int(trigger.fire_count or 0) + 1
            if trigger.type == "once":
                trigger.is_enabled = False
            if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
                trigger.is_enabled = False
        elif trigger_outcome == "failure":
            from app.services.trigger_failure_policy import apply_trigger_failure_policy

            failure = apply_trigger_failure_policy(
                trigger,
                error=str(metadata.get("terminal_reason") or metadata.get("error") or task.result_summary or status),
                now=now,
            )
            config = dict(trigger.config or {})
            config.pop("_fire_inflight", None)
            trigger.config = config
            failure_backoff.append(
                {
                    "trigger_id": str(trigger.id),
                    "trigger_name": trigger.name,
                    **failure,
                }
            )
        else:
            config.pop("_fire_inflight", None)
            trigger.config = config
        trigger_id_text = str(trigger.id)
        if trigger_id_text not in settled:
            settled.append(trigger_id_text)
        settled_outcomes[trigger_id_text] = trigger_outcome
        newly_settled_outcomes[trigger_id_text] = trigger_outcome

    receipt: dict[str, Any] = {
        "schema": "trigger_runtime_settlement.v1",
        "runtime_task_id": str(task.id),
        "status": status,
        "outcome": outcome,
        "trigger_ids": settled,
        "trigger_outcomes": settled_outcomes,
    }
    if failure_backoff:
        receipt["failure_backoff"] = failure_backoff
    existing_audit_id = str(existing.get("audit_log_id") or "") if isinstance(existing, dict) else ""
    if existing_audit_id:
        receipt["audit_log_id"] = existing_audit_id
    elif any(value == "success" for value in newly_settled_outcomes.values()):
        audit_id = uuid.uuid5(task.id, "trigger-settlement-audit")
        db.add(
            AuditLog(
                id=audit_id,
                tenant_id=task.tenant_id,
                agent_id=task.parent_agent_id,
                action="trigger_fired",
                details={
                    "runtime_task_id": str(task.id),
                    "terminal_status": status,
                    "trigger_ids": settled,
                    "trigger_names": list(metadata.get("trigger_names") or []),
                    "trigger_types": list(metadata.get("trigger_types") or []),
                    "trigger_outcomes": settled_outcomes,
                },
            )
        )
        receipt["audit_log_id"] = str(audit_id)
    return receipt


async def settle_trigger_runtime_reconciliation(
    db: Any,
    task: RuntimeTask,
    *,
    disposition: str,
) -> tuple[str, dict[str, Any]]:
    """Advance only held trigger intents after an authenticated operator decision."""

    outcome_by_disposition = {
        "confirmed_success": "success",
        "confirmed_failure": "failure",
        "release": "release",
    }
    outcome = outcome_by_disposition.get(str(disposition or "").strip())
    if outcome is None:
        raise ValueError(f"Unsupported trigger reconciliation disposition: {disposition!r}")
    existing = dict(task.metadata_json or {}).get("trigger_settlement")
    prior_outcomes = dict(existing.get("trigger_outcomes") or {}) if isinstance(existing, dict) else {}
    final_outcomes = {
        str(trigger_id): outcome if str(prior_outcome) == "hold" else str(prior_outcome)
        for trigger_id, prior_outcome in prior_outcomes.items()
    }
    if not final_outcomes or final_outcomes == prior_outcomes or "hold" in final_outcomes.values():
        raise ValueError("trigger reconciliation requires at least one exact held settlement")
    target_status = (
        "failed"
        if "failure" in final_outcomes.values()
        else "completed"
        if "success" in final_outcomes.values()
        else "killed"
    )
    settlement = await _settle_trigger_runtime_task(
        db,
        task,
        status=target_status,
        reconciliation_outcome=outcome,
    )
    if settlement is None or dict(settlement.get("trigger_outcomes") or {}) != final_outcomes:
        raise ValueError("trigger reconciliation did not settle the exact held trigger set")
    settlement["status"] = target_status
    settlement["outcome"] = (
        "failure" if target_status == "failed" else "success" if target_status == "completed" else "release"
    )
    settlement["reconciliation_disposition"] = disposition
    return target_status, settlement


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
    if task_type == "delegation":
        return bool(
            metadata.get("resume_after_restart")
            and metadata.get("resumable_delegation")
            and has_restart_replay_contract(metadata, task_type="delegation", task_id=task_id_text)
        )
    if task_type == "subagent":
        return bool(metadata.get("resume_after_restart") and metadata.get("resumable_subagent"))
    return False


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
        "summary": (summary or "").strip(),
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
    root_user_id: uuid.UUID | None = None,
    root_session_id: str | None = None,
    root_runtime_task_id: uuid.UUID | str | None = None,
    delegation_chain: list[str] | tuple[str, ...] | None = None,
    root_idempotency_key: str | None = None,
    config_snapshot_hash: str | None = None,
    policy_snapshot_hash: str | None = None,
    root_item_intent_key: str | None = None,
    root_item_work_type: str | None = None,
    root_item_target_ref: str | None = None,
    root_item_path: list[str] | tuple[str, ...] | None = None,
    root_item_state: str = "queued",
    root_item_admission_disposition: str | None = None,
    root_item_reason_code: str | None = None,
    root_item_approval_ref: str | None = None,
) -> str:
    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        raise ValueError(f"Invalid runtime task id: {task_id!r}")
    normalized_root_runtime_task_id = (
        _coerce_task_id(root_runtime_task_id) if root_runtime_task_id is not None else None
    )
    if root_runtime_task_id is not None and normalized_root_runtime_task_id is None:
        raise ValueError(f"Invalid root runtime task id: {root_runtime_task_id!r}")

    started_at = datetime.now(timezone.utc) if status == "running" else None
    # Stage-2b: runtime_tasks now carries a tenant_id (RLS). Derive it from the
    # parent agent so the INSERT lands inside the agent's tenant. A missing or
    # unresolved parent is a blocked precondition, never a global orphan row.
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
        raise RuntimeTenantPreconditionError(
            reason_code="tenant_required",
            message=f"runtime_task:{task_type} requires parent_agent_id to resolve tenant authority.",
            source=f"runtime_task:{task_type}",
        )
    async with tenant_scoped_session(
        tenant_id,
        require_tenant=True,
        source=f"runtime_task:{task_type}",
    ) as db:
        try:
            effective_root_runtime_task_id = (
                normalized_root_runtime_task_id
                if normalized_root_runtime_task_id is not None
                else runtime_task_id
                if str(root_item_intent_key or "").strip()
                else None
            )
            if (
                budget_run_id is not None
                and str(budget_reservation_key or "").strip()
                and budget_admission_status == "reserved"
            ):
                from app.services.runtime_budget_service import (
                    RuntimeBudgetSettlementConflict,
                    assert_runtime_task_budget_reservation_open,
                )

                existing_task = await assert_runtime_task_budget_reservation_open(
                    db,
                    budget_run_id=budget_run_id,
                    reservation_key=str(budget_reservation_key),
                    runtime_task_id=runtime_task_id,
                )
                if existing_task is not None:
                    expected_binding = (
                        tenant_id,
                        task_type,
                        parent_agent_id,
                        child_agent_id,
                        budget_run_id,
                        str(budget_reservation_key),
                        effective_root_runtime_task_id,
                    )
                    durable_binding = (
                        existing_task.tenant_id,
                        existing_task.task_type,
                        existing_task.parent_agent_id,
                        existing_task.child_agent_id,
                        existing_task.budget_run_id,
                        str(existing_task.budget_reservation_key or ""),
                        existing_task.root_runtime_task_id,
                    )
                    if durable_binding != expected_binding:
                        raise RuntimeBudgetSettlementConflict(
                            f"RuntimeTask {runtime_task_id} is already bound to another execution"
                        )
                    return existing_task.id.hex
            task = RuntimeTask(
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
                root_user_id=root_user_id,
                root_session_id=str(root_session_id or "").strip() or None,
                root_runtime_task_id=effective_root_runtime_task_id,
                delegation_chain_json=[str(item) for item in (delegation_chain or []) if str(item).strip()],
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
            from app.services.session_writer_epoch import assign_runtime_task_writer_generation

            await assign_runtime_task_writer_generation(db, task)
            db.add(task)
            if str(root_item_intent_key or "").strip():
                from app.services.runtime_root_ledger import register_runtime_root_item

                await register_runtime_root_item(
                    db,
                    tenant_id=tenant_id,
                    root_runtime_task_id=effective_root_runtime_task_id or runtime_task_id,
                    parent_runtime_task_id=(
                        normalized_root_runtime_task_id if normalized_root_runtime_task_id != runtime_task_id else None
                    ),
                    runtime_task_id=runtime_task_id,
                    source_agent_id=parent_agent_id,
                    root_user_id=root_user_id,
                    root_session_id=root_session_id,
                    intent_key=str(root_item_intent_key),
                    work_type=str(root_item_work_type or task_type),
                    target_ref=str(
                        root_item_target_ref
                        or (f"agent:{child_agent_id}" if child_agent_id is not None else f"runtime:{task_type}")
                    ),
                    path=root_item_path or delegation_chain,
                    state=root_item_state,
                    admission_disposition=root_item_admission_disposition,
                    reason_code=root_item_reason_code,
                    budget_reservation_key=budget_reservation_key,
                    approval_ref=root_item_approval_ref,
                    child_session_id=child_session_id,
                    metadata={"runtime_task_type": task_type},
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

    completion_notification = fields.pop("completion_notification", None)
    root_item_state = str(fields.pop("root_item_state", "") or "").strip() or None
    root_item_reason_code = str(fields.pop("root_item_reason_code", "") or "").strip() or None
    if completion_notification is not None and not isinstance(completion_notification, CompletionNotification):
        raise TypeError("completion_notification must be CompletionNotification")

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
            result = await db.execute(select(RuntimeTask).where(RuntimeTask.id == runtime_task_id).with_for_update())
            task = result.scalar_one_or_none()
            if task is None:
                return False

            from app.services.runtime_task_fence import assert_runtime_task_fence

            assert_runtime_task_fence(task)

            requested_status = str(fields.get("status") or "").strip()
            current_status = str(getattr(task, "status", "") or "").strip()
            sealed_terminal_statuses = {"completed", "failed", "killed", "skipped"}
            incoming_metadata = fields.get("metadata_json")
            if requested_status in _TERMINAL_STATUSES:
                terminal_metadata = dict(incoming_metadata) if isinstance(incoming_metadata, dict) else {}
                existing_metadata = dict(getattr(task, "metadata_json", None) or {})
                if (
                    "runtime_budget_actuals" not in terminal_metadata
                    and "runtime_budget_actuals" not in existing_metadata
                ):
                    outer_actuals = runtime_task_outer_budget_actuals(task)
                    if outer_actuals:
                        terminal_metadata["runtime_budget_actuals"] = outer_actuals
                        fields["metadata_json"] = terminal_metadata
                        incoming_metadata = terminal_metadata
            if isinstance(incoming_metadata, dict) and "runtime_budget_actuals" in incoming_metadata:
                from app.services.runtime_budget_service import RuntimeBudgetSettlementConflict

                existing_actuals = dict(getattr(task, "metadata_json", None) or {}).get("runtime_budget_actuals")
                try:
                    incoming_actuals = _normalized_runtime_budget_actuals(incoming_metadata["runtime_budget_actuals"])
                    existing_normalized = (
                        _normalized_runtime_budget_actuals(existing_actuals) if existing_actuals is not None else None
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeBudgetSettlementConflict(
                        f"invalid runtime budget actuals for RuntimeTask {runtime_task_id}"
                    ) from exc
                if existing_normalized is not None and incoming_actuals != existing_normalized:
                    raise RuntimeBudgetSettlementConflict(
                        f"runtime budget actuals conflict for RuntimeTask {runtime_task_id}"
                    )
            if current_status in sealed_terminal_statuses and requested_status == current_status:
                from app.services.runtime_budget_service import RuntimeBudgetSettlementConflict

                for field_name in ("budget_run_id", "budget_reservation_key"):
                    if field_name not in fields:
                        continue
                    persisted_value = getattr(task, field_name, None)
                    incoming_value = fields[field_name]
                    if persisted_value is not None and str(incoming_value) != str(persisted_value):
                        raise RuntimeBudgetSettlementConflict(
                            f"{field_name} conflict for terminal RuntimeTask {runtime_task_id}"
                        )
            if current_status in sealed_terminal_statuses and requested_status and requested_status != current_status:
                metadata = dict(getattr(task, "metadata_json", None) or {})
                metadata["late_terminal_attempt"] = {
                    "observed_status": current_status,
                    "requested_status": requested_status,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                }
                task.metadata_json = metadata
                if getattr(task, "root_runtime_task_id", None) is not None:
                    from app.services.runtime_root_ledger import transition_runtime_root_item_by_task

                    await transition_runtime_root_item_by_task(
                        db,
                        runtime_task_id=runtime_task_id,
                        requested_state=_runtime_task_root_state(
                            requested_status,
                            override=root_item_state,
                        ),
                        reason_code=root_item_reason_code or "late_terminal_attempt",
                        metadata={"observed_runtime_task_status": current_status},
                    )
                await db.commit()
                return False

            direct_terminal_snapshot = None
            if (
                current_status in _TERMINAL_STATUSES
                and getattr(task, "task_type", None) in {"business_task", "trigger", "delegation"}
                and getattr(task, "terminal_boundary_enqueued_at", None) is not None
            ):
                from app.services.direct_invocation_terminal_boundary_processor import (
                    direct_terminal_authority_snapshot,
                )

                direct_terminal_snapshot = direct_terminal_authority_snapshot(task)

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
            if status in _TERMINAL_STATUSES and task.task_type == "trigger":
                settlement = await _settle_trigger_runtime_task(db, task, status=str(status))
                if settlement is not None:
                    metadata = dict(task.metadata_json or {})
                    metadata["trigger_settlement"] = settlement
                    if settlement.get("failure_backoff"):
                        metadata["failure_backoff"] = list(settlement["failure_backoff"])
                    task.metadata_json = metadata
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
            if direct_terminal_snapshot is not None:
                from app.services.direct_invocation_terminal_boundary_processor import (
                    direct_terminal_authority_snapshot,
                )

                if direct_terminal_authority_snapshot(task) != direct_terminal_snapshot:
                    await db.rollback()
                    logger.warning(
                        "Rejected RuntimeTask %s mutation after its direct terminal boundary was sealed",
                        runtime_task_id,
                    )
                    return False
            trigger_delivery = str((getattr(task, "metadata_json", None) or {}).get("delivery") or "")
            intentional_trigger_no_delivery = (
                getattr(task, "task_type", None) == "trigger"
                and completion_notification is None
                and (status == "skipped" or trigger_delivery == "workflow")
            )
            if intentional_trigger_no_delivery:
                # Skipped polls have no result to deliver. Pure-workflow
                # wrappers leave user delivery to the child workflow run.
                # Both still enqueue their direct terminal evidence below.
                task.completion_outbox_settled_at = now
                task.completion_outbox_last_error = None
                metadata = dict(getattr(task, "metadata_json", None) or {})
                metadata["completion_delivery_disposition"] = (
                    "workflow_child_owned" if trigger_delivery == "workflow" else "intentional_no_delivery"
                )
                task.metadata_json = metadata
            if completion_notification is not None:
                if status not in _TERMINAL_STATUSES:
                    raise ValueError("completion_notification requires a terminal RuntimeTask status")
                outbox_id = await enqueue_completion_notification(db, completion_notification)
                metadata = dict(getattr(task, "metadata_json", None) or {})
                metadata["completion_outbox_id"] = str(outbox_id)
                task.metadata_json = metadata

            if status in _TERMINAL_STATUSES:
                from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal

                await settle_and_enqueue_runtime_task_terminal(
                    db,
                    task,
                    terminal_source="runtime_task_service.update",
                    root_reason_code=(
                        root_item_reason_code or str(fields.get("budget_terminal_reason") or "").strip() or None
                    ),
                    root_state=_runtime_task_root_state(str(status), override=root_item_state),
                )

            if (
                requested_status
                and requested_status not in _TERMINAL_STATUSES
                and getattr(task, "root_runtime_task_id", None) is not None
            ):
                from app.services.runtime_root_ledger import transition_runtime_root_item_by_task

                await transition_runtime_root_item_by_task(
                    db,
                    runtime_task_id=runtime_task_id,
                    requested_state=_runtime_task_root_state(
                        requested_status,
                        override=root_item_state,
                    ),
                    reason_code=(
                        root_item_reason_code or str(fields.get("budget_terminal_reason") or "").strip() or None
                    ),
                    result_refs=[f"runtime-task://{runtime_task_id}"],
                )

            if status in _TERMINAL_STATUSES:
                from app.services.runtime_task_fence import finish_current_runtime_task_claim

                finish_current_runtime_task_claim(task_id=runtime_task_id)
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
    root_user_id: uuid.UUID | None = None,
    root_session_id: str | None = None,
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
            if root_user_id is not None:
                stmt = stmt.where(RuntimeTask.root_user_id == root_user_id)
            if root_session_id is not None:
                stmt = stmt.where(RuntimeTask.root_session_id == str(root_session_id))
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
                select(RuntimeTask)
                .where(
                    RuntimeTask.id.in_(task_ids),
                    RuntimeTask.status == "running",
                    or_(
                        RuntimeTask.task_type.is_(None),
                        RuntimeTask.task_type.notin_(_RESTART_RESUMABLE_TASK_TYPES),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            for task in result.scalars().all():
                if getattr(task, "id", None) in excluded:
                    continue
                task_type = str(getattr(task, "task_type", None) or "runtime_task")
                metadata = dict(getattr(task, "metadata_json", None) or {})
                if task_type in {"delegation", "trigger"} and _is_restart_resumable_runtime_task(task):
                    # Runs beyond a bounded startup scan remain reachable
                    # through lease reclaim. Replay safety is enforced after
                    # the worker binds a fresh claim fence.
                    continue
                if _is_restart_resumable_runtime_task(task):
                    if task_type in _RESTART_RESUMABLE_TASK_TYPES:
                        continue
                    metadata.setdefault("restart_resume_blocker", "restart_resume_not_confirmed")
                if task_type in {"delegation", "subagent", "trigger", "heartbeat", "business_task"}:
                    task.status = "needs_reconciliation"
                    blocker = str(metadata.get("restart_resume_blocker") or "non_idempotent_restart_orphan")
                    if not task.result_summary:
                        task.result_summary = (
                            "Task was interrupted by a worker restart and may have performed external side effects; "
                            "it requires reconciliation before retrying or marking complete."
                        )
                    reconciliation_metadata = build_restart_reconciliation_metadata(
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
                    outer_actuals = runtime_task_outer_budget_actuals(task)
                    if outer_actuals:
                        reconciliation_metadata["runtime_budget_actuals"] = outer_actuals
                    task.metadata_json = reconciliation_metadata
                    if task_type == "business_task":
                        try:
                            business_task_id = uuid.UUID(str(metadata.get("business_task_id")))
                        except (TypeError, ValueError):
                            business_task_id = None
                        if business_task_id is not None:
                            business_task = (
                                await db.execute(
                                    select(Task)
                                    .where(
                                        Task.id == business_task_id,
                                        Task.tenant_id == tenant_id,
                                        Task.agent_id == task.parent_agent_id,
                                    )
                                    .with_for_update()
                                )
                            ).scalar_one_or_none()
                            if business_task is not None and business_task.active_runtime_task_id == task.id:
                                business_task.status = "needs_reconciliation"
                                business_task.last_execution_status = "needs_reconciliation"
                                business_task.last_error = task.result_summary
                                business_task.completed_at = now
                    task.completed_at = now
                    from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal

                    await settle_and_enqueue_runtime_task_terminal(
                        db,
                        task,
                        terminal_source="runtime_task_service.startup_orphan_reconciliation",
                        root_reason_code=blocker,
                    )
                    updated += 1
                    continue
                task.status = "failed"
                task.completed_at = now
                if not task.result_summary:
                    task.result_summary = "Task failed because the worker process restarted before completion."
                metadata["orphaned_by_restart"] = True
                outer_actuals = runtime_task_outer_budget_actuals(task)
                if outer_actuals:
                    metadata["runtime_budget_actuals"] = outer_actuals
                task.metadata_json = metadata
                from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal

                await settle_and_enqueue_runtime_task_terminal(
                    db,
                    task,
                    terminal_source="runtime_task_service.startup_orphan_reconciliation",
                    root_reason_code="worker_restart_orphan",
                )
                updated += 1
    return updated
