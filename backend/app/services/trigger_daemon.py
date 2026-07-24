"""Trigger Daemon — evaluates all agent triggers in a single background loop.

Replaces the separate heartbeat and scheduler services with a unified trigger
evaluation engine. Runs as an asyncio background task.

Every 15 seconds:
  1. Load all enabled triggers from DB
  2. Evaluate each trigger (cron/once/interval/poll/on_message/webhook)
  3. Group fired triggers by agent_id (30s dedup window)
  4. Invoke each agent once with all its fired triggers as context
"""

import asyncio
import hashlib
import ipaddress
import json as _json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from croniter import croniter
from loguru import logger
from sqlalchemy import select

from app.runtime.context_candidates import build_context_candidate_ref
from app.services.daemon_concurrency import run_bounded
from app.core.events import get_redis
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.trigger import AgentTrigger
from app.models.agent import Agent
from app.services.agent_identity_lifecycle import agent_lifecycle_active_clause
from app.services.plan_mode_core import build_plan_execution_instruction
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.runtime_tenant_admission import admit_agent_runtime_tenant
from app.services.runtime_task_service import (
    build_restart_reconciliation_metadata,
    build_restart_replay_contract,
    build_restart_replay_journal_entry,
    create_runtime_task_record,
    get_runtime_task_record,
    list_active_runtime_task_records,
    merge_restart_replay_journal,
    update_runtime_task_record,
)
from app.services.execution_admission import ExecutionAdmission, ExecutionAdmissionDecision
from app.services.runtime_budget_service import (
    RuntimeBudgetPolicyLookup,
    RuntimeBudgetReservation,
    RuntimeBudgetRunCreate,
    RuntimeBudgetService,
)
from app.services.runtime_notification_outbox import CompletionNotification
from app.services.trigger_preflight import (
    collect_trigger_runtime_options,
    evaluate_trigger_preflight,
    load_context_from,
    select_trigger_model,
)

TICK_INTERVAL = 15  # seconds
DEDUP_WINDOW = 120  # seconds — same agent won't be invoked twice within this window
MAX_AGENT_CHAIN_DEPTH = 5  # A→B→A→B→A max depth before stopping
MIN_POLL_INTERVAL_MINUTES = 5  # align with tenant/agent min_poll_interval_floor defaults
MAX_FIRES_PER_HOUR = 6  # hard cap: ~10 min minimum interval between fires
_TRIGGER_FIRE_LEASE_TTL_SECONDS = 600
_TRIGGER_FIRE_INFLIGHT_STALE_SECONDS = 6 * 60 * 60

# Track last invocation time per agent to enforce dedup window
_last_invoke: dict[uuid.UUID, datetime] = {}

# Track fire timestamps per agent for hourly rate limiting
_fire_history: dict[uuid.UUID, list[datetime]] = {}


class TriggerRuntimeTaskRef(str):
    """String-compatible task id carrying its durable admission state."""

    admission_status: str

    def __new__(cls, value: str, *, admission_status: str):
        instance = super().__new__(cls, value)
        instance.admission_status = admission_status
        return instance


def _runtime_task_uuid_or_none(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _trigger_completion_notification(
    *,
    runtime_task_id: str | None,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    status: str,
    summary: str,
    trigger_names: list[str] | None = None,
    trigger_types: list[str] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> CompletionNotification | None:
    if not runtime_task_id or tenant_id is None or user_id is None or session_id is None:
        return None
    return CompletionNotification(
        tenant_id=tenant_id,
        source_kind="trigger",
        source_run_id=str(runtime_task_id),
        parent_session_id=session_id,
        parent_agent_id=agent_id,
        parent_user_id=user_id,
        terminal_status=status,
        task_type="trigger",
        summary=summary,
        delivery_mode="session_projection",
        artifacts=list(artifacts or []),
        metadata={
            "trigger_names": list(trigger_names or []),
            "trigger_types": list(trigger_types or []),
            **(metadata or {}),
        },
    )


def _confirmed_plan_ref_from_trigger(trigger: AgentTrigger) -> dict[str, Any]:
    config = getattr(trigger, "config", None) or {}
    plan_id = config.get("plan_id")
    if not plan_id:
        return {}
    return {
        "plan_id": str(plan_id),
        "plan_version": config.get("plan_version"),
        "plan_hash": config.get("plan_hash"),
    }


def _build_trigger_wake_context_candidate(
    triggers: list[AgentTrigger],
    *,
    runtime_task_id: str | None = None,
    budget_run_id: str | None = None,
    preflight_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trigger_ids = [str(getattr(trigger, "id", "")) for trigger in triggers if str(getattr(trigger, "id", "")).strip()]
    trigger_classes = [
        str((getattr(trigger, "config", None) or {}).get("trigger_class") or "")
        for trigger in triggers
        if (getattr(trigger, "config", None) or {}).get("trigger_class")
    ]
    confirmed_plan_ref = next(
        (ref for ref in (_confirmed_plan_ref_from_trigger(trigger) for trigger in triggers) if ref),
        {},
    )
    payload = {
        "trigger_ids": trigger_ids,
        "trigger_classes": trigger_classes,
        "runtime_task_id": runtime_task_id,
        "budget_run_id": budget_run_id,
        "confirmed_plan_ref": confirmed_plan_ref,
        "preflight_decision": dict(preflight_decision or {}),
    }
    ref = build_context_candidate_ref(
        kind="trigger_wake",
        item_id="+".join(trigger_ids) or "+".join(str(getattr(trigger, "name", "")) for trigger in triggers),
        payload=payload,
    ).to_manifest()
    return {
        "schema": "hive.ccplus.trigger_wake_context_candidate.v1",
        "context_candidate_ref": ref,
        **payload,
    }


async def _create_trigger_runtime_task(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    *,
    metadata_json: dict | None = None,
) -> str | None:
    """Create the mandatory RuntimeTask ledger row for a fired trigger batch.

    Callers must fail closed on ``None``. A trigger is never allowed to execute
    when its durable evidence/recovery row could not be committed.
    """
    trigger_names = [str(getattr(trigger, "name", "")) for trigger in triggers]
    metadata = {
        "source": "trigger_daemon",
        "agent_id": str(agent_id),
        "trigger_ids": [str(getattr(trigger, "id", "")) for trigger in triggers],
        "trigger_names": trigger_names,
        "trigger_types": [str(getattr(trigger, "type", "")) for trigger in triggers],
        "trigger_classes": [
            str((getattr(trigger, "config", None) or {}).get("trigger_class") or "")
            for trigger in triggers
            if (getattr(trigger, "config", None) or {}).get("trigger_class")
        ],
    }
    metadata.update(metadata_json or {})
    reservation_service: RuntimeBudgetService | None = None
    admission_decision: ExecutionAdmissionDecision | None = None
    budget_reservation_key: str | None = None
    try:
        task_id = uuid.uuid4().hex
        trace_id = f"trigger:{task_id}"
        side_effect_risk = str(metadata.get("side_effect_risk") or "mutating")
        tenant_id = await resolve_tenant_for_agent(agent_id)
        budget_run = None
        trigger_id_values = [getattr(trigger, "id", None) for trigger in triggers if getattr(trigger, "id", None)]
        trigger_profile = str(
            next(
                (
                    (getattr(trigger, "config", None) or {}).get("trigger_class")
                    for trigger in triggers
                    if (getattr(trigger, "config", None) or {}).get("trigger_class")
                ),
                "",
            )
            or (getattr(triggers[0], "type", "") if triggers else "")
            or "trigger"
        )
        if tenant_id is not None:
            reservation_service = RuntimeBudgetService()
            budget_lookup = RuntimeBudgetPolicyLookup(
                tenant_id=tenant_id,
                source="trigger",
                profile=trigger_profile,
                agent_id=agent_id,
                trigger_id=trigger_id_values[0] if len(trigger_id_values) == 1 else None,
            )
            budget_policy = await reservation_service.resolve_policy(budget_lookup)
            budget_run = await reservation_service.create_run(
                RuntimeBudgetRunCreate(
                    tenant_id=tenant_id,
                    root_run_kind="trigger_fire",
                    root_run_key=task_id,
                    source="trigger",
                    profile=trigger_profile,
                    policy_id=getattr(budget_policy, "id", None),
                    root_runtime_task_id=uuid.UUID(task_id),
                    root_agent_id=agent_id,
                    enforcement_mode=str(getattr(budget_policy, "enforcement_mode", None) or "enforce"),
                    fail_mode=str(getattr(budget_policy, "fail_mode", None) or "fail_closed"),
                    max_tokens=getattr(budget_policy, "max_tokens", None),
                    max_cache_miss_tokens=getattr(budget_policy, "max_cache_miss_tokens", None),
                    max_subagents=getattr(budget_policy, "max_subagents", None),
                    max_team_sessions=getattr(budget_policy, "max_team_sessions", None),
                    max_delegations=getattr(budget_policy, "max_delegations", None),
                    max_background_tasks=getattr(budget_policy, "max_background_tasks", None),
                    max_continuation_wakes=getattr(budget_policy, "max_continuation_wakes", None),
                    max_provider_calls=getattr(budget_policy, "max_provider_calls", None),
                    max_failures=getattr(budget_policy, "max_failures", None),
                    max_needs_reconciliation=getattr(budget_policy, "max_needs_reconciliation", None),
                    max_child_failure_ratio=getattr(budget_policy, "max_child_failure_ratio", None),
                    max_parent_invocations=getattr(budget_policy, "max_parent_invocations", None),
                    policy_snapshot={
                        "policy_id": str(getattr(budget_policy, "id", "")),
                        "scope_type": getattr(budget_policy, "scope_type", None),
                        "source": getattr(budget_policy, "source", None),
                        "profile": getattr(budget_policy, "profile", None),
                        "max_team_sessions": getattr(budget_policy, "max_team_sessions", None),
                        "default_child_token_reservation": getattr(
                            budget_policy, "default_child_token_reservation", None
                        ),
                        "default_llm_call_token_reservation": getattr(
                            budget_policy, "default_llm_call_token_reservation", None
                        ),
                        "policy_json": getattr(budget_policy, "policy_json", None),
                    },
                )
            )
            if bool(metadata.get("preflight_allowed", True)):
                budget_reservation_key = f"trigger:{task_id}:start"
                admission_decision = await ExecutionAdmission(reservation_service).admit(
                    RuntimeBudgetReservation(
                        budget_run_id=budget_run.id,
                        reservation_key=budget_reservation_key,
                        background_tasks=1,
                        reason="trigger_start",
                        runtime_task_id=uuid.UUID(task_id),
                        metadata={
                            "work_type": "trigger",
                            "agent_id": str(agent_id),
                            "trigger_ids": [str(value) for value in trigger_id_values],
                        },
                    )
                )
        metadata.update(
            {
                "runtime_task_id": task_id,
                "request_id": str(uuid.UUID(task_id)),
                "trace_id": trace_id,
                "resumable_trigger": True,
                "resume_after_restart": True,
                "side_effect_risk": side_effect_risk,
                "restart_replay_contract": build_restart_replay_contract(
                    task_type="trigger",
                    task_id=task_id,
                    side_effect_risk=side_effect_risk,
                    trace_id=trace_id,
                ),
            }
        )
        if budget_run is not None:
            metadata["budget_run_id"] = str(budget_run.id)
            metadata["budget_reservation_key"] = budget_reservation_key
            metadata["execution_admission_status"] = (
                admission_decision.status if admission_decision is not None else "not_required"
            )
        trigger_wake_candidate = _build_trigger_wake_context_candidate(
            triggers,
            runtime_task_id=task_id,
            budget_run_id=metadata.get("budget_run_id"),
            preflight_decision=metadata_json or {},
        )
        metadata["trigger_wake_context_candidate"] = trigger_wake_candidate
        metadata.setdefault("context_candidate_refs", []).append(trigger_wake_candidate["context_candidate_ref"])
        metadata = merge_restart_replay_journal(
            metadata,
            build_restart_replay_journal_entry(
                task_type="trigger",
                task_id=task_id,
                side_effect_risk=side_effect_risk,
                phase="spawn_intent_recorded",
                trace_id=trace_id,
            ),
        )
        admission_status = (
            "waiting_budget_approval" if admission_decision is not None and admission_decision.waiting else "admitted"
        )
        persisted_task_id = await create_runtime_task_record(
            task_id=task_id,
            task_type="trigger",
            status="pending" if admission_status == "waiting_budget_approval" else "running",
            parent_agent_id=agent_id,
            prompt=f"Trigger wake: {', '.join(name for name in trigger_names if name) or 'unknown'}",
            trace_id=trace_id,
            metadata_json=metadata,
            budget_run_id=budget_run.id if budget_run is not None else None,
            budget_reservation_key=budget_reservation_key,
            budget_admission_status=(
                "waiting_budget_approval"
                if admission_status == "waiting_budget_approval"
                else "reserved"
                if budget_run is not None and budget_reservation_key
                else None
            ),
            budget_terminal_reason=(
                "runtime_budget_approval_required" if admission_status == "waiting_budget_approval" else None
            ),
        )
        return TriggerRuntimeTaskRef(persisted_task_id, admission_status=admission_status)
    except Exception as exc:
        if admission_decision is not None and admission_decision.status == "admitted":
            try:
                await ExecutionAdmission(reservation_service).settle(
                    admission_decision,
                    reason="trigger_ledger_create_failed",
                    runtime_task_id=uuid.UUID(task_id),
                )
            except Exception:
                logger.exception("[TriggerDaemon] Failed to release trigger reservation after ledger failure")
        logger.error("[TriggerDaemon] Refusing trigger without RuntimeTask ledger for {}: {}", agent_id, exc)
        return None


async def _update_trigger_runtime_task(
    runtime_task_id: str | None,
    *,
    status: str,
    result_summary: str,
    session_id: str | None = None,
    metadata_json: dict | None = None,
    completion_notification: CompletionNotification | None = None,
) -> None:
    if not runtime_task_id:
        return
    fields = {
        "status": status,
        "result_summary": result_summary,
        "metadata_json": metadata_json or {},
    }
    if session_id:
        fields["child_session_id"] = session_id
    if completion_notification is not None:
        fields["completion_notification"] = completion_notification
    try:
        await update_runtime_task_record(runtime_task_id, **fields)
    except Exception as exc:
        logger.warning("[TriggerDaemon] Failed to update trigger RuntimeTask {}: {}", runtime_task_id, exc)


async def _skip_trigger_runtime_task(
    runtime_task_id: str | None,
    *,
    skip_reason: str,
    result_summary: str,
    metadata_json: dict | None = None,
) -> None:
    metadata = {"skip_reason": skip_reason}
    metadata.update(metadata_json or {})
    await _update_trigger_runtime_task(
        runtime_task_id,
        status="skipped",
        result_summary=result_summary,
        metadata_json=metadata,
    )


async def _mark_trigger_runtime_task_needs_reconciliation(
    runtime_task_id: str,
    *,
    metadata: dict[str, Any] | None,
    blocker: str,
    summary: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> None:
    await update_runtime_task_record(
        runtime_task_id,
        status="needs_reconciliation",
        result_summary=summary,
        metadata_json=build_restart_reconciliation_metadata(
            metadata,
            task_type="trigger",
            task_id=runtime_task_id,
            blocker=blocker,
            summary=summary,
            trace_id=trace_id,
            session_id=session_id,
        ),
    )


async def _load_triggers_for_resume(agent_id: uuid.UUID, trigger_ids: list[str]) -> list[AgentTrigger]:
    parsed_ids: list[uuid.UUID] = []
    for trigger_id in trigger_ids:
        try:
            parsed_ids.append(uuid.UUID(str(trigger_id)))
        except (TypeError, ValueError, AttributeError):
            continue
    if not parsed_ids:
        return []

    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        result = await db.execute(
            select(AgentTrigger).where(AgentTrigger.agent_id == agent_id, AgentTrigger.id.in_(parsed_ids))
        )
        triggers = list(result.scalars().all())
    by_id = {str(trigger.id): trigger for trigger in triggers}
    return [by_id[str(trigger_id)] for trigger_id in parsed_ids if str(trigger_id) in by_id]


async def resume_persisted_trigger_runs(*, limit: int = 50) -> list[str]:
    """Restart-safe recovery for trigger runs that never reached the execution session.

    Once a trigger run has a session id, the previous process may already have
    executed tools or written transcript rows. Without per-tool checkpoints, a
    blind replay can duplicate external side effects, so those runs enter
    ``needs_reconciliation`` instead of being replayed.
    """

    resumed: list[str] = []
    records = await list_active_runtime_task_records(limit=limit, statuses=("pending", "running"))
    for record in records:
        if record.get("task_type") != "trigger":
            continue
        run_id = str(record.get("task_id") or "").strip()
        if not run_id:
            continue
        metadata = dict(record.get("metadata") or {})
        if not metadata.get("resume_after_restart") or not metadata.get("resumable_trigger"):
            continue
        trace_id = str(record.get("trace_id") or metadata.get("trace_id") or "")
        session_id = str(record.get("child_session_id") or metadata.get("session_id") or "").strip()
        if session_id:
            await _mark_trigger_runtime_task_needs_reconciliation(
                run_id,
                metadata=metadata,
                blocker="session_bound_mutating_trigger",
                summary=(
                    "Trigger run was interrupted after binding a session; replay could duplicate side effects. "
                    "Reconciliation is required before retry."
                ),
                trace_id=trace_id,
                session_id=session_id,
            )
            continue
        try:
            agent_id = uuid.UUID(str(record.get("parent_agent_id") or metadata.get("agent_id") or ""))
        except (TypeError, ValueError, AttributeError):
            await _mark_trigger_runtime_task_needs_reconciliation(
                run_id,
                metadata=metadata,
                blocker="missing_trigger_parent_agent",
                summary="Trigger run could not be resumed after restart because parent agent id is unavailable.",
                trace_id=trace_id,
            )
            continue
        trigger_ids = [str(item) for item in metadata.get("trigger_ids", []) if str(item).strip()]
        triggers = await _load_triggers_for_resume(agent_id, trigger_ids)
        if not triggers:
            await _mark_trigger_runtime_task_needs_reconciliation(
                run_id,
                metadata=metadata,
                blocker="missing_resume_triggers",
                summary="Trigger run could not be resumed after restart because its trigger rows are unavailable.",
                trace_id=trace_id,
            )
            continue
        side_effect_risk = str(metadata.get("side_effect_risk") or "mutating")
        resume_metadata = merge_restart_replay_journal(
            metadata,
            build_restart_replay_journal_entry(
                task_type="trigger",
                task_id=run_id,
                side_effect_risk=side_effect_risk,
                phase="resume_intent_recorded",
                trace_id=trace_id,
            ),
        )
        await update_runtime_task_record(
            run_id,
            status="running",
            metadata_json={
                "resumed_after_restart": True,
                "restart_replay_contract": metadata.get("restart_replay_contract"),
                "restart_replay_journal": resume_metadata.get("restart_replay_journal"),
            },
        )
        asyncio.create_task(
            run_bounded("trigger", _invoke_agent_for_triggers(agent_id, triggers, runtime_task_id=run_id))
        )
        resumed.append(run_id)
    return resumed


# M-16: Persist dedup state to survive process restarts
# Use AGENT_DATA_DIR if available, otherwise a restricted temp path
def _get_dedup_path() -> Path:
    try:
        from app.config import get_settings

        return Path(get_settings().AGENT_DATA_DIR) / ".trigger_dedup.json"
    except Exception:
        return Path("/tmp/.hive_trigger_dedup.json")


_DEDUP_FILE = _get_dedup_path()


def _load_dedup_state() -> None:
    global _last_invoke
    try:
        if _DEDUP_FILE.exists():
            data = _json.loads(_DEDUP_FILE.read_text())
            _last_invoke = {uuid.UUID(k): datetime.fromisoformat(v) for k, v in data.items()}
    except Exception as exc:
        logger.debug("[TriggerDaemon] Failed to load dedup state: {}", exc)


def _save_dedup_state() -> None:
    import os
    import tempfile

    try:
        data = {str(k): v.isoformat() for k, v in _last_invoke.items()}
        _DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(_DEDUP_FILE.parent), suffix=".tmp")
        fd_closed = False
        try:
            os.write(tmp_fd, _json.dumps(data).encode("utf-8"))
            os.close(tmp_fd)
            fd_closed = True
            os.replace(tmp_path, str(_DEDUP_FILE))
        except BaseException:
            if not fd_closed:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_path)
            except OSError as _unlink_err:
                logger.debug("[TriggerDaemon] Failed to clean up temp dedup file: {}", _unlink_err)
            raise
    except Exception as exc:
        logger.debug("[TriggerDaemon] Failed to save dedup state: {}", exc)


# Webhook rate limiter: token -> list of timestamps
_webhook_hits: dict[str, list[float]] = {}
WEBHOOK_RATE_LIMIT = 5  # max hits per minute per token


# ── Reply target recovery for pre-unified triggers ──────────────────


async def _recover_reply_target_from_session(
    agent_id: uuid.UUID,
    triggers: list,
) -> dict | None:
    """Try to recover a channel delivery target for triggers that have
    reply_context=NULL (created before the unified-delivery refactor).

    Strategy: find the agent's most recent non-web ChatSession that has
    a delivery_target_json with a "channel" key. This is a best-effort
    heuristic — it picks the last channel the user talked to this agent on.
    """
    from app.models.chat_session import ChatSession

    try:
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.source_channel != "web",
                    ChatSession.source_channel != "agent",
                    ChatSession.delivery_target_json.isnot(None),
                )
                .order_by(ChatSession.last_message_at.desc().nullslast())
                .limit(1)
            )
            session = result.scalar_one_or_none()
            if not session or not session.delivery_target_json:
                return None

            target = dict(session.delivery_target_json)
            if not target.get("channel"):
                return None

            # Persist the recovered context back to the trigger so this
            # fallback only runs once per trigger.
            for trigger in triggers:
                if getattr(trigger, "reply_context", None) is None:
                    try:
                        trigger_r = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger.id))
                        t_obj = trigger_r.scalar_one_or_none()
                        if t_obj:
                            t_obj.reply_context = target
                    except Exception as exc:
                        logger.debug("[TriggerDaemon] reply_context persist skipped: {}", exc)
            await db.commit()
            return target
    except Exception as exc:
        logger.debug("[TriggerDaemon] _recover_reply_target_from_session failed: {}", exc)
        return None


async def backfill_null_reply_contexts() -> dict:
    """One-time startup job: patch all enabled triggers that have reply_context=NULL.

    For each such trigger, look up the agent's most recent non-web ChatSession
    with a delivery_target_json and copy it into reply_context. This fixes
    triggers created before commit c0e00c8 (unified delivery refactor) where
    only Feishu triggers got reply_context — TG/WeChat were left NULL.

    Returns: {"patched": N, "skipped": M}
    """
    from app.models.chat_session import ChatSession

    patched = 0
    skipped = 0
    try:
        async with (
            async_session() as db,
            enter_rls_bypass(db, reason="trigger reply_context backfill — enumerate all tenants' enabled triggers"),
        ):
            # All enabled triggers with NULL reply_context
            null_triggers = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.is_enabled.is_(True),
                    AgentTrigger.reply_context.is_(None),
                )
            )
            triggers = null_triggers.scalars().all()
            if not triggers:
                return {"patched": 0, "skipped": 0}

            # Group by agent for efficient session lookup
            agent_triggers: dict[uuid.UUID, list] = {}
            for t in triggers:
                agent_triggers.setdefault(t.agent_id, []).append(t)

            for aid, agent_trigs in agent_triggers.items():
                # Find the most recent non-web session with delivery_target
                session_r = await db.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.agent_id == aid,
                        ChatSession.source_channel != "web",
                        ChatSession.source_channel != "agent",
                        ChatSession.delivery_target_json.isnot(None),
                    )
                    .order_by(ChatSession.last_message_at.desc().nullslast())
                    .limit(1)
                )
                session = session_r.scalar_one_or_none()
                if not session or not session.delivery_target_json or not session.delivery_target_json.get("channel"):
                    skipped += len(agent_trigs)
                    continue

                target = dict(session.delivery_target_json)
                for t in agent_trigs:
                    t.reply_context = target
                    patched += 1
                    logger.info(
                        "[TriggerDaemon] Backfilled reply_context for trigger '{}' (agent {}): channel={}",
                        t.name,
                        aid,
                        target.get("channel"),
                    )

            await db.commit()
    except Exception as exc:
        logger.warning("[TriggerDaemon] backfill_null_reply_contexts failed: {}", exc)

    return {"patched": patched, "skipped": skipped}


# ── SSRF Protection ─────────────────────────────────────────────────


def _is_private_url(url: str) -> bool:
    """Block private/internal URLs to prevent SSRF attacks."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True

        # Block obvious private hostnames
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True

        # Try to resolve hostname and check IP
        import socket

        try:
            infos = socket.getaddrinfo(hostname, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
        except (socket.gaierror, ValueError):
            return True  # Cannot resolve = block

        return False
    except Exception:
        return True  # Block on any parsing error


def _is_trigger_in_active_hours(active_hours: str, now: datetime, tz_name: str = "UTC") -> bool:
    """Return whether a configured trigger time window is currently active."""
    try:
        from zoneinfo import ZoneInfo

        start_str, end_str = active_hours.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        try:
            tz = ZoneInfo(str(tz_name or "UTC"))
        except (KeyError, Exception):
            tz = ZoneInfo("UTC")
        local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=timezone.utc).astimezone(tz)
        current_minutes = local_now.hour * 60 + local_now.minute
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes
    except Exception:
        return True


# ── Trigger Evaluation ──────────────────────────────────────────────


def _inflight_fire_is_active(config: dict, now: datetime) -> bool:
    inflight = config.get("_fire_inflight")
    if not isinstance(inflight, dict):
        return False
    started_at = inflight.get("started_at")
    if not started_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - parsed) < timedelta(seconds=_TRIGGER_FIRE_INFLIGHT_STALE_SECONDS)


async def _evaluate_trigger(trigger: AgentTrigger, now: datetime) -> bool:
    """Return True if this trigger should fire right now."""
    if not trigger.is_enabled:
        return False
    cfg = trigger.config or {}
    if _inflight_fire_is_active(cfg, now):
        return False
    backoff_until = cfg.get("backoff_until")
    if backoff_until:
        try:
            parsed_backoff = datetime.fromisoformat(str(backoff_until).replace("Z", "+00:00"))
            if parsed_backoff.tzinfo is None:
                parsed_backoff = parsed_backoff.replace(tzinfo=timezone.utc)
            if now < parsed_backoff:
                return False
        except ValueError:
            logger.debug("[TriggerDaemon] Invalid backoff_until for trigger {}: {}", trigger.name, backoff_until)
    if trigger.expires_at and now >= trigger.expires_at:
        # Auto-disable expired triggers
        return False
    if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
        return False

    # Cooldown check
    if trigger.last_fired_at:
        cooldown = timedelta(seconds=trigger.cooldown_seconds)
        if (now - trigger.last_fired_at) < cooldown:
            return False

    active_hours = str(cfg.get("active_hours") or "").strip()
    if active_hours:
        tz_name = str(cfg.get("timezone") or "").strip()
        if not tz_name:
            from app.services.timezone_utils import get_agent_timezone

            tz_name = await get_agent_timezone(trigger.agent_id)
        if not _is_trigger_in_active_hours(active_hours, now, tz_name):
            return False

    t = trigger.type

    if t == "cron":
        expr = cfg.get("expr")
        if not expr:
            logger.warning(f"Cron trigger '{trigger.name}' has no expr in config — skipping")
            return False
        base = trigger.last_fired_at or trigger.created_at
        try:
            # Resolve timezone: trigger config → agent → tenant → UTC
            tz_name = cfg.get("timezone")
            if not tz_name:
                from app.services.timezone_utils import get_agent_timezone

                tz_name = await get_agent_timezone(trigger.agent_id)
            from zoneinfo import ZoneInfo

            try:
                tz = ZoneInfo(tz_name)
            except (KeyError, Exception):
                tz = ZoneInfo("UTC")
            # Evaluate cron in agent's timezone
            local_now = now.astimezone(tz)
            local_base = base.astimezone(tz) if base.tzinfo else base.replace(tzinfo=tz)
            cron = croniter(expr, local_base)
            next_run = cron.get_next(datetime)
            return local_now >= next_run
        except Exception as e:
            logger.warning(f"Invalid cron expr '{expr}' for trigger {trigger.name}: {e}")
            return False

    elif t == "once":
        at_str = cfg.get("at")
        if not at_str:
            return False
        try:
            at = datetime.fromisoformat(at_str)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return now >= at and trigger.fire_count == 0
        except Exception:
            return False

    elif t == "interval":
        minutes = cfg.get("minutes", 30)
        base = trigger.last_fired_at or trigger.created_at
        return (now - base) >= timedelta(minutes=minutes)

    elif t == "poll":
        interval_min = max(cfg.get("interval_min", 5), MIN_POLL_INTERVAL_MINUTES)
        base = trigger.last_fired_at or trigger.created_at
        if (now - base) < timedelta(minutes=interval_min):
            return False
        # Actual HTTP poll + change detection
        return await _poll_check(trigger)

    elif t == "on_message":
        return await _check_new_agent_messages(trigger)

    elif t == "webhook":
        # Check if a webhook payload is pending
        if cfg.get("_webhook_pending"):
            return True
        return False

    return False


async def _poll_check(trigger: AgentTrigger) -> bool:
    """HTTP poll: fetch URL, extract value via json_path, detect change.

    Persists _last_value into the trigger's config JSONB so it survives
    across process restarts.
    """
    import httpx

    cfg = trigger.config or {}
    url = cfg.get("url")
    if not url:
        return False

    # SSRF protection: block private/internal URLs
    if _is_private_url(url):
        logger.warning(f"Poll blocked for trigger {trigger.name}: private/internal URL '{url}'")
        return False

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(cfg.get("method", "GET"), url, headers=cfg.get("headers", {}))
            resp.raise_for_status()

        data = resp.json()
        json_path = cfg.get("json_path", "$")
        current_value = _extract_json_path(data, json_path)
        current_str = str(current_value)

        fire_on = cfg.get("fire_on", "change")
        should_fire = False

        if fire_on == "match":
            should_fire = current_str == str(cfg.get("match_value", ""))
            if should_fire:
                cfg["_last_event"] = f"Polled {url} → value matched: {current_str}"
        else:  # "change"
            last_value = cfg.get("_last_value")
            # First poll — don't fire, just record baseline
            if last_value is None:
                should_fire = False
            else:
                should_fire = current_str != last_value
                if should_fire:
                    # Record what changed so the agent digests the actual event
                    # (event-driven v1: emit the event, not just "a trigger fired").
                    cfg["_last_event"] = f"Polled {url} → value changed from '{last_value}' to '{current_str}'"

        # Persist _last_value to DB so it survives restarts
        cfg["_last_value"] = current_str
        try:
            from sqlalchemy import update

            tid = await resolve_tenant_for_agent(trigger.agent_id)
            async with tenant_scoped_session(tid) as db:
                await db.execute(update(AgentTrigger).where(AgentTrigger.id == trigger.id).values(config=cfg))
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to persist poll _last_value for {trigger.name}: {e}")

        return should_fire

    except Exception as e:
        logger.warning(f"Poll failed for trigger {trigger.name}: {e}")
        return False


def _extract_json_path(data, path: str):
    """Simple JSONPath extraction: $.key.subkey → data['key']['subkey']."""
    if path == "$" or not path:
        return data
    parts = path.lstrip("$.").split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


async def _check_new_agent_messages(trigger: AgentTrigger) -> bool:
    """Check if there are new messages matching this trigger.

    Supports two modes:
    - from_agent_name: check for agent-to-agent messages
    - from_user_name: check for human user messages (Feishu/Slack/Discord)

    Stores the actual message content in trigger.config['_matched_message']
    so the invocation context can include it.
    """
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    cfg = trigger.config or {}
    from_agent_name = str(cfg.get("from_agent_name") or "").strip()
    from_agent_id = str(cfg.get("from_agent_id") or "").strip()
    from_user_name = str(cfg.get("from_user_name") or "").strip()
    from_user_identity = str(cfg.get("from_user_identity") or "").strip()
    from_channel = str(cfg.get("from_channel") or "").strip()
    reply_to_current_sender = bool(cfg.get("reply_to_current_sender"))

    if not any([from_agent_name, from_agent_id, from_user_name, from_user_identity, reply_to_current_sender]):
        return False

    since = trigger.last_fired_at or trigger.created_at
    if trigger.fire_count == 0 and not trigger.last_fired_at:
        since_ts_str = cfg.get("_since_ts")
        if since_ts_str:
            try:
                since = datetime.fromisoformat(since_ts_str)
            except Exception:
                since = trigger.created_at

    try:
        tid = await resolve_tenant_for_agent(trigger.agent_id)
        async with tenant_scoped_session(tid) as db:
            from sqlalchemy import cast as sa_cast, String as SaString, or_
            from app.models.agent import Agent as AgentModel
            from app.models.participant import Participant
            from app.models.user import User

            def _record_match(msg: ChatMessage, source_label: str) -> bool:
                cfg["_matched_message"] = msg.content or ""
                cfg["_matched_from"] = source_label
                msg_id = getattr(msg, "id", None)
                if msg_id:
                    cfg["_matched_event_key"] = f"chat_message:{msg_id}"
                return True

            def _session_sender_identity(session_obj: ChatSession) -> str:
                from app.services.channel_delivery_service import ChannelDeliveryService
                from app.services.pending_reply_service import sender_identity_from_external_conv_id

                sender_identity = sender_identity_from_external_conv_id(
                    getattr(session_obj, "external_conv_id", "") or ""
                )
                if sender_identity:
                    return sender_identity
                return ChannelDeliveryService.identity_from_delivery_target(
                    getattr(session_obj, "delivery_target_json", None)
                )

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == trigger.agent_id))
            current_agent = agent_r.scalar_one_or_none()
            current_tenant_id = getattr(current_agent, "tenant_id", None)

            if reply_to_current_sender:
                reply_ctx = getattr(trigger, "reply_context", None) or {}
                session_id = str(reply_ctx.get("session_id") or "")
                if not session_id:
                    return False
                result = await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.agent_id == trigger.agent_id,
                        ChatMessage.conversation_id == session_id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                return _record_match(
                    msg, reply_ctx.get("user_label") or reply_ctx.get("sender_identity") or "current_sender"
                )

            if from_user_identity:
                result = await db.execute(
                    select(ChatMessage, ChatSession)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(20)
                )
                for msg, session_obj in result.all():
                    if from_channel and getattr(session_obj, "source_channel", "") != from_channel:
                        continue
                    if _session_sender_identity(session_obj) != from_user_identity:
                        continue
                    source_label = from_user_identity
                    delivery_target = getattr(session_obj, "delivery_target_json", None) or {}
                    source_label = delivery_target.get("user_label") or delivery_target.get("user_id") or source_label
                    return _record_match(msg, source_label)
                return False

            if from_agent_id or from_agent_name:
                source_agent = None
                if from_agent_id:
                    try:
                        source_agent_id = uuid.UUID(from_agent_id)
                    except ValueError:
                        return False
                    agent_r = await db.execute(
                        select(AgentModel).where(
                            AgentModel.id == source_agent_id,
                            AgentModel.tenant_id == current_tenant_id,
                        )
                    )
                    source_agent = agent_r.scalar_one_or_none()
                else:
                    agent_r = await db.execute(
                        select(AgentModel).where(
                            AgentModel.tenant_id == current_tenant_id,
                            AgentModel.name.ilike(from_agent_name),
                        )
                    )
                    matches = agent_r.scalars().all()
                    if len(matches) != 1:
                        if len(matches) > 1:
                            cfg["_match_error"] = f"Ambiguous from_agent_name: {from_agent_name}"
                        return False
                    source_agent = matches[0]

                if not source_agent:
                    return False

                result = await db.execute(
                    select(Participant.id).where(
                        Participant.type == "agent",
                        Participant.ref_id == source_agent.id,
                    )
                )
                from_participant = result.scalar_one_or_none()
                if not from_participant:
                    return False

                result = await db.execute(
                    select(ChatMessage)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatMessage.participant_id == from_participant,
                        ChatMessage.created_at > since,
                        ChatMessage.role == "assistant",
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                return _record_match(msg, getattr(source_agent, "name", from_agent_name or from_agent_id))

            if from_user_name:
                user_r = await db.execute(
                    select(User)
                    .where(
                        User.tenant_id == current_tenant_id,
                        or_(
                            User.display_name.ilike(f"%{from_user_name}%"),
                            User.username.ilike(f"%{from_user_name}%"),
                        ),
                    )
                    .limit(1)
                )
                target_user = user_r.scalars().first()
                if not target_user:
                    return False

                result = await db.execute(
                    select(ChatMessage)
                    .join(ChatSession, ChatMessage.conversation_id == sa_cast(ChatSession.id, SaString))
                    .where(
                        ChatSession.agent_id == trigger.agent_id,
                        ChatSession.user_id == target_user.id,
                        ChatMessage.role == "user",
                        ChatMessage.created_at > since,
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                msg = result.scalar_one_or_none()
                if not msg:
                    return False
                return _record_match(msg, from_user_name)

    except Exception as e:
        logger.warning(f"on_message check failed for trigger {trigger.name}: {e}")
        return False

    return False


def _default_trigger_event_key(trigger: AgentTrigger, now: datetime, evaluation: dict | bool | None = None) -> str:
    cfg = trigger.config or {}
    if isinstance(evaluation, dict) and evaluation.get("event_key"):
        return str(evaluation["event_key"])
    if cfg.get("_matched_event_key"):
        return str(cfg["_matched_event_key"])
    if trigger.type == "once":
        return f"once:{trigger.id}:{cfg.get('at') or trigger.created_at.isoformat()}"
    if trigger.type == "cron":
        return f"cron:{trigger.id}:{now.strftime('%Y%m%d%H%M')}"
    if trigger.type == "interval":
        minutes = max(int(cfg.get("minutes", 30) or 30), 1)
        return f"interval:{trigger.id}:{int(now.timestamp()) // (minutes * 60)}"
    if trigger.type == "poll":
        current_value = str(cfg.get("_last_value") or "")
        return f"poll:{trigger.id}:{hashlib.sha256(current_value.encode('utf-8')).hexdigest()[:16]}"
    if trigger.type == "webhook":
        payload = str(cfg.get("_webhook_payload") or "")
        payload_hash = (
            hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16] if payload else now.strftime("%Y%m%d%H%M%S")
        )
        return f"webhook:{trigger.id}:{cfg.get('_webhook_event_key') or payload_hash}"
    return f"{trigger.type}:{trigger.id}:{now.strftime('%Y%m%d%H%M%S')}"


async def _acquire_trigger_fire_lease(
    trigger_id: uuid.UUID,
    event_key: str,
    *,
    ttl_seconds: int = _TRIGGER_FIRE_LEASE_TTL_SECONDS,
) -> bool:
    lease_key = f"trigger_fire:{trigger_id}:{hashlib.sha256(event_key.encode('utf-8')).hexdigest()[:24]}"
    try:
        redis = await get_redis()
        acquired = await redis.set(lease_key, "1", ex=ttl_seconds, nx=True)
        return bool(acquired)
    except Exception as exc:
        logger.warning("[TriggerDaemon] Failed to acquire trigger fire lease for {}: {}", trigger_id, exc)
        return False


async def _preflight_trigger_group(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    now: datetime,
) -> tuple[bool, str | None, str, dict]:
    """Run P5 wake gate before mutating trigger fire counters."""
    try:
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                return False, "agent_not_found", f"Agent {agent_id} was not found.", {}
            model, model_metadata, model_error = await select_trigger_model(db, agent, triggers)
            if model_error:
                return (
                    False,
                    model_error,
                    f"Trigger wake skipped by preflight: {model_error}.",
                    model_metadata,
                )
            preflight = await evaluate_trigger_preflight(db, agent=agent, model=model, triggers=triggers, now=now)
            metadata = {**model_metadata, **preflight.metadata}
            return preflight.ok, preflight.skip_reason, preflight.result_summary, metadata
    except Exception as exc:
        logger.warning("[TriggerDaemon] Trigger preflight failed for {}: {}", agent_id, exc)
        return False, "preflight_failed", f"Trigger preflight failed: {exc}", {"error": str(exc)}


async def _record_trigger_success_state(agent_id: uuid.UUID, trigger_ids: list[uuid.UUID]) -> None:
    if not trigger_ids:
        return
    from app.services.trigger_failure_policy import reset_trigger_failure_policy

    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        changed = False
        for trigger_id in trigger_ids:
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger_id))
            trigger = result.scalar_one_or_none()
            if not trigger:
                continue
            cfg = dict(trigger.config or {})
            cfg.pop("_fire_inflight", None)
            if trigger.type == "webhook":
                cfg["_webhook_pending"] = False
                cfg["_webhook_payload"] = None
            trigger.config = cfg
            trigger.last_fired_at = datetime.now(timezone.utc)
            trigger.fire_count = int(trigger.fire_count or 0) + 1
            if trigger.type == "once":
                trigger.is_enabled = False
            if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
                trigger.is_enabled = False
            if reset_trigger_failure_policy(trigger):
                cfg = dict(trigger.config or {})
                cfg.pop("_fire_inflight", None)
                trigger.config = cfg
            changed = True
        if changed:
            await db.commit()


async def _mark_trigger_fire_started(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    *,
    now: datetime,
    runtime_task_id: str | uuid.UUID | None,
    event_keys: dict[uuid.UUID, str],
) -> None:
    if not triggers:
        return
    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        changed = False
        for detached in triggers:
            trigger_id = getattr(detached, "id", None)
            if trigger_id is None:
                continue
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger_id))
            trigger = result.scalar_one_or_none()
            if not trigger:
                continue
            cfg = dict(trigger.config or {})
            cfg["_fire_inflight"] = {
                "event_key": event_keys.get(trigger_id) or _default_trigger_event_key(trigger, now),
                "runtime_task_id": str(runtime_task_id) if runtime_task_id else None,
                "started_at": now.isoformat(),
            }
            trigger.config = cfg
            changed = True
        if changed:
            await db.commit()


async def _record_trigger_failure_state(agent_id: uuid.UUID, triggers: list[AgentTrigger], error: str) -> dict:
    from app.services.trigger_failure_policy import apply_trigger_failure_policy

    metadata: dict = {"failure_backoff": []}
    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        for detached in triggers:
            result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == getattr(detached, "id", None)))
            trigger = result.scalar_one_or_none()
            if not trigger:
                continue
            failure_meta = apply_trigger_failure_policy(trigger, error=error)
            cfg = dict(trigger.config or {})
            cfg.pop("_fire_inflight", None)
            trigger.config = cfg
            metadata["failure_backoff"].append(
                {
                    "trigger_id": str(trigger.id),
                    "trigger_name": trigger.name,
                    **failure_meta,
                }
            )
        await db.commit()
    return metadata


# ── Agent Invocation ────────────────────────────────────────────────


async def _load_confirmed_plan_for_trigger(db: Any, trigger: AgentTrigger, agent_id: uuid.UUID) -> Any | None:
    """E: load the confirmed plan a plan-born trigger fires for.

    The trigger carries ``config.plan_id`` (set at handoff — the load-bearing
    backstop contract). Fire-time reads the plan row fresh: single source of
    truth, no config bloat, and the plan body cannot go stale. Fails closed
    (returns ``None`` → wake by reason/focus exactly as before) when there is no
    ``plan_id``, the plan is missing, not ``confirmed``, or belongs to a
    different agent.
    """
    config = getattr(trigger, "config", None) or {}
    plan_id_raw = str(config.get("plan_id") or "").strip()
    if not plan_id_raw:
        return None
    try:
        plan_uuid = uuid.UUID(plan_id_raw)
    except (TypeError, ValueError):
        logger.warning(
            "[TriggerDaemon] trigger {} has invalid plan_id {!r} — waking without plan", trigger.name, plan_id_raw
        )
        return None

    from app.models.plan_request import AgentPlanRequest

    plan = (await db.execute(select(AgentPlanRequest).where(AgentPlanRequest.id == plan_uuid))).scalar_one_or_none()
    if plan is None:
        logger.warning(
            "[TriggerDaemon] trigger {} plan_id {} not found — waking without plan", trigger.name, plan_id_raw
        )
        return None
    if plan.status != "confirmed":
        logger.info(
            "[TriggerDaemon] trigger {} plan {} status={} (not confirmed) — waking without plan",
            trigger.name,
            plan_id_raw,
            plan.status,
        )
        return None
    if str(plan.agent_id) != str(agent_id):
        logger.warning(
            "[TriggerDaemon] trigger {} plan {} belongs to agent {} not {} — waking without plan",
            trigger.name,
            plan_id_raw,
            plan.agent_id,
            agent_id,
        )
        return None
    return plan


async def _build_confirmed_plan_context(db: Any, triggers: list[AgentTrigger], agent_id: uuid.UUID) -> str:
    """E: render the confirmed-plan marching orders for the plan-born triggers in
    this fire batch (deduped by plan id).

    Returns ``""`` when no fired trigger carries a confirmed plan — the ordinary
    reason/focus wake is then unchanged. The body is rendered by the shared
    :func:`build_plan_execution_instruction`, so a trigger wake executes the same
    confirmed plan the live chat would, with no wording drift.
    """
    seen: set[str] = set()
    blocks: list[str] = []
    for trigger in triggers:
        plan = await _load_confirmed_plan_for_trigger(db, trigger, agent_id)
        if plan is None:
            continue
        key = str(plan.id)
        if key in seen:
            continue
        seen.add(key)
        plan_json = plan.plan_json or {}
        blocks.append(
            build_plan_execution_instruction(
                plan_id=plan.id,
                plan_version=plan.plan_version,
                plan_markdown=str(plan_json.get("plan_markdown") or ""),
                objective=str(plan_json.get("objective") or ""),
                original_request=str(plan.original_request or ""),
                source="trigger",
            )
        )
    if not blocks:
        return ""
    return "\n\n===== 已确认的计划（本次唤醒按此执行）=====\n" + "\n\n".join(blocks)


def _format_trigger_event(trigger: AgentTrigger, cfg: dict) -> str:
    """The event payload for an event-driven trigger (poll/on_message/webhook).

    Exec/automation §2 — event-driven v1: the fired trigger must hand the agent
    the *actual event*, not just "a trigger fired". Returns "" for triggers with
    no captured event (e.g. a poll that fired with no recorded change)."""
    t = trigger.type
    if t == "on_message" and cfg.get("_matched_message"):
        return f'Message from {cfg.get("_matched_from", "?")}:\n"{cfg["_matched_message"]}"'
    if t == "webhook" and cfg.get("_webhook_payload"):
        payload_str = str(cfg["_webhook_payload"])
        return f"Webhook payload:\n{payload_str}"
    if t == "poll":
        # The change description recorded by _poll_check (falls back to last value).
        event = cfg.get("_last_event") or (f"Polled value: {cfg.get('_last_value')}" if cfg.get("_last_value") else "")
        return str(event)
    return ""


def _build_trigger_context(
    triggers: list[AgentTrigger],
    *,
    explicit_context: str = "",
    confirmed_plan_context: str = "",
) -> tuple[str, list[str]]:
    """Build the wake context fed to the agent as one user message (pure/testable).

    Three-bucket framing (exec/automation §2): event-driven triggers
    (poll/on_message/webhook) are presented as an *event to react to* with the
    event payload inline; scheduled triggers (cron/once/interval) are a plain
    scheduled run. Returns ``(context, trigger_names)``.
    """
    from app.services.agent_tool_domains.triggers import TRIGGER_BUCKET_EVENT_DRIVEN, trigger_bucket

    context_parts: list[str] = []
    trigger_names: list[str] = []
    for t in triggers:
        cfg = t.config or {}
        if trigger_bucket(t.type) == TRIGGER_BUCKET_EVENT_DRIVEN:
            part = f"Event from trigger: {t.name} ({t.type})\nReason: {t.reason}"
            event = _format_trigger_event(t, cfg)
            if event:
                part += f"\n{event}"
        else:
            part = f"Scheduled trigger: {t.name} ({t.type})\nReason: {t.reason}"

        # Reply channel context if the trigger was created from a channel.
        reply_ctx = getattr(t, "reply_context", None) or {}
        if reply_ctx.get("channel"):
            ch = reply_ctx["channel"]
            user_label = reply_ctx.get("user_label", "the requesting user")
            part += (
                f"\nReply Channel: {ch}"
                f"\nReply To: {user_label}"
                "\n→ MUST use send_channel_message / send_channel_file for this reply target."
            )

        context_parts.append(part)
        trigger_names.append(t.name)

    multiple = len(triggers) > 1
    trigger_context = (
        "===== Trigger Awakening Context =====\n"
        f"Source: trigger ({'multiple triggers fired simultaneously' if multiple else 'single trigger fired'})\n\n"
        + "\n---\n".join(context_parts)
        + (f"\n\nExplicit Context From configured refs:\n{explicit_context}" if explicit_context else "")
        + confirmed_plan_context
        + "\n\nExecute this trigger now. If you finish or get blocked, record the outcome "
        "with concrete evidence in your work ledger and memory."
        "\n==========================="
    )
    return trigger_context, trigger_names


# ── B3: same_session delivery (CC /loop "inject into current session") ───


def _trigger_delivery_mode(trigger: AgentTrigger) -> str:
    """Delivery semantics for a fired trigger (config-carried, JSONB).

    ``new_invocation`` (default) keeps the historical behaviour — each fire
    starts a fresh ``trigger_run`` child session. ``same_session`` routes the
    fire into an existing chat session as a new turn (CC first-gen ``/loop``
    cron "塞进当前 session 命令队列" semantics)."""
    mode = str((getattr(trigger, "config", None) or {}).get("delivery") or "new_invocation").strip()
    return mode if mode in {"new_invocation", "same_session"} else "new_invocation"


def _resolve_batch_same_session_target(triggers: list[AgentTrigger]) -> str | None:
    """Return the single source session id when the whole fired batch is a
    ``same_session`` delivery targeting one chat session, else ``None``.

    Conservative on purpose: a mixed batch (some ``same_session``, some normal)
    or a batch spanning multiple source sessions shares one trigger RuntimeTask,
    so splitting delivery would be ambiguous — those keep the normal
    new-invocation path. In practice a ``/loop`` trigger fires on its own
    interval cadence and reaches this alone."""
    if not triggers:
        return None
    session_ids: set[str] = set()
    for trigger in triggers:
        if _trigger_delivery_mode(trigger) != "same_session":
            return None
        sid = str((getattr(trigger, "config", None) or {}).get("source_session_id") or "").strip()
        if not sid:
            return None
        session_ids.add(sid)
    if len(session_ids) != 1:
        return None
    return next(iter(session_ids))


async def _deliver_batch_to_source_session(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    *,
    source_session_id: str,
    runtime_task_id: str | None,
) -> bool:
    """Deliver a fired ``same_session`` batch into its source chat session as a
    new turn instead of starting a fresh ``trigger_run`` invocation.

    Returns ``True`` when the fire was delivered (either started as a new turn
    or queued behind an active run — REPL-busy → queue, never concurrent).
    Returns ``False`` when the source session is gone / the agent is not
    runnable, so the caller falls back to the normal new-invocation path."""
    from fastapi import HTTPException

    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.web_chat_runtime import (
        WEB_CHAT_TURN_TASK_TYPE,
        ActiveWebChatRunExists,
        start_web_chat_run,
    )

    tid = await resolve_tenant_for_agent(agent_id)
    delivered_run_id: str | None = None
    queued = False
    async with tenant_scoped_session(tid) as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if agent is None or getattr(agent, "status", None) in ("expired", "stopped", "error", "archived"):
            logger.warning(
                "[TriggerDaemon] same_session delivery falling back to new invocation — agent {} not runnable",
                agent_id,
            )
            return False
        try:
            session_uuid = uuid.UUID(str(source_session_id))
        except (TypeError, ValueError):
            return False
        session = (
            await db.execute(
                select(ChatSession).where(ChatSession.id == session_uuid, ChatSession.agent_id == agent_id)
            )
        ).scalar_one_or_none()
        if session is None:
            logger.warning(
                "[TriggerDaemon] same_session source session {} missing — falling back to new invocation",
                source_session_id,
            )
            return False
        user = (await db.execute(select(User).where(User.id == session.user_id))).scalar_one_or_none()
        if user is None:
            logger.warning(
                "[TriggerDaemon] same_session source session {} has no resolvable owner — falling back",
                source_session_id,
            )
            return False

        content, trigger_names = _build_trigger_context(triggers)
        trigger_ids = [str(getattr(t, "id", "")) for t in triggers if getattr(t, "id", None)]
        try:
            payload = await start_web_chat_run(
                db=db,
                agent=agent,
                user=user,
                session=session,
                content=content,
                runtime_task_type=WEB_CHAT_TURN_TASK_TYPE,
                budget_interactive=False,
                extra_metadata={
                    "source": "loop_same_session",
                    "trigger_ids": trigger_ids,
                    "trigger_names": trigger_names,
                    "trigger_runtime_task_id": runtime_task_id,
                },
            )
            delivered_run_id = str(payload.get("run_id") or "") or None
        except ActiveWebChatRunExists as busy:
            # REPL-busy: the loop prompt is queued behind the active run — never
            # started concurrently (CC "only fires when the REPL is idle").
            queued = True
            delivered_run_id = str((getattr(busy, "run", None) or {}).get("run_id") or "") or None
        except HTTPException as exc:
            logger.warning(
                "[TriggerDaemon] same_session delivery rejected for session {}: {}",
                source_session_id,
                getattr(exc, "detail", exc),
            )
            return False

    # The fire is durable now: advance the interval clock / clear the inflight
    # marker and point the trigger RuntimeTask at the session it delivered into.
    try:
        await _record_trigger_success_state(agent_id, [getattr(t, "id") for t in triggers if getattr(t, "id", None)])
    except Exception as exc:  # noqa: BLE001 - state reset is best-effort.
        logger.debug("[TriggerDaemon] same_session success-state reset failed (non-fatal): {}", exc)
    await _update_trigger_runtime_task(
        runtime_task_id,
        status="completed",
        result_summary=(
            f"Loop delivered into session {source_session_id} "
            f"({'queued behind active run' if queued else 'started new turn'})."
        ),
        session_id=source_session_id,
        metadata_json={
            "delivery": "same_session",
            "delivered_run_id": delivered_run_id,
            "queued": queued,
            "source_session_id": source_session_id,
        },
    )
    return True


async def _invoke_agent_for_triggers(
    agent_id: uuid.UUID,
    triggers: list[AgentTrigger],
    *,
    runtime_task_id: str | None = None,
):
    """Invoke an agent with context from one or more fired triggers.

    Creates a Reflection Session and calls the LLM.
    """
    from app.api.websocket import call_llm
    from app.kernel.contracts import ExecutionIdentityRef
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant
    from app.services.audit_logger import write_audit_log
    from app.services.chat_transcript import append_session_event

    # §9 P8 (§6.2): triggers carrying a workflow_ref take the deterministic
    # engine branch; the rest continue down the existing prose-ReAct path.
    from app.services.workflow_trigger import fire_workflow_for_trigger

    react_triggers: list[AgentTrigger] = []
    for trigger in triggers:
        try:
            fire_result = await fire_workflow_for_trigger(
                agent_id=agent_id,
                trigger_config=trigger.config or {},
                trigger_name=trigger.name,
                webhook_payload=(trigger.config or {}).get("_webhook_payload"),
            )
        except Exception as exc:
            logger.error("[TriggerDaemon] workflow_ref fire failed for {}: {}", trigger.name, exc)
            fire_result = None
        if fire_result is None:
            react_triggers.append(trigger)
        else:
            logger.info(
                "[TriggerDaemon] trigger {} → workflow branch: {} (run={})",
                trigger.name,
                fire_result.status,
                fire_result.run_id,
            )
    if not react_triggers:
        await _skip_trigger_runtime_task(
            runtime_task_id,
            skip_reason="workflow_ref_handled",
            result_summary="All fired triggers were handled by the workflow engine branch.",
        )
        return
    triggers = react_triggers

    # B3: same_session delivery — a ``/loop``-style batch injects its prompt into
    # its source chat session as a new turn instead of starting a fresh
    # trigger_run child session. Falls through to the normal new-invocation path
    # when the source session is gone or the agent is not runnable.
    same_session_target = _resolve_batch_same_session_target(triggers)
    if same_session_target is not None:
        if await _deliver_batch_to_source_session(
            agent_id, triggers, source_session_id=same_session_target, runtime_task_id=runtime_task_id
        ):
            return

    admission = await admit_agent_runtime_tenant(
        agent_id,
        source="trigger",
        tenant_resolver=resolve_tenant_for_agent,
    )
    if not admission.ok:
        await _skip_trigger_runtime_task(
            runtime_task_id,
            skip_reason=admission.reason_code,
            result_summary=admission.message,
            metadata_json=admission.metadata(),
        )
        return
    tenant_id = admission.tenant_id
    agent_tenant_id: uuid.UUID | None = None
    agent_creator_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None

    try:
        async with tenant_scoped_session(tenant_id, require_tenant=True, source="trigger") as db:
            # Load agent
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning("[TriggerDaemon] Agent {} not found — skipping trigger", agent_id)
                await _skip_trigger_runtime_task(
                    runtime_task_id,
                    skip_reason="agent_not_found",
                    result_summary=f"Skipped trigger invocation because agent {agent_id} was not found.",
                )
                return
            if agent.status in ("expired", "stopped", "error", "archived"):
                await _skip_trigger_runtime_task(
                    runtime_task_id,
                    skip_reason="agent_not_runnable",
                    result_summary=f"Skipped trigger invocation because agent status is {agent.status}.",
                    metadata_json={"agent_status": agent.status},
                )
                return

            # Set execution identity — autonomous agent action
            from app.core.execution_context import set_agent_bot_identity

            set_agent_bot_identity(agent_id, agent.name, source="trigger")

            # Load LLM model. P5 supports per-job model pinning via trigger.config.model_id.
            model, model_metadata, model_error = await select_trigger_model(db, agent, triggers)
            if model_error:
                logger.warning("[TriggerDaemon] Model preflight failed for {}: {}", agent.name, model_error)
                await _skip_trigger_runtime_task(
                    runtime_task_id,
                    skip_reason=model_error,
                    result_summary=f"Skipped trigger invocation because model preflight failed: {model_error}.",
                    metadata_json=model_metadata,
                )
                return
            runtime_options = collect_trigger_runtime_options(triggers)

            explicit_context = ""
            if runtime_options.get("context_from"):
                explicit_context = await load_context_from(
                    db,
                    agent_id=agent_id,
                    context_refs=list(runtime_options.get("context_from") or []),
                )

            # E: a plan-born trigger fires for a user-confirmed plan — inject the
            # approved plan body as marching orders (read fresh from the plan row).
            confirmed_plan_context = await _build_confirmed_plan_context(db, triggers, agent_id)

            # Three-bucket framing + event payload (exec/automation §2) — pure helper.
            trigger_context, trigger_names = _build_trigger_context(
                triggers,
                explicit_context=explicit_context,
                confirmed_plan_context=confirmed_plan_context,
            )

            # Create a fresh Reflection Session for this wake.
            title = f"🤖 Reflection: {', '.join(trigger_names)}"
            run_uuid = _runtime_task_uuid_or_none(runtime_task_id)
            # Find agent's participant
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            session = ChatSession(
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
                source_channel="trigger",
                session_kind="trigger_run",
                actor_type="agent",
                runtime_source="trigger",
                visibility_scope="agent_owner",
                listed_surface="task_updates",
                runtime_task_id=run_uuid,
                title=title[:200],
            )
            db.add(session)
            await db.flush()
            session_id = session.id

            memory_messages = [{"role": "user", "content": trigger_context}]
            messages = list(memory_messages)

            trigger_metadata = {
                "source": "trigger",
                "trigger_ids": [str(getattr(t, "id", "")) for t in triggers],
                "trigger_names": trigger_names,
                "trigger_types": [str(getattr(t, "type", "")) for t in triggers],
                "runtime_task_id": runtime_task_id,
                "request_id": str(run_uuid) if run_uuid else None,
                "trace_id": f"trigger:{runtime_task_id}" if runtime_task_id else None,
                "semantic_memory_eligible": True,
            }
            trigger_wake_candidate = _build_trigger_wake_context_candidate(
                triggers,
                runtime_task_id=runtime_task_id,
                preflight_decision={"session_started": True},
            )
            trigger_metadata["trigger_wake_context_candidate"] = trigger_wake_candidate
            trigger_metadata["context_candidate_refs"] = [trigger_wake_candidate["context_candidate_ref"]]
            await append_session_event(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                run_id=run_uuid,
                actor_type="user",
                event_type="user_message",
                role="user",
                user_id=agent.creator_id,
                participant_id=agent_participant.id if agent_participant else None,
                content=trigger_context,
                source="trigger",
                visibility_scope="agent_owner",
                listed_surface="task_updates",
                metadata=trigger_metadata,
            )
            session.last_message_at = datetime.now(timezone.utc)
            await db.commit()
            if runtime_task_id:
                await update_runtime_task_record(
                    runtime_task_id,
                    status="running",
                    child_session_id=str(session_id),
                    result_summary="Trigger session started.",
                    metadata_json={
                        "session_id": str(session_id),
                        "session_bound": True,
                    },
                )
            # Cache participant ID + tenant for callbacks (they run after this
            # scoped session closes; stage-2b ChatMessage INSERTs must set
            # tenant_id and run under a tenant-scoped session).
            agent_participant_id = agent_participant.id if agent_participant else None
            agent_tenant_id = tenant_id
            agent_creator_id = agent.creator_id
            trigger_run_uuid = run_uuid

        # Call LLM (outside the DB session to avoid long transactions)
        collected_content = []

        async def on_chunk(text):
            collected_content.append(text)

        # Persist tool calls into Reflection Session for Reflections visibility
        async def on_tool_call(data):
            try:
                async with tenant_scoped_session(agent_tenant_id) as _tc_db:
                    status = str(data.get("status") or "")
                    payload = {
                        "name": data.get("name", ""),
                        "args": data.get("args"),
                        "status": status,
                        "tool_call_id": data.get("tool_call_id"),
                        "step_id": data.get("step_id"),
                        "visibility": data.get("visibility") or "collapsed",
                        "started_at": data.get("started_at") or data.get("startedAt"),
                        "completed_at": data.get("completed_at") or data.get("completedAt"),
                        "duration_ms": data.get("duration_ms"),
                        "reasoning_content": data.get("reasoning_content"),
                        "reasoning_signature": data.get("reasoning_signature"),
                    }
                    if status in {"done", "completed", "failed"} or "result" in data:
                        payload["result"] = str(data.get("result", ""))
                    payload = {key: value for key, value in payload.items() if value is not None}
                    event_type = "tool_result" if status in {"done", "completed", "failed"} else "tool_call"
                    await append_session_event(
                        db=_tc_db,
                        agent_id=agent_id,
                        tenant_id=agent_tenant_id,
                        session_id=session_id,
                        run_id=trigger_run_uuid,
                        actor_type="tool",
                        event_type=event_type,
                        role="tool_call",
                        t0_role="tool",
                        user_id=agent_creator_id,
                        participant_id=agent_participant_id,
                        content=_json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                        source="trigger",
                        visibility_scope="agent_owner",
                        listed_surface="task_updates",
                        metadata={
                            "source": "trigger",
                            "tool_name": data.get("name", ""),
                            "status": status,
                            "tool_call_id": data.get("tool_call_id"),
                            "step_id": data.get("step_id"),
                            "duration_ms": data.get("duration_ms"),
                            "visibility": data.get("visibility") or "collapsed",
                            "runtime_task_id": runtime_task_id,
                        },
                    )
                    await _tc_db.commit()
            except Exception as e:
                logger.warning(f"Failed to persist tool call for trigger session: {e}")

        from app.services.channel_delivery_service import channel_delivery_target

        reply_target = next(
            (
                getattr(trigger, "reply_context", None)
                for trigger in triggers
                if getattr(trigger, "reply_context", None) and getattr(trigger, "reply_context", None).get("channel")
            ),
            None,
        )

        # Fallback: if reply_context is NULL (pre-unified-delivery triggers),
        # try to recover from the agent's most recent non-web ChatSession.
        if reply_target is None:
            try:
                reply_target = await _recover_reply_target_from_session(agent_id, triggers)
                if reply_target:
                    logger.info(
                        "[TriggerDaemon] Recovered reply_target from session for agent {}: channel={}",
                        agent_id,
                        reply_target.get("channel"),
                    )
            except Exception as _recover_err:
                logger.debug("[TriggerDaemon] reply_target recovery failed: {}", _recover_err)

        _delivery_token = None
        if reply_target:
            _delivery_token = channel_delivery_target.set(reply_target)
        system_prompt_suffix_parts = []
        if runtime_options.get("execution_class"):
            system_prompt_suffix_parts.append(f"Trigger execution class: {runtime_options['execution_class']}.")
        if runtime_options.get("workdir"):
            system_prompt_suffix_parts.append(
                f"Use this job workdir for generated files when applicable: {runtime_options['workdir']}."
            )
        if runtime_options.get("allowed_tool_names"):
            system_prompt_suffix_parts.append(
                "This job declares an explicit toolset; stay within it. If a needed capability is missing, use "
                "`tool_search` to discover matching deferred schemas when that tool is available, otherwise report "
                "the missing capability instead of assuming a loaded skill can expand tools."
            )
        system_prompt_suffix = "\n".join(system_prompt_suffix_parts)
        try:
            from app.runtime.session import SessionContext

            reply = await call_llm(
                model=model,
                messages=messages,
                agent_name=agent.name,
                role_description=agent.role_description or "",
                tenant_id=agent_tenant_id,
                agent_id=agent_id,
                user_id=agent.creator_id,
                on_chunk=on_chunk,
                on_tool_call=on_tool_call,
                session_id=str(session_id),
                memory_messages=memory_messages,
                execution_identity=ExecutionIdentityRef(
                    identity_type="agent_bot",
                    identity_id=agent_id,
                    label=f"Agent: {agent.name} (trigger)",
                ),
                allowed_tool_names=runtime_options.get("allowed_tool_names") or (),
                excluded_tool_names=runtime_options.get("excluded_tool_names") or (),
                system_prompt_suffix=system_prompt_suffix,
                # P1-1: trigger runs must NOT masquerade as live web chat. The
                # source drives the unattended Plan Mode lane and the T0 bucket
                # (trigger-*.md, not chat-*.md) — defaulting to "web" mis-routed
                # both and polluted T2 source weights.
                session_context=SessionContext(
                    session_id=str(session_id),
                    source="trigger",
                    channel="trigger",
                    metadata={
                        "tenant_id": str(agent_tenant_id) if agent_tenant_id else None,
                        "runtime_task_id": runtime_task_id,
                        "request_id": str(uuid.UUID(runtime_task_id)) if runtime_task_id else None,
                        "trace_id": f"trigger:{runtime_task_id}" if runtime_task_id else None,
                    },
                ),
                session_source="trigger",
                session_channel="trigger",
            )
        finally:
            if _delivery_token is not None:
                channel_delivery_target.reset(_delivery_token)

        final_reply = reply or "".join(collected_content)

        # Save assistant reply to Reflection session
        async with tenant_scoped_session(agent_tenant_id) as db:
            result = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id)
            )
            agent_participant = result.scalar_one_or_none()

            await append_session_event(
                db=db,
                agent_id=agent_id,
                tenant_id=agent_tenant_id,
                session_id=session_id,
                run_id=trigger_run_uuid,
                actor_type="assistant",
                event_type="assistant_message",
                role="assistant",
                user_id=agent_creator_id,
                participant_id=agent_participant.id if agent_participant else None,
                content=final_reply,
                source="trigger",
                visibility_scope="agent_owner",
                listed_surface="task_updates",
                metadata={
                    "source": "trigger",
                    "runtime_task_id": runtime_task_id,
                    "trigger_names": trigger_names,
                    "trigger_types": [str(getattr(t, "type", "")) for t in triggers],
                },
            )

            # NOTE: trigger state (last_fired_at, fire_count, auto-disable)
            # is already updated in _tick() BEFORE this task was launched,
            # to prevent race-condition duplicate fires.

            await db.commit()

        # Trigger results live in the Reflection Session only.
        # Do NOT push to user's chat WebSocket — it pollutes the conversation.
        # Users can view trigger results in the self-awareness tab.

        # Outcome metadata only. Durable learning enters the canonical T0 -> T2
        # path through the TRIGGER_END hook below.
        trigger_outcome = "unknown"
        trigger_score = None
        try:
            from app.services.heartbeat import _parse_heartbeat_outcome

            trigger_outcome, trigger_score = _parse_heartbeat_outcome(final_reply)
            logger.debug(
                "[TriggerDaemon] Trigger outcome parsed for {}: {} score={}",
                agent_id,
                trigger_outcome,
                trigger_score,
            )
        except Exception as _outcome_err:
            logger.debug("[TriggerDaemon] Trigger outcome parse failed (non-fatal): {}", _outcome_err)

        # Count trigger execution as a session for auto-dream gate
        try:
            from app.services.auto_dream import record_session_end
            from app.services.dream_runtime import enqueue_due_dream

            record_session_end(agent_id)
            if agent.tenant_id:
                queued = await enqueue_due_dream(
                    agent_id=agent_id,
                    tenant_id=agent.tenant_id,
                    source="trigger_end",
                )
                if queued is not None:
                    logger.info("[TriggerDaemon] Durable {} Dream queued for agent {}", queued.mode, agent_id)
        except Exception as _dream_err:
            logger.debug("[TriggerDaemon] Auto-dream check failed: {}", _dream_err)

        # Audit log
        await write_audit_log(
            "trigger_fired",
            {
                "agent_name": agent.name,
                "triggers": [{"name": t.name, "type": t.type} for t in triggers],
            },
            agent_id=agent_id,
        )

        output_artifact = None
        try:
            from app.config import get_settings
            from app.services.trigger_artifacts import write_trigger_output_artifact

            output_artifact = write_trigger_output_artifact(
                agent_data_dir=get_settings().AGENT_DATA_DIR,
                agent_id=agent_id,
                runtime_task_id=runtime_task_id,
                triggers=triggers,
                final_reply=final_reply or "",
                metadata={
                    **model_metadata,
                    **runtime_options,
                    "outcome": trigger_outcome,
                    "score": trigger_score,
                    "session_id": str(session_id),
                },
            )
        except Exception as _artifact_err:
            logger.debug("[TriggerDaemon] Trigger output artifact failed (non-fatal): {}", _artifact_err)

        try:
            await _record_trigger_success_state(agent_id, [getattr(trigger, "id") for trigger in triggers])
        except Exception as _success_state_err:
            logger.debug("[TriggerDaemon] Trigger success state reset failed (non-fatal): {}", _success_state_err)

        completion_summary = final_reply or "Trigger completed."
        await _update_trigger_runtime_task(
            runtime_task_id,
            status="completed",
            result_summary=completion_summary,
            session_id=str(session_id),
            metadata_json={
                **model_metadata,
                **runtime_options,
                "outcome": trigger_outcome,
                "score": trigger_score,
                "output_artifact": output_artifact,
            },
            completion_notification=_trigger_completion_notification(
                runtime_task_id=runtime_task_id,
                tenant_id=agent_tenant_id,
                agent_id=agent_id,
                user_id=agent_creator_id,
                session_id=session_id,
                status="completed",
                summary=completion_summary,
                trigger_names=trigger_names,
                trigger_types=[str(getattr(trigger, "type", "")) for trigger in triggers],
                artifacts=[output_artifact] if isinstance(output_artifact, dict) else [],
                metadata={"outcome": trigger_outcome, "score": trigger_score},
            ),
        )
        await _settle_trigger_runtime_budget(runtime_task_id, status="completed")

        logger.info(f"⚡ Triggers fired for {agent.name}: {[t.name for t in triggers]}")

        # Emit TRIGGER_END hook → T0 session ledger + extraction pipeline
        try:
            from app.runtime.hooks import HookEvent, emit_hook

            await emit_hook(
                HookEvent.TRIGGER_END,
                evidence_mode="independent",
                agent_id=agent_id,
                session_id=str(session_id),
                messages=[],
                source="trigger",
                metadata={
                    "tenant_id": str(agent_tenant_id) if agent_tenant_id else None,
                    "runtime_task_id": runtime_task_id,
                    "semantic_memory_eligible": True,
                    "trigger_name": trigger_names[0] if trigger_names else "unknown",
                    "trigger_type": triggers[0].type if triggers else "unknown",
                    "trigger_names": trigger_names,
                    "trigger_types": [t.type for t in triggers],
                    "status": "success",
                    "outcome": trigger_outcome,
                    "score": trigger_score,
                },
            )
        except Exception as _hook_err:
            logger.debug("[TriggerDaemon] TRIGGER_END hook failed (non-fatal): {}", _hook_err)

    except Exception as e:
        logger.error(f"Failed to invoke agent {agent_id} for triggers: {e}", exc_info=True)
        failure_summary = f"Trigger invocation failed: {str(e)}"
        failure_metadata = {"error": str(e)}
        try:
            failure_metadata.update(await _record_trigger_failure_state(agent_id, triggers, str(e)))
        except Exception as _failure_state_err:
            logger.debug("[TriggerDaemon] Trigger failure state update failed (non-fatal): {}", _failure_state_err)
        await _update_trigger_runtime_task(
            runtime_task_id,
            status="failed",
            result_summary=failure_summary,
            metadata_json=failure_metadata,
            session_id=str(session_id) if session_id else None,
            completion_notification=_trigger_completion_notification(
                runtime_task_id=runtime_task_id,
                tenant_id=agent_tenant_id,
                agent_id=agent_id,
                user_id=agent_creator_id,
                session_id=session_id,
                status="failed",
                summary=failure_summary,
                trigger_names=[str(getattr(trigger, "name", "")) for trigger in triggers],
                trigger_types=[str(getattr(trigger, "type", "")) for trigger in triggers],
                metadata=failure_metadata,
            ),
        )
        await _settle_trigger_runtime_budget(runtime_task_id, status="failed")


async def _settle_trigger_runtime_budget(runtime_task_id: str | None, *, status: str) -> None:
    if not runtime_task_id:
        return
    try:
        record = await get_runtime_task_record(str(runtime_task_id))
        if not record:
            return
        budget_run_id = _runtime_task_uuid_or_none(record.get("budget_run_id"))
        reservation_key = str(record.get("budget_reservation_key") or "").strip()
        if budget_run_id is None or not reservation_key:
            return
        reservation = RuntimeBudgetReservation(
            budget_run_id=budget_run_id,
            reservation_key=reservation_key,
            background_tasks=1,
            runtime_task_id=uuid.UUID(str(runtime_task_id)),
            metadata={"work_type": "trigger", "status": status},
        )
        await ExecutionAdmission().settle(
            ExecutionAdmissionDecision(
                status="admitted",
                reservation=reservation,
                budget_run_id=budget_run_id,
            ),
            actual_background_tasks=1,
            reason=f"trigger_{status}",
            runtime_task_id=uuid.UUID(str(runtime_task_id)),
        )
        await update_runtime_task_record(str(runtime_task_id), budget_admission_status="settled")
    except Exception:
        logger.exception("[TriggerDaemon] Failed to settle trigger budget for {}", runtime_task_id)


async def execute_claimed_trigger_runtime_task(task_id: uuid.UUID | str) -> bool:
    """Resume a budget-approved trigger intent from the shared RuntimeTask worker."""

    task_uuid = uuid.UUID(str(task_id))
    record = await get_runtime_task_record(task_uuid.hex)
    if not record or record.get("task_type") != "trigger":
        return False
    metadata = dict(record.get("metadata") or {})
    raw_trigger_ids = list(metadata.get("trigger_ids") or [])
    trigger_ids = [value for value in (_runtime_task_uuid_or_none(item) for item in raw_trigger_ids) if value]
    agent_id = _runtime_task_uuid_or_none(record.get("parent_agent_id") or metadata.get("agent_id"))
    tenant_id = _runtime_task_uuid_or_none(record.get("tenant_id"))
    if not trigger_ids or agent_id is None or tenant_id is None:
        await update_runtime_task_record(
            task_uuid.hex,
            status="failed",
            result_summary="Approved trigger intent is missing tenant, agent, or trigger identity.",
        )
        await _settle_trigger_runtime_budget(task_uuid.hex, status="failed")
        return False
    async with tenant_scoped_session(
        tenant_id,
        require_tenant=True,
        source="approved_trigger_runtime_task",
    ) as db:
        triggers = list(
            (
                await db.execute(
                    select(AgentTrigger).where(
                        AgentTrigger.id.in_(trigger_ids),
                        AgentTrigger.agent_id == agent_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    if not triggers:
        await update_runtime_task_record(
            task_uuid.hex,
            status="skipped",
            result_summary="Approved trigger intent no longer has active trigger definitions.",
        )
        await _settle_trigger_runtime_budget(task_uuid.hex, status="skipped")
        return False
    event_keys = {
        trigger_id: str(value)
        for raw_id, value in dict(metadata.get("fire_event_keys") or {}).items()
        if (trigger_id := _runtime_task_uuid_or_none(raw_id)) is not None and value
    }
    await _mark_trigger_fire_started(
        agent_id,
        triggers,
        now=datetime.now(timezone.utc),
        runtime_task_id=task_uuid.hex,
        event_keys=event_keys,
    )
    await _invoke_agent_for_triggers(agent_id, triggers, runtime_task_id=task_uuid.hex)
    return True


async def fire_trigger_once_now(
    agent_id: uuid.UUID,
    trigger_id: str | uuid.UUID,
    *,
    event_key_prefix: str = "loop_immediate",
) -> dict[str, Any]:
    """Fire a single trigger immediately through the normal daemon fire path.

    B1 (CC ``loop.ts:67`` "run once immediately"): reuses the exact
    preflight → RuntimeTask admission → mark-fired → invoke sequence the tick
    loop runs, skipping only the schedule-timing check (we *want* it now). It
    never bypasses preflight / governance / budget admission, so an immediate
    ``/loop`` run is subject to the same wake gate as a scheduled fire. The
    heavy agent invocation is spawned as a background task; this returns as soon
    as the fire is admitted and marked in-flight so callers get an observable
    ``runtime_task_id``."""
    now = datetime.now(timezone.utc)
    try:
        trigger_uuid = uuid.UUID(str(trigger_id))
    except (TypeError, ValueError):
        return {"fired": False, "reason": "invalid_trigger_id", "runtime_task_id": None}

    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        trigger = (
            await db.execute(
                select(AgentTrigger).where(AgentTrigger.id == trigger_uuid, AgentTrigger.agent_id == agent_id)
            )
        ).scalar_one_or_none()
    if trigger is None:
        return {"fired": False, "reason": "trigger_not_found", "runtime_task_id": None}

    event_key = f"{event_key_prefix}:{trigger.id}:{int(now.timestamp())}"
    # Best-effort dedup marker; the in-flight config guard below is the real
    # protection against a same-tick daemon double-fire, so proceed regardless.
    try:
        await _acquire_trigger_fire_lease(trigger.id, event_key)
    except Exception as exc:  # noqa: BLE001 - lease is advisory for the immediate run.
        logger.debug("[TriggerDaemon] immediate fire lease non-fatal error for {}: {}", trigger.id, exc)

    preflight_ok, skip_reason, skip_summary, preflight_metadata = await _preflight_trigger_group(
        agent_id, [trigger], now
    )
    runtime_task_id = await _create_trigger_runtime_task(
        agent_id,
        [trigger],
        metadata_json={
            **preflight_metadata,
            "preflight_allowed": preflight_ok,
            "immediate_fire": True,
            "fire_event_keys": {str(trigger.id): event_key},
        },
    )
    if runtime_task_id is None:
        return {"fired": False, "reason": "runtime_ledger_unavailable", "runtime_task_id": None}
    trigger_admission_status = getattr(runtime_task_id, "admission_status", "admitted")
    runtime_task_id = str(runtime_task_id)
    if not preflight_ok:
        await _skip_trigger_runtime_task(
            runtime_task_id,
            skip_reason=skip_reason or "preflight_blocked",
            result_summary=skip_summary or "Immediate trigger fire skipped by preflight.",
            metadata_json=preflight_metadata,
        )
        return {"fired": False, "reason": skip_reason or "preflight_blocked", "runtime_task_id": runtime_task_id}
    if trigger_admission_status == "waiting_budget_approval":
        return {
            "fired": False,
            "reason": "waiting_budget_approval",
            "runtime_task_id": runtime_task_id,
        }

    await _mark_trigger_fire_started(
        agent_id,
        [trigger],
        now=now,
        runtime_task_id=runtime_task_id,
        event_keys={trigger.id: event_key},
    )
    asyncio.create_task(
        run_bounded("trigger", _invoke_agent_for_triggers(agent_id, [trigger], runtime_task_id=runtime_task_id))
    )
    return {"fired": True, "runtime_task_id": runtime_task_id}


# ── Main Tick Loop ──────────────────────────────────────────────────


async def _tick():
    """One daemon tick: evaluate all triggers, group by agent, invoke."""
    now = datetime.now(timezone.utc)

    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="trigger daemon tick — enumerate all enabled triggers across tenants"),
    ):
        result = await db.execute(
            select(AgentTrigger)
            .join(Agent, Agent.id == AgentTrigger.agent_id)
            .where(AgentTrigger.is_enabled, agent_lifecycle_active_clause())
        )
        all_triggers = result.scalars().all()

    if not all_triggers:
        logger.debug("[TriggerDaemon] No enabled triggers — tick skipped")
        return

    # Evaluate and group fired triggers by agent. A schedule is just a trigger,
    # so all of an agent's fired triggers collapse into one wake invocation.
    fired_by_group: dict[uuid.UUID, list[AgentTrigger]] = {}
    fire_event_keys: dict[uuid.UUID, str] = {}
    for trigger in all_triggers:
        # Auto-disable expired triggers — single-agent write, scope to its tenant.
        if trigger.expires_at and now >= trigger.expires_at:
            tid = await resolve_tenant_for_agent(trigger.agent_id)
            async with tenant_scoped_session(tid) as db:
                result = await db.execute(select(AgentTrigger).where(AgentTrigger.id == trigger.id))
                t = result.scalar_one_or_none()
                if t:
                    t.is_enabled = False
                    await db.commit()
            continue

        try:
            evaluation = await _evaluate_trigger(trigger, now)
            if not evaluation:
                continue
            event_key = _default_trigger_event_key(trigger, now, evaluation)
            if not await _acquire_trigger_fire_lease(trigger.id, event_key):
                logger.debug("[TriggerDaemon] Duplicate fire lease rejected for {} ({})", trigger.name, event_key)
                continue
            fire_event_keys[trigger.id] = event_key
            fired_by_group.setdefault(trigger.agent_id, []).append(trigger)
        except Exception as e:
            logger.warning(f"Error evaluating trigger {trigger.name}: {e}")

    # Invoke each agent for the trigger events that acquired a fire lease.
    # Per-agent try/except so one agent's failure doesn't block others (C-08)
    for agent_id, agent_triggers in fired_by_group.items():
        try:
            preflight_ok, skip_reason, skip_summary, preflight_metadata = await _preflight_trigger_group(
                agent_id,
                agent_triggers,
                now,
            )
            runtime_metadata = {
                **preflight_metadata,
                "preflight_allowed": preflight_ok,
                "fire_event_keys": {
                    str(getattr(trigger, "id", "")): fire_event_keys.get(getattr(trigger, "id", None))
                    for trigger in agent_triggers
                    if getattr(trigger, "id", None) in fire_event_keys
                },
            }
            runtime_task_id = await _create_trigger_runtime_task(
                agent_id,
                agent_triggers,
                metadata_json=runtime_metadata,
            )
            if runtime_task_id is None:
                logger.error(
                    "[TriggerDaemon] Trigger batch for agent {} was not executed because RuntimeTask persistence failed",
                    agent_id,
                )
                continue
            trigger_admission_status = getattr(runtime_task_id, "admission_status", "admitted")
            runtime_task_id = str(runtime_task_id)
            if not preflight_ok:
                await _skip_trigger_runtime_task(
                    runtime_task_id,
                    skip_reason=skip_reason or "preflight_blocked",
                    result_summary=skip_summary or "Trigger wake skipped by preflight.",
                    metadata_json=preflight_metadata,
                )
                continue
            if trigger_admission_status == "waiting_budget_approval":
                logger.info(
                    "[TriggerDaemon] Trigger batch for agent {} is waiting for runtime budget approval",
                    agent_id,
                )
                continue

            try:
                await _mark_trigger_fire_started(
                    agent_id,
                    agent_triggers,
                    now=now,
                    runtime_task_id=runtime_task_id,
                    event_keys=fire_event_keys,
                )
            except Exception as e:
                logger.warning(f"Failed to mark trigger fire in-flight: {e}")
                await _update_trigger_runtime_task(
                    runtime_task_id,
                    status="failed",
                    result_summary=f"Trigger fire could not be marked in-flight: {str(e)}",
                    metadata_json={"error": str(e), "stage": "mark_inflight"},
                )
                continue

            asyncio.create_task(
                run_bounded(
                    "trigger", _invoke_agent_for_triggers(agent_id, agent_triggers, runtime_task_id=runtime_task_id)
                )
            )
        except Exception as _agent_err:
            logger.warning("[TriggerDaemon] Failed to process agent {}: {}", agent_id, _agent_err)


async def start_trigger_daemon():
    """Start the background trigger daemon loop. Called from FastAPI startup.

    P1-W2-4: heartbeat + workspace sync moved to `evolution_daemon` so a
    slow heartbeat tick or workspace volume I/O can't push trigger schedule
    jitter past TICK_INTERVAL. This loop is now single-purpose.
    """
    from app.services.daemon_liveness import mark_daemon_error, mark_daemon_started, mark_daemon_tick

    mark_daemon_started("trigger_daemon")
    logger.info(f"⚡ Trigger Daemon started ({TICK_INTERVAL}s tick)")

    while True:
        try:
            await _tick()
            mark_daemon_tick("trigger_daemon")
        except Exception as e:
            mark_daemon_error("trigger_daemon", e)
            logger.error(f"Trigger Daemon error: {e}")
            import traceback

            traceback.print_exc()

        await asyncio.sleep(TICK_INTERVAL)
