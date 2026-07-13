"""System launcher for explicit Plan Mode authoring.

The explicit "no active agent run" entries — REST ``create``/``regenerate`` /
``revise`` and accepted channel Plan Mode recommendations — have no existing
live user message stream to drive the agent main loop. This launcher lets the
agent author the draft plan in Plan Mode without executing the planned work.

Given a freshly created **draft** plan, it pre-arms the Plan Mode runtime
(read-only policy + scheduler reminder + ``exit_plan_mode``), seeds the loop
with a guiding user prompt, and runs the agent. The agent explores read-only,
then calls ``exit_plan_mode``, which fills THAT draft and lands it
``awaiting_confirmation``. The id the entry point already returned to the
frontend never changes.

Fail-closed: any launcher / agent / ``exit_plan_mode`` failure leaves the plan
in a non-confirmable state (``planning_failed`` if the agent never submitted, or
the existing draft/planning row), and this launcher NEVER executes the planned
work — it only authors a confirmable plan. The caller re-loads the row to
observe the outcome.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, tenant_scoped_session
from app.kernel import ExecutionIdentityRef
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.plan_request import AgentPlanRequest
from app.models.runtime_task import RuntimeTask
from app.runtime.session import PlanModeState, SessionContext
from app.services.model_resolution import choose_runtime_model_pair, primary_model_unavailable
from app.services.plan_mode_file import provision_agent_plan_file_slot
from app.services.runtime_task_fence import current_runtime_task_fence, run_claimed_runtime_task
from app.services.tenant_resolver import resolve_tenant_for_agent, resolve_tenant_for_runtime_task

logger = logging.getLogger(__name__)

SYSTEM_PLAN_RUN_SOURCE = "system_plan_run"
SYSTEM_PLAN_RUN_TASK_TYPE = "system_plan_run"
SYSTEM_PLAN_RUN_LEASE_SECONDS = 120.0
SYSTEM_PLAN_RUN_RETRY_MAX_SECONDS = 300
#: Generous-but-bounded exploration budget for a system-initiated plan run. The
#: agent needs room to inspect read-only context before authoring, and Plan
#: Mode's read-only policy prevents side effects. Keep this aligned with the
#: main agent / CC AgentTool practical floor so planning is not starved.
SYSTEM_PLAN_RUN_MAX_ROUNDS = 200
_SYSTEM_PLAN_RUNTIME_NAMESPACE = uuid.UUID("fa73649a-1e69-4c72-83ef-f4fc8455524e")
_AUTHORABLE_PLAN_STATUSES = {"draft", "planning", "planning_failed"}
_COMPLETED_PLAN_STATUSES = {"awaiting_confirmation", "confirmed"}
_TERMINAL_PLAN_STATUSES = {"rejected", "superseded", "expired"}


class SystemPlanRuntimeAuthorityError(RuntimeError):
    """The durable authoring RuntimeTask does not match the Plan authority."""


@dataclass(frozen=True, slots=True)
class SystemPlanRuntimeClaim:
    task_id: UUID
    tenant_id: UUID
    agent_id: UUID
    root_user_id: UUID
    session_id: str
    claim_version: int
    worker_id: str
    root_runtime_task_id: UUID | None
    input_revision: int = 1


@dataclass(frozen=True, slots=True)
class ClaimedSystemPlanExecution:
    plan: AgentPlanRequest
    agent: Agent
    model: LLMModel | None
    fallback_model: LLMModel | None
    claim: SystemPlanRuntimeClaim
    seed_context: dict[str, Any]


def system_plan_runtime_task_id(plan_id: UUID | str) -> UUID:
    """Return the stable authoring-run id without aliasing the Plan row id."""

    return uuid.uuid5(_SYSTEM_PLAN_RUNTIME_NAMESPACE, str(UUID(str(plan_id))))


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _system_plan_session_id(plan: AgentPlanRequest) -> str:
    return str(plan.session_id or "").strip() or f"plan-{plan.id.hex}"


def _durable_seed_context(seed_context: dict[str, Any] | None) -> dict[str, Any]:
    """Return a complete JSON-safe copy suitable for durable worker recovery."""

    if not isinstance(seed_context, dict):
        return {}
    return json.loads(json.dumps(seed_context, ensure_ascii=False, sort_keys=True, default=str))


def _model_id_text(model_id: UUID | str | None) -> str | None:
    normalized = _uuid_or_none(model_id)
    return str(normalized) if normalized is not None else None


def _normalized_model_id_pair(
    model_id: UUID | str | None,
    fallback_model_id: UUID | str | None,
) -> tuple[str | None, str | None]:
    primary = _model_id_text(model_id)
    fallback = _model_id_text(fallback_model_id)
    if primary is None and fallback is not None:
        return fallback, None
    if fallback == primary:
        fallback = None
    return primary, fallback


def _configured_agent_model_ids(agent: Agent) -> tuple[UUID | None, UUID | None]:
    """Return only model ids explicitly configured on the current Agent."""

    primary_id = _uuid_or_none(agent.primary_model_id)
    fallback_id = _uuid_or_none(agent.fallback_model_id)
    if primary_id is None:
        return fallback_id, None
    if fallback_id == primary_id:
        fallback_id = None
    return primary_id, fallback_id


def _initialize_system_plan_input_metadata(
    *,
    seed_context: dict[str, Any],
    model_id: UUID | None,
    fallback_model_id: UUID | None,
) -> dict[str, Any]:
    model_id_value, fallback_model_id_value = _normalized_model_id_pair(model_id, fallback_model_id)
    return {
        "input_revision": 1,
        "input_revision_source": "explicit_launch",
        "seed_context": _durable_seed_context(seed_context),
        "model_id": model_id_value,
        "fallback_model_id": fallback_model_id_value,
        "original_seed_context": _durable_seed_context(seed_context),
        "original_model_id": model_id_value,
        "original_fallback_model_id": fallback_model_id_value,
        "previous_input_revisions": [],
    }


def _apply_explicit_system_plan_input_revision(
    metadata: dict[str, Any],
    *,
    seed_context: dict[str, Any],
    seed_context_provided: bool,
    model_id: UUID | None,
    fallback_model_id: UUID | None,
    now: datetime,
) -> dict[str, Any]:
    """Atomically advance explicit input while worker restart stays frozen."""

    updated = dict(metadata)
    current_seed = _durable_seed_context(updated.get("seed_context"))
    requested_seed = _durable_seed_context(seed_context) if seed_context_provided else current_seed
    current_model_id = _model_id_text(updated.get("model_id"))
    current_fallback_model_id = _model_id_text(updated.get("fallback_model_id"))
    requested_model_id, requested_fallback_model_id = _normalized_model_id_pair(model_id, fallback_model_id)
    current_revision = max(1, int(updated.get("input_revision") or 1))
    updated.setdefault("input_revision", current_revision)
    updated.setdefault("input_revision_source", "explicit_launch")
    updated.setdefault("original_seed_context", current_seed)
    updated.setdefault("original_model_id", current_model_id)
    updated.setdefault("original_fallback_model_id", current_fallback_model_id)
    updated.setdefault("previous_input_revisions", [])
    changed = (
        requested_seed != current_seed
        or requested_model_id != current_model_id
        or requested_fallback_model_id != current_fallback_model_id
    )
    if not changed:
        updated["seed_context"] = current_seed
        return updated

    next_revision = current_revision + 1
    previous = [dict(item) for item in updated.get("previous_input_revisions", []) if isinstance(item, dict)]
    previous.append(
        {
            "revision": current_revision,
            "seed_context": current_seed,
            "model_id": current_model_id,
            "fallback_model_id": current_fallback_model_id,
            "superseded_by_revision": next_revision,
            "superseded_at": now.isoformat(),
        }
    )
    updated.update(
        {
            "input_revision": next_revision,
            "input_revision_source": "explicit_regenerate",
            "seed_context": requested_seed,
            "model_id": requested_model_id,
            "fallback_model_id": requested_fallback_model_id,
            "previous_input_revisions": previous[-20:],
            "input_revision_updated_at": now.isoformat(),
        }
    )
    return updated


def _archive_terminal_reconciliation_for_regenerate(
    metadata: dict[str, Any],
    *,
    task: RuntimeTask,
    now: datetime,
) -> dict[str, Any]:
    """Preserve a completed non-retry operator decision before explicit reopen."""

    updated = dict(metadata)
    operation = updated.get("reconciliation_operation")
    if not isinstance(operation, dict):
        return updated
    if str(operation.get("status") or "") != "completed" or str(operation.get("action") or "") == "retry":
        return updated
    history = [
        dict(item) for item in updated.get("system_plan_terminal_reconciliation_history", []) if isinstance(item, dict)
    ]
    archived = _durable_seed_context(operation)
    archived.update(
        {
            "archived_at": now.isoformat(),
            "reopened_from_task_status": str(task.status),
        }
    )
    history.append(archived)
    updated["system_plan_terminal_reconciliation_history"] = history[-20:]
    updated.pop("reconciliation_operation", None)
    for key in (
        "needs_reconciliation",
        "reconciliation_status",
        "reconciliation_reason",
        "restart_resume_blocker",
    ):
        updated.pop(key, None)
    return updated


def _reopen_terminal_system_plan_task(
    task: RuntimeTask,
    metadata: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Rearm a stable task when the canonical Plan is still authorable."""

    history = [dict(item) for item in metadata.get("system_plan_reopen_history", []) if isinstance(item, dict)]
    history.append(
        {
            "previous_status": str(task.status),
            "previous_result_summary": str(task.result_summary or "")[:2_000],
            "previous_completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "reopened_at": now.isoformat(),
            "reason": "explicit_regenerate_authorable_plan",
        }
    )
    updated = dict(metadata)
    updated["system_plan_reopen_history"] = history[-20:]
    task.status = "pending"
    task.completed_at = None
    task.scheduled_at = None
    task.claim_expires_at = None
    task.result_summary = "System Plan authoring reopened by an explicit regenerate request."
    return updated


def _project_plan_runtime_status(
    plan: AgentPlanRequest,
    *,
    runtime_status: str,
    reason: str,
    task_id: UUID,
    input_revision: int,
    recorded_at: datetime,
    retry_at: datetime | None = None,
) -> None:
    metadata = dict(plan.metadata_json or {})
    metadata["system_plan_runtime"] = {
        "status": runtime_status,
        "reason": reason,
        "runtime_task_id": str(task_id),
        "input_revision": input_revision,
        "recorded_at": recorded_at.isoformat(),
        "retry_at": retry_at.isoformat() if retry_at is not None else None,
    }
    plan.metadata_json = metadata


def _reset_plan_for_queued_input_revision(
    plan: AgentPlanRequest,
    *,
    task_id: UUID,
    input_revision: int,
    recorded_at: datetime,
) -> None:
    """Remove an old worker's confirmable output before authoring the queued input."""

    plan.status = "draft"
    plan.plan_hash = None
    plan.plan_markdown_path = None
    plan.plan_json = {}
    plan.handoff_payload = None
    plan.handoff_status = None
    plan.confirmed_by_user_id = None
    plan.confirmed_at = None
    plan.rejected_by_user_id = None
    plan.rejected_at = None
    _project_plan_runtime_status(
        plan,
        runtime_status="resumable",
        reason="newer_input_revision_queued",
        task_id=task_id,
        input_revision=input_revision,
        recorded_at=recorded_at,
        retry_at=recorded_at,
    )


async def _enqueue_system_plan_runtime_notification(
    db: AsyncSession,
    *,
    task: RuntimeTask,
    plan: AgentPlanRequest,
    root_user_id: UUID | None,
    runtime_status: str,
    reason: str,
) -> None:
    """Persist a retryable user/session notification in the terminal transaction."""

    def record_delivery_state(status: str, state_reason: str, *, outbox_id: UUID | None = None) -> None:
        metadata = dict(task.metadata_json or {})
        metadata["system_plan_notification"] = {
            "status": status,
            "reason": state_reason,
            "terminal_status": runtime_status,
            "delivery_mode": "session_projection",
            "outbox_id": str(outbox_id) if outbox_id is not None else None,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        task.metadata_json = metadata

    session_id = _uuid_or_none(task.parent_session_id)
    if session_id is None:
        record_delivery_state("skipped", "parent_session_not_uuid")
        return
    if task.tenant_id is None or task.parent_agent_id is None:
        record_delivery_state("skipped", "runtime_authority_incomplete")
        return
    if root_user_id is None:
        record_delivery_state("skipped", "root_user_missing")
        return
    from app.models.chat_session import ChatSession
    from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification

    parent_session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.tenant_id == task.tenant_id,
                ChatSession.agent_id == task.parent_agent_id,
                ChatSession.user_id == root_user_id,
            )
        )
    ).scalar_one_or_none()
    if parent_session is None:
        record_delivery_state("skipped", "parent_session_not_deliverable")
        return
    summary = {
        "completed": f"Plan {plan.id} is ready for confirmation.",
        "resumable": f"Plan {plan.id} authoring was interrupted and will retry automatically.",
        "needs_reconciliation": f"Plan {plan.id} authoring is blocked pending recovery reconciliation.",
        "skipped": f"Plan {plan.id} authoring stopped because the Plan is {plan.status}.",
        "failed": f"Plan {plan.id} authoring failed before a confirmable Plan was produced; regenerate it.",
        "killed": f"Plan {plan.id} authoring was cancelled before completion.",
    }.get(runtime_status, str(task.result_summary or f"Plan {plan.id} authoring status: {runtime_status}."))
    rank = {
        "resumable": 50,
        "completed": 300,
        "skipped": 300,
        "failed": 300,
        "killed": 300,
        "needs_reconciliation": 400,
    }.get(runtime_status, 100)
    outbox_id = await enqueue_completion_notification(
        db,
        CompletionNotification(
            tenant_id=task.tenant_id,
            source_kind=SYSTEM_PLAN_RUN_SOURCE,
            source_run_id=str(task.id),
            parent_session_id=session_id,
            parent_agent_id=task.parent_agent_id,
            parent_user_id=root_user_id,
            terminal_status=runtime_status,
            task_type=SYSTEM_PLAN_RUN_TASK_TYPE,
            summary=summary,
            child_agent_name=task.child_agent_name,
            delivery_mode="session_projection",
            metadata={
                "plan_id": str(plan.id),
                "plan_status": str(plan.status),
                "runtime_task_id": str(task.id),
                "input_revision": int((task.metadata_json or {}).get("input_revision") or 1),
                "terminal_reason": reason,
                "session_source": str(plan.source or ""),
            },
            payload_rank=rank,
        ),
    )
    record_delivery_state("queued", "durable_outbox_enqueued", outbox_id=outbox_id)


async def _load_system_plan_notification_authority(
    db: AsyncSession,
    *,
    task: RuntimeTask,
) -> AgentPlanRequest | None:
    plan_id = _uuid_or_none((task.metadata_json or {}).get("plan_id"))
    if plan_id is None or task.tenant_id is None or task.parent_agent_id is None:
        return None
    return (
        await db.execute(
            select(AgentPlanRequest).where(
                AgentPlanRequest.id == plan_id,
                AgentPlanRequest.tenant_id == task.tenant_id,
                AgentPlanRequest.agent_id == task.parent_agent_id,
            )
        )
    ).scalar_one_or_none()


def _plan_terminal_runtime_status(plan_status: str) -> tuple[str, str] | None:
    if plan_status in _COMPLETED_PLAN_STATUSES:
        return "completed", "canonical_plan_completed"
    if plan_status in _TERMINAL_PLAN_STATUSES:
        return "skipped", "canonical_plan_terminal"
    return None


def _project_canonical_plan_terminal(
    task: RuntimeTask,
    *,
    plan_status: str,
    now: datetime,
    invalidate_running_claim: bool,
) -> None:
    terminal = _plan_terminal_runtime_status(plan_status)
    if terminal is None or task.status == "needs_reconciliation":
        return
    runtime_status, reason = terminal
    if task.status in {"completed", "failed", "killed", "skipped"}:
        return
    metadata = dict(task.metadata_json or {})
    previous_claim_version = int(task.claim_version or 0)
    previous_claim_worker_id = str(task.claimed_by or "") or None
    if invalidate_running_claim and task.status == "running":
        task.claim_version = previous_claim_version + 1
        task.claimed_by = "system-plan-terminalizer"
        metadata.update(
            {
                "claim_version": task.claim_version,
                "claimed_by": task.claimed_by,
                "claim_expires_at": None,
                "claim_fence": f"{task.id.hex}:{task.claim_version}",
                "system_plan_terminal_claim_invalidation": {
                    "previous_claim_version": previous_claim_version,
                    "previous_claim_worker_id": previous_claim_worker_id,
                    "invalidated_at": now.isoformat(),
                },
            }
        )
    task.status = runtime_status
    task.completed_at = now
    task.scheduled_at = None
    task.claim_expires_at = None
    task.result_summary = (
        "Plan authoring already completed in canonical Plan state."
        if runtime_status == "completed"
        else f"Plan authoring stopped because canonical Plan is {plan_status}."
    )
    metadata["system_plan_terminal"] = {
        "status": runtime_status,
        "plan_status": plan_status,
        "reason": reason,
        "claim_version": int(task.claim_version or 0),
        "claim_worker_id": str(task.claimed_by or "") or None,
        "recorded_at": now.isoformat(),
        "error_type": None,
    }
    task.metadata_json = metadata


def _build_launcher_user_prompt(plan: AgentPlanRequest, *, seed_context: dict[str, Any] | None) -> str:
    """Compose the single guiding user turn that drives the plan-mode loop.

    The agent is already constrained by the per-round Plan Mode reminder
    (read-only policy + ``exit_plan_mode`` contract injected by the kernel); this
    prompt only states the request and any structured seed the entry point
    carried (intercepted tool args, intent), and tells the agent to finish by
    calling ``exit_plan_mode``.
    """
    lines = [
        "You are in Plan Mode. Do NOT execute the requested work — plan it only.",
        "Inspect read-only context as needed and design a concrete approach. If a missing decision "
        "materially changes scope, risk, cost, deliverable, recipient, or cadence, ask the user with "
        "ask_user_question before finalizing — do not assume a default. Then submit the final plan by "
        "calling the exit_plan_mode tool. The exit_plan_mode card is the approval mechanism; do not "
        "ask in prose whether the plan is OK.",
        "",
        f"Request to plan (intent_type={plan.intent_type}):",
        (plan.original_request or "").strip() or "(no explicit request text; infer from the seed context below)",
    ]
    if seed_context:
        import json as _json

        lines.extend(
            [
                "",
                "Seed context (a starting hypothesis from the runtime — treat as clues, not the "
                "final answer; rewrite user-visible plan fields in your own words):",
                _json.dumps(seed_context, ensure_ascii=False, sort_keys=True, default=str, indent=2),
            ]
        )
    return "\n".join(lines)


def _assert_runtime_task_authority(
    task: RuntimeTask,
    *,
    plan: AgentPlanRequest,
    agent_id: UUID,
    root_user_id: UUID,
    session_id: str,
) -> None:
    expected_root_runtime_task_id = _uuid_or_none(plan.runtime_task_id)
    mismatches = {
        "task_type": task.task_type != SYSTEM_PLAN_RUN_TASK_TYPE,
        "tenant_id": task.tenant_id != plan.tenant_id,
        "parent_agent_id": task.parent_agent_id != agent_id,
        "child_agent_id": task.child_agent_id != agent_id,
        "parent_session_id": str(task.parent_session_id or "") != session_id,
        "child_session_id": str(task.child_session_id or "") != session_id,
        "root_user_id": _uuid_or_none(task.root_user_id) != root_user_id,
        "root_session_id": str(task.root_session_id or "") != session_id,
        "root_runtime_task_id": _uuid_or_none(task.root_runtime_task_id) != expected_root_runtime_task_id,
        "plan_id": str((task.metadata_json or {}).get("plan_id") or "") != str(plan.id),
    }
    invalid = sorted(name for name, mismatch in mismatches.items() if mismatch)
    if invalid:
        raise SystemPlanRuntimeAuthorityError(f"System Plan RuntimeTask authority mismatch: {', '.join(invalid)}")


async def _claim_system_plan_runtime_task(
    plan: AgentPlanRequest,
    *,
    agent: Agent,
    session_id: str,
    seed_context: dict[str, Any] | None,
    seed_context_provided: bool,
    model_id: UUID | None,
    fallback_model_id: UUID | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> SystemPlanRuntimeClaim | None:
    tenant_id = _uuid_or_none(plan.tenant_id)
    if tenant_id is None or tenant_id != _uuid_or_none(agent.tenant_id) or plan.agent_id != agent.id:
        raise SystemPlanRuntimeAuthorityError("System Plan tenant/agent authority is incomplete")

    task_id = system_plan_runtime_task_id(plan.id)
    now = datetime.now(timezone.utc)
    async with tenant_scoped_session(
        tenant_id,
        session_factory=session_factory,
        require_tenant=True,
        source="system_plan_runtime_task_claim",
    ) as db:
        canonical_plan = (
            await db.execute(
                select(AgentPlanRequest)
                .where(
                    AgentPlanRequest.id == plan.id,
                    AgentPlanRequest.tenant_id == tenant_id,
                    AgentPlanRequest.agent_id == agent.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if canonical_plan is None:
            raise SystemPlanRuntimeAuthorityError("System Plan row is outside the claimed authority")
        if str(canonical_plan.session_id or "").strip() and str(canonical_plan.session_id) != session_id:
            raise SystemPlanRuntimeAuthorityError("System Plan session authority changed before authoring")
        root_user_id = _uuid_or_none(canonical_plan.requested_by_user_id or agent.owner_user_id or agent.creator_id)
        if root_user_id is None:
            raise SystemPlanRuntimeAuthorityError("System Plan root user authority is incomplete")

        task = (
            await db.execute(
                select(RuntimeTask)
                .where(RuntimeTask.id == task_id, RuntimeTask.tenant_id == tenant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

        if (
            canonical_plan.status == "awaiting_confirmation"
            and task is not None
            and task.status == "running"
            and task.claim_expires_at is not None
            and task.claim_expires_at > now
        ):
            _assert_runtime_task_authority(
                task,
                plan=canonical_plan,
                agent_id=agent.id,
                root_user_id=root_user_id,
                session_id=session_id,
            )
            live_metadata = dict(task.metadata_json or {})
            previous_input_revision = max(1, int(live_metadata.get("input_revision") or 1))
            live_metadata = _apply_explicit_system_plan_input_revision(
                live_metadata,
                seed_context=_durable_seed_context(seed_context),
                seed_context_provided=seed_context_provided,
                model_id=model_id,
                fallback_model_id=fallback_model_id,
                now=now,
            )
            current_input_revision = max(1, int(live_metadata.get("input_revision") or 1))
            if current_input_revision > previous_input_revision:
                live_metadata.update(
                    {
                        "queued_input_revision": current_input_revision,
                        "queued_input_revision_at": now.isoformat(),
                        "queued_behind_claim_version": int(task.claim_version or 0),
                        "queued_behind_claim_worker_id": str(task.claimed_by or ""),
                    }
                )
                task.metadata_json = live_metadata
                _reset_plan_for_queued_input_revision(
                    canonical_plan,
                    task_id=task.id,
                    input_revision=current_input_revision,
                    recorded_at=now,
                )
                _project_plan_runtime_status(
                    canonical_plan,
                    runtime_status="running",
                    reason="newer_input_revision_queued",
                    task_id=task.id,
                    input_revision=current_input_revision,
                    recorded_at=now,
                )
                await db.flush()
                return None

        if canonical_plan.status not in _AUTHORABLE_PLAN_STATUSES:
            if task is not None:
                _assert_runtime_task_authority(
                    task,
                    plan=canonical_plan,
                    agent_id=agent.id,
                    root_user_id=root_user_id,
                    session_id=session_id,
                )
                _project_canonical_plan_terminal(
                    task,
                    plan_status=canonical_plan.status,
                    now=now,
                    invalidate_running_claim=True,
                )
                terminal_projection = (task.metadata_json or {}).get("system_plan_terminal")
                terminal_reason = (
                    str(terminal_projection.get("reason") or "canonical_plan_terminal")
                    if isinstance(terminal_projection, dict)
                    else "canonical_plan_terminal"
                )
                await _enqueue_system_plan_runtime_notification(
                    db,
                    task=task,
                    plan=canonical_plan,
                    root_user_id=root_user_id,
                    runtime_status=str(task.status),
                    reason=terminal_reason,
                )
            return None

        if task is None:
            task = RuntimeTask(
                id=task_id,
                task_type=SYSTEM_PLAN_RUN_TASK_TYPE,
                status="pending",
                tenant_id=tenant_id,
                parent_agent_id=agent.id,
                child_agent_id=agent.id,
                child_agent_name=agent.name,
                parent_session_id=session_id,
                child_session_id=session_id,
                root_user_id=root_user_id,
                root_session_id=session_id,
                root_runtime_task_id=_uuid_or_none(canonical_plan.runtime_task_id),
                prompt=canonical_plan.original_request,
                trace_id=f"system_plan_run:{task_id.hex}",
                root_idempotency_key=f"system_plan_run:{canonical_plan.id}",
                metadata_json={
                    "schema": "system_plan_runtime_task.v1",
                    "source": SYSTEM_PLAN_RUN_SOURCE,
                    "plan_id": str(canonical_plan.id),
                    "plan_version": int(canonical_plan.plan_version or 1),
                    "plan_authority_type": "agent_plan_request",
                    "plan_authority_id": str(canonical_plan.id),
                    "plan_root_runtime_task_id": str(canonical_plan.runtime_task_id)
                    if canonical_plan.runtime_task_id
                    else None,
                    "recovery_authority_type": "runtime_task",
                    "recovery_authority_id": str(task_id),
                    "system_plan_session_id": session_id,
                    "intent_type": canonical_plan.intent_type,
                    "resume_after_restart": True,
                    "resumable_system_plan": True,
                    "side_effect_risk": "read_only",
                    **_initialize_system_plan_input_metadata(
                        seed_context=_durable_seed_context(seed_context),
                        model_id=model_id,
                        fallback_model_id=fallback_model_id,
                    ),
                },
            )
            db.add(task)
        else:
            _assert_runtime_task_authority(
                task,
                plan=canonical_plan,
                agent_id=agent.id,
                root_user_id=root_user_id,
                session_id=session_id,
            )

        metadata = dict(task.metadata_json or {})
        metadata.setdefault("seed_context", _durable_seed_context(seed_context))
        metadata.setdefault("model_id", _model_id_text(model_id))
        metadata.setdefault("fallback_model_id", _model_id_text(fallback_model_id))
        terminal_task_statuses = {"completed", "killed", "skipped", "failed"}
        if task.status in terminal_task_statuses:
            metadata = _archive_terminal_reconciliation_for_regenerate(metadata, task=task, now=now)
        if isinstance(metadata.get("reconciliation_operation"), dict):
            from app.services.runtime_reconciliation import (
                RuntimeReconciliationConflict,
                consume_completed_reconciliation_retry,
            )

            try:
                metadata = consume_completed_reconciliation_retry(
                    metadata,
                    next_claim_version=int(task.claim_version or 0) + 1,
                )
            except RuntimeReconciliationConflict as exc:
                raise SystemPlanRuntimeAuthorityError(
                    "System Plan cannot consume a non-retry reconciliation operation"
                ) from exc
        if task.status == "needs_reconciliation" or metadata.get("needs_reconciliation") is True:
            return None
        if task.status in terminal_task_statuses:
            metadata = _reopen_terminal_system_plan_task(task, metadata, now=now)
        if task.status not in {"pending", "running", "resumable"}:
            raise SystemPlanRuntimeAuthorityError(
                f"System Plan RuntimeTask cannot be claimed from status {task.status!r}"
            )
        previous_input_revision = max(1, int(metadata.get("input_revision") or 1))
        metadata = _apply_explicit_system_plan_input_revision(
            metadata,
            seed_context=_durable_seed_context(seed_context),
            seed_context_provided=seed_context_provided,
            model_id=model_id,
            fallback_model_id=fallback_model_id,
            now=now,
        )
        current_input_revision = max(1, int(metadata.get("input_revision") or 1))
        if task.status == "running" and task.claim_expires_at is not None and task.claim_expires_at > now:
            if current_input_revision > previous_input_revision:
                metadata.update(
                    {
                        "queued_input_revision": current_input_revision,
                        "queued_input_revision_at": now.isoformat(),
                        "queued_behind_claim_version": int(task.claim_version or 0),
                        "queued_behind_claim_worker_id": str(task.claimed_by or ""),
                    }
                )
                task.metadata_json = metadata
                _project_plan_runtime_status(
                    canonical_plan,
                    runtime_status="running",
                    reason="newer_input_revision_queued",
                    task_id=task.id,
                    input_revision=current_input_revision,
                    recorded_at=now,
                )
                await db.flush()
            return None

        previous_claim = {
            "claim_version": int(task.claim_version or 0),
            "claim_worker_id": str(task.claimed_by or ""),
            "claim_expires_at": task.claim_expires_at.isoformat() if task.claim_expires_at else None,
        }
        task.claim_version = int(task.claim_version or 0) + 1
        task.claimed_by = f"system-plan:{task_id.hex}:{uuid.uuid4().hex}"
        task.claim_expires_at = now + timedelta(seconds=SYSTEM_PLAN_RUN_LEASE_SECONDS)
        task.attempt_count = int(task.attempt_count or 0) + 1
        task.status = "running"
        task.scheduled_at = None
        if task.started_at is None:
            task.started_at = now
        task.completed_at = None
        metadata.pop("queued_input_revision", None)
        metadata.pop("queued_input_revision_at", None)
        metadata.pop("queued_behind_claim_version", None)
        metadata.pop("queued_behind_claim_worker_id", None)
        claim_history = [item for item in metadata.get("claim_history", []) if isinstance(item, dict)]
        claim_history.append(
            {
                "claim_version": task.claim_version,
                "claim_worker_id": task.claimed_by,
                "claimed_at": now.isoformat(),
                "input_revision": current_input_revision,
                "previous_claim": previous_claim,
            }
        )
        metadata.update(
            {
                "schema": "system_plan_runtime_task.v1",
                "source": SYSTEM_PLAN_RUN_SOURCE,
                "plan_id": str(canonical_plan.id),
                "plan_version": int(canonical_plan.plan_version or 1),
                "plan_authority_type": "agent_plan_request",
                "plan_authority_id": str(canonical_plan.id),
                "plan_root_runtime_task_id": str(canonical_plan.runtime_task_id)
                if canonical_plan.runtime_task_id
                else None,
                "recovery_authority_type": "runtime_task",
                "recovery_authority_id": str(task.id),
                "system_plan_session_id": session_id,
                "intent_type": canonical_plan.intent_type,
                "seed_context": metadata.get("seed_context", {}),
                "model_id": metadata.get("model_id"),
                "fallback_model_id": metadata.get("fallback_model_id"),
                "resume_after_restart": True,
                "resumable_system_plan": True,
                "side_effect_risk": "read_only",
                "claim_history": claim_history[-20:],
                "claimed_by": task.claimed_by,
                "claimed_at": now.isoformat(),
                "claim_expires_at": task.claim_expires_at.isoformat(),
                "claim_version": task.claim_version,
                "claim_fence": f"{task.id.hex}:{task.claim_version}",
            }
        )
        task.metadata_json = metadata
        await db.flush()
        return SystemPlanRuntimeClaim(
            task_id=task.id,
            tenant_id=tenant_id,
            agent_id=agent.id,
            root_user_id=root_user_id,
            session_id=session_id,
            claim_version=task.claim_version,
            worker_id=str(task.claimed_by),
            root_runtime_task_id=_uuid_or_none(canonical_plan.runtime_task_id),
            input_revision=current_input_revision,
        )


def _requires_reconciliation(event: dict[str, Any]) -> bool:
    policy = event.get("runtime_failure_policy") if isinstance(event.get("runtime_failure_policy"), dict) else {}
    return event.get("status") == "needs_reconciliation" or policy.get("requires_reconciliation") is True


def _bind_final_recovery_cas(
    metadata: dict[str, Any],
    *,
    claim: SystemPlanRuntimeClaim,
    session_context: SessionContext,
) -> dict[str, Any]:
    from app.runtime.recovery_manifest import (
        inspect_recovery_manifest_checkpoint,
        reviewed_recovery_manifest_evidence,
    )

    updated = dict(metadata)
    targets = [dict(item) for item in updated.get("recovery_resolution_targets", []) if isinstance(item, dict)]
    target = next(
        (item for item in targets if _uuid_or_none(item.get("runtime_task_id")) == claim.task_id),
        None,
    )
    if target is None:
        target = {
            "agent_id": str(claim.agent_id),
            "session_id": claim.session_id,
            "runtime_task_id": str(claim.task_id),
            "source": "current_run",
        }
        targets.append(target)
    target.update(
        {
            "agent_id": str(claim.agent_id),
            "session_id": claim.session_id,
            "runtime_task_id": str(claim.task_id),
            "source": "current_run",
            "expected_claim_version": claim.claim_version,
            "expected_claim_worker_id": claim.worker_id,
        }
    )
    receipt = session_context.metadata.get("recovery_manifest_checkpoint_receipt")
    inspection: dict[str, Any] | None
    if isinstance(receipt, dict):
        inspection = {"state": "valid", "receipt": receipt}
    else:
        inspection = inspect_recovery_manifest_checkpoint(
            agent_id=claim.agent_id,
            tenant_id=claim.tenant_id,
            session_id=claim.session_id,
            runtime_task_id=claim.task_id,
        )
    target.update(reviewed_recovery_manifest_evidence(inspection))
    if isinstance(receipt, dict):
        if receipt.get("ref") is not None:
            target["expected_manifest_ref"] = receipt["ref"]
            updated["recovery_manifest_ref"] = receipt["ref"]
        if receipt.get("sha256") is not None:
            target["expected_sha256"] = receipt["sha256"]
            updated["recovery_manifest_sha256"] = receipt["sha256"]
    checkpoint_seq = session_context.metadata.get("recovery_checkpoint_seq")
    if checkpoint_seq is not None:
        target["expected_checkpoint_seq"] = checkpoint_seq
        updated["recovery_checkpoint_seq"] = checkpoint_seq
    updated["recovery_resolution_targets"] = targets
    return updated


async def _project_system_plan_recovery_event(
    event: dict[str, Any],
    *,
    claim: SystemPlanRuntimeClaim,
    session_context: SessionContext,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    if not _requires_reconciliation(event):
        return
    from app.services.runtime_reconciliation import mark_runtime_task_recovery_reconciliation

    receipt = session_context.metadata.get("recovery_manifest_checkpoint_receipt")
    async with tenant_scoped_session(
        claim.tenant_id,
        session_factory=session_factory,
        require_tenant=True,
        source="system_plan_recovery_event_projection",
    ) as db:
        row = (
            await db.execute(
                select(RuntimeTask)
                .where(RuntimeTask.id == claim.task_id, RuntimeTask.tenant_id == claim.tenant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None or row.child_agent_id != claim.agent_id:
            raise SystemPlanRuntimeAuthorityError("System Plan recovery event lost child RuntimeTask authority")
        if row.claim_version != claim.claim_version or str(row.claimed_by or "") != claim.worker_id:
            raise SystemPlanRuntimeAuthorityError("Stale System Plan claim cannot project a recovery event")
        row_metadata = dict(row.metadata_json or {})
        reconciliation_operation = row_metadata.get("reconciliation_operation")
        if isinstance(reconciliation_operation, dict):
            raise SystemPlanRuntimeAuthorityError(
                "System Plan recovery event cannot overwrite a reconciliation operation"
            )
        if row.status == "needs_reconciliation":
            event_identity = (
                str(event.get("tool_call_id") or ""),
                str(event.get("tool_name") or ""),
            )
            projected_identities = {
                (str(frame.get("tool_call_id") or ""), str(frame.get("tool_name") or ""))
                for frame in row_metadata.get("recovery_tool_frames", [])
                if isinstance(frame, dict)
            }
            if event_identity not in projected_identities:
                raise SystemPlanRuntimeAuthorityError(
                    "System Plan recovery event cannot append to a non-running RuntimeTask"
                )
            row.metadata_json = _bind_final_recovery_cas(
                row_metadata,
                claim=claim,
                session_context=session_context,
            )
            plan = await _load_system_plan_notification_authority(db, task=row)
            if plan is not None:
                await _enqueue_system_plan_runtime_notification(
                    db,
                    task=row,
                    plan=plan,
                    root_user_id=_uuid_or_none(row.root_user_id),
                    runtime_status="needs_reconciliation",
                    reason="recovery_reconciliation_required",
                )
            await db.flush()
            return
        if row.status != "running":
            raise SystemPlanRuntimeAuthorityError("System Plan recovery event requires a running RuntimeTask claim")
        view = await mark_runtime_task_recovery_reconciliation(
            db,
            task_id=claim.task_id,
            tenant_id=claim.tenant_id,
            agent_id=claim.agent_id,
            session_id=claim.session_id,
            event=event,
            recovery_manifest_receipt=receipt if isinstance(receipt, dict) else None,
            expected_status="running",
            expected_claim_version=claim.claim_version,
            expected_claim_worker_id=claim.worker_id,
        )
        if view is None:
            raise SystemPlanRuntimeAuthorityError("System Plan recovery event has no durable RuntimeTask consumer")
        row.metadata_json = _bind_final_recovery_cas(
            dict(row.metadata_json or {}),
            claim=claim,
            session_context=session_context,
        )
        plan = await _load_system_plan_notification_authority(db, task=row)
        if plan is not None:
            await _enqueue_system_plan_runtime_notification(
                db,
                task=row,
                plan=plan,
                root_user_id=_uuid_or_none(row.root_user_id),
                runtime_status="needs_reconciliation",
                reason="recovery_reconciliation_required",
            )
        await db.flush()


def _merge_unsafe_events(
    metadata: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    updated = dict(metadata)
    frames = [dict(item) for item in updated.get("recovery_tool_frames", []) if isinstance(item, dict)]
    by_key = {(str(frame.get("tool_call_id") or ""), str(frame.get("tool_name") or "")): frame for frame in frames}
    for event in events:
        frame = {
            "tool_name": str(event.get("tool_name") or ""),
            "tool_call_id": str(event.get("tool_call_id") or ""),
            "status": "needs_reconciliation",
            "event_type": str(event.get("event_type") or "recovery_reconciliation_required"),
            "reason": str(event.get("reason") or "tool_execution_outcome_unknown"),
        }
        by_key[(frame["tool_call_id"], frame["tool_name"])] = frame
    updated["recovery_tool_frames"] = list(by_key.values())[-50:]
    return updated


def _restored_unsafe_events(session_context: SessionContext) -> list[dict[str, Any]]:
    """Return unsafe frames recovered from a manifest without a new event.

    A process can die after the kernel checkpointed an unknown tool outcome but
    before the event reached PostgreSQL. On the next claim the kernel hydrates
    these typed frame lists. They are evidence, not permission to replay, and
    must be projected onto the same RuntimeTask before the run can terminate.
    """

    metadata = session_context.metadata if isinstance(session_context.metadata, dict) else {}
    restored: dict[tuple[str, str], dict[str, Any]] = {}
    for key in (
        "recovered_tool_frame_reconciliation",
        "recovered_pending_tool_frames",
        "pending_tool_frames",
    ):
        raw_frames = metadata.get(key)
        frames = [raw_frames] if isinstance(raw_frames, dict) else raw_frames
        if not isinstance(frames, list):
            continue
        for raw in frames:
            if not isinstance(raw, dict) or str(raw.get("status") or "") != "needs_reconciliation":
                continue
            tool_name = str(raw.get("tool_name") or "")
            tool_call_id = str(raw.get("tool_call_id") or "")
            if not tool_name or not tool_call_id:
                continue
            restored[(tool_call_id, tool_name)] = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "status": "needs_reconciliation",
                "event_type": str(raw.get("event_type") or "recovered_tool_frame_reconciliation"),
                "reason": str(raw.get("reason") or "tool_execution_outcome_unknown"),
            }
    return list(restored.values())[-50:]


async def _finalize_system_plan_runtime_task(
    plan: AgentPlanRequest,
    *,
    claim: SystemPlanRuntimeClaim,
    session_context: SessionContext,
    result: Any | None,
    error: Exception | None,
    unsafe_events: list[dict[str, Any]],
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> str:
    now = datetime.now(timezone.utc)
    async with tenant_scoped_session(
        claim.tenant_id,
        session_factory=session_factory,
        require_tenant=True,
        source="system_plan_runtime_task_finalize",
    ) as db:
        # Claim and finalize must acquire the two durable authority rows in the
        # same order.  Taking Plan first keeps a waiting re-claim from holding
        # Plan while the finalizer holds RuntimeTask, which is a real PG
        # deadlock under concurrent recovery.
        canonical_plan = (
            await db.execute(
                select(AgentPlanRequest)
                .where(
                    AgentPlanRequest.id == plan.id,
                    AgentPlanRequest.tenant_id == claim.tenant_id,
                    AgentPlanRequest.agent_id == claim.agent_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if canonical_plan is None:
            raise SystemPlanRuntimeAuthorityError("System Plan row disappeared before terminal projection")
        task = (
            await db.execute(
                select(RuntimeTask)
                .where(RuntimeTask.id == claim.task_id, RuntimeTask.tenant_id == claim.tenant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            raise SystemPlanRuntimeAuthorityError("System Plan RuntimeTask disappeared before terminal projection")
        if task.claim_version != claim.claim_version or str(task.claimed_by or "") != claim.worker_id:
            raise SystemPlanRuntimeAuthorityError("Stale System Plan claim cannot finalize the authoring run")
        metadata = dict(task.metadata_json or {})
        reconciliation_operation = metadata.get("reconciliation_operation")
        if isinstance(reconciliation_operation, dict):
            raise SystemPlanRuntimeAuthorityError("System Plan finalizer cannot overwrite a reconciliation operation")
        if task.status not in {"running", "needs_reconciliation"}:
            raise SystemPlanRuntimeAuthorityError(
                f"System Plan finalizer requires an active claim, not status {task.status!r}"
            )
        _assert_runtime_task_authority(
            task,
            plan=canonical_plan,
            agent_id=claim.agent_id,
            root_user_id=claim.root_user_id,
            session_id=claim.session_id,
        )

        current_input_revision = max(1, int(metadata.get("input_revision") or 1))
        if current_input_revision > claim.input_revision and canonical_plan.status in {
            *_AUTHORABLE_PLAN_STATUSES,
            "awaiting_confirmation",
        }:
            _reset_plan_for_queued_input_revision(
                canonical_plan,
                task_id=claim.task_id,
                input_revision=current_input_revision,
                recorded_at=now,
            )
            task.status = "resumable"
            task.completed_at = None
            task.scheduled_at = now
            task.claim_expires_at = None
            task.result_summary = "A newer explicit System Plan input revision is queued for authoring."
            metadata["system_plan_revision_requeue"] = {
                "superseded_claim_input_revision": claim.input_revision,
                "queued_input_revision": current_input_revision,
                "superseded_claim_version": claim.claim_version,
                "superseded_claim_worker_id": claim.worker_id,
                "recorded_at": now.isoformat(),
            }
            metadata["system_plan_terminal"] = {
                "status": "resumable",
                "plan_status": canonical_plan.status,
                "reason": "newer_input_revision_queued",
                "claim_version": claim.claim_version,
                "claim_worker_id": claim.worker_id,
                "recorded_at": now.isoformat(),
                "error_type": None,
            }
            task.metadata_json = metadata
            await _enqueue_system_plan_runtime_notification(
                db,
                task=task,
                plan=canonical_plan,
                root_user_id=claim.root_user_id,
                runtime_status="resumable",
                reason="newer_input_revision_queued",
            )
            await db.flush()
            return "resumable"

        recovered_unsafe_events = _restored_unsafe_events(session_context)
        all_unsafe_events = [*unsafe_events, *recovered_unsafe_events]
        blocked = (
            task.status == "needs_reconciliation"
            or metadata.get("needs_reconciliation") is True
            or session_context.metadata.get("recovery_reconciliation_blocked") is True
            or bool(all_unsafe_events)
        )
        terminal_reason = "safe_retry_scheduled"
        if blocked:
            metadata = _merge_unsafe_events(metadata, all_unsafe_events)
            metadata = _bind_final_recovery_cas(
                metadata,
                claim=claim,
                session_context=session_context,
            )
            metadata.update(
                {
                    "needs_reconciliation": True,
                    "reconciliation_status": "open",
                    "recovery_agent_id": str(claim.agent_id),
                    "recovery_session_id": claim.session_id,
                    "recovery_runtime_task_id": str(claim.task_id),
                }
            )
            task.status = "needs_reconciliation"
            task.completed_at = None
            task.scheduled_at = None
            task.result_summary = "System Plan authoring requires recovery reconciliation."
            terminal_reason = "recovery_reconciliation_required"
        elif canonical_plan.status in _COMPLETED_PLAN_STATUSES:
            task.status = "completed"
            task.completed_at = now
            task.scheduled_at = None
            task.result_summary = str(getattr(result, "content", "") or "Plan authoring completed.")[:20_000]
            terminal_reason = "canonical_plan_completed"
        elif canonical_plan.status in _TERMINAL_PLAN_STATUSES:
            task.status = "skipped"
            task.completed_at = now
            task.scheduled_at = None
            task.result_summary = f"Plan authoring stopped because canonical Plan is {canonical_plan.status}."
            terminal_reason = "canonical_plan_terminal"
        else:
            task.status = "resumable"
            task.completed_at = None
            retry_delay_seconds = min(
                SYSTEM_PLAN_RUN_RETRY_MAX_SECONDS,
                max(5, int(task.attempt_count or 1) * 10),
            )
            task.scheduled_at = now + timedelta(seconds=retry_delay_seconds)
            task.result_summary = (
                f"System Plan authoring interrupted safely: {type(error).__name__}: {error}"
                if error is not None
                else f"System Plan authoring returned without a confirmable plan (status={canonical_plan.status})."
            )[:20_000]
        task.claim_expires_at = None
        metadata["system_plan_terminal"] = {
            "status": task.status,
            "plan_status": canonical_plan.status,
            "reason": terminal_reason,
            "claim_version": claim.claim_version,
            "claim_worker_id": claim.worker_id,
            "recorded_at": now.isoformat(),
            "error_type": type(error).__name__ if error is not None else None,
        }
        task.metadata_json = metadata
        _project_plan_runtime_status(
            canonical_plan,
            runtime_status=task.status,
            reason=terminal_reason,
            task_id=task.id,
            input_revision=max(1, int(metadata.get("input_revision") or 1)),
            recorded_at=now,
            retry_at=task.scheduled_at if task.status == "resumable" else None,
        )
        await _enqueue_system_plan_runtime_notification(
            db,
            task=task,
            plan=canonical_plan,
            root_user_id=claim.root_user_id,
            runtime_status=task.status,
            reason=terminal_reason,
        )
        await db.flush()
        final_status = task.status
    return final_status


async def _resolve_agent_models(
    agent_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[LLMModel | None, LLMModel | None, Agent | None]:
    """Resolve (primary, fallback, agent) tenant-scoped for the plan-mode run."""
    tenant_id = await resolve_tenant_for_agent(agent_id, session_factory=session_factory)
    if tenant_id is None:
        return None, None, None
    async with tenant_scoped_session(
        tenant_id,
        session_factory=session_factory,
        require_tenant=True,
        source="system_plan_agent_model_resolution",
    ) as db:
        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            return None, None, None

        primary_model = None
        fallback_model = None
        primary_id = _uuid_or_none(agent.primary_model_id)
        fallback_id = _uuid_or_none(agent.fallback_model_id)
        if primary_id is not None:
            primary_model = (
                await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == primary_id,
                        LLMModel.tenant_id == agent.tenant_id,
                        LLMModel.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
        if fallback_id is not None and fallback_id != primary_id:
            fallback_model = (
                await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == fallback_id,
                        LLMModel.tenant_id == agent.tenant_id,
                        LLMModel.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
        if primary_model_unavailable(agent, primary_model):
            return None, None, agent
        model, explicit_fallback = choose_runtime_model_pair(primary_model, fallback_model, None)
        return model, explicit_fallback, agent


def _claim_from_persisted_system_plan(
    task: RuntimeTask,
    *,
    plan: AgentPlanRequest,
    agent: Agent,
) -> SystemPlanRuntimeClaim:
    session_id = str(task.parent_session_id or "").strip()
    root_user_id = _uuid_or_none(task.root_user_id)
    if not session_id or root_user_id is None:
        raise SystemPlanRuntimeAuthorityError("Claimed System Plan has incomplete session/root authority")
    _assert_runtime_task_authority(
        task,
        plan=plan,
        agent_id=agent.id,
        root_user_id=root_user_id,
        session_id=session_id,
    )
    metadata = dict(task.metadata_json or {})
    if task.status != "running":
        raise SystemPlanRuntimeAuthorityError("Claimed System Plan RuntimeTask is not running")
    if metadata.get("needs_reconciliation") is True or isinstance(metadata.get("reconciliation_operation"), dict):
        raise SystemPlanRuntimeAuthorityError("Claimed System Plan is blocked by reconciliation authority")
    claim_version = int(task.claim_version or 0)
    worker_id = str(task.claimed_by or "").strip()
    if claim_version <= 0 or not worker_id:
        raise SystemPlanRuntimeAuthorityError("Claimed System Plan has no durable worker fence")
    fence = current_runtime_task_fence()
    if fence is not None and (
        fence.task_id != task.id or fence.claim_version != claim_version or fence.worker_id != worker_id
    ):
        raise SystemPlanRuntimeAuthorityError("Claimed System Plan does not match the active worker fence")
    return SystemPlanRuntimeClaim(
        task_id=task.id,
        tenant_id=task.tenant_id,
        agent_id=agent.id,
        root_user_id=root_user_id,
        session_id=session_id,
        claim_version=claim_version,
        worker_id=worker_id,
        root_runtime_task_id=_uuid_or_none(task.root_runtime_task_id),
        input_revision=max(1, int(metadata.get("input_revision") or 1)),
    )


async def _load_claimed_system_plan_execution(
    task_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> ClaimedSystemPlanExecution:
    factory = session_factory or async_session
    tenant_id = await resolve_tenant_for_runtime_task(task_id, session_factory=factory)
    if tenant_id is None:
        raise SystemPlanRuntimeAuthorityError("Claimed System Plan RuntimeTask has no tenant authority")
    async with tenant_scoped_session(
        tenant_id,
        session_factory=factory,
        require_tenant=True,
        source="system_plan_worker_hydration",
    ) as db:
        task = (
            await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.id == task_id,
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type == SYSTEM_PLAN_RUN_TASK_TYPE,
                )
            )
        ).scalar_one_or_none()
        if task is None:
            raise SystemPlanRuntimeAuthorityError("Claimed System Plan RuntimeTask disappeared")
        metadata = dict(task.metadata_json or {})
        plan_id = _uuid_or_none(metadata.get("plan_id"))
        if plan_id is None:
            raise SystemPlanRuntimeAuthorityError("Claimed System Plan has no durable Plan authority id")
        plan = (
            await db.execute(
                select(AgentPlanRequest).where(
                    AgentPlanRequest.id == plan_id,
                    AgentPlanRequest.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if plan is None:
            raise SystemPlanRuntimeAuthorityError("Claimed System Plan authority row disappeared")
        agent = (
            await db.execute(
                select(Agent).where(
                    Agent.id == plan.agent_id,
                    Agent.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise SystemPlanRuntimeAuthorityError("Claimed System Plan Agent authority disappeared")
        claim = _claim_from_persisted_system_plan(task, plan=plan, agent=agent)

        persisted_model_id = _uuid_or_none(metadata.get("model_id"))
        persisted_fallback_model_id = _uuid_or_none(metadata.get("fallback_model_id"))
        current_primary_model_id = _uuid_or_none(agent.primary_model_id)
        current_fallback_model_id = _uuid_or_none(agent.fallback_model_id)
        candidate_ids = [
            candidate_id
            for index, candidate_id in enumerate((current_primary_model_id, current_fallback_model_id))
            if candidate_id is not None
            and candidate_id not in (current_primary_model_id, current_fallback_model_id)[:index]
        ]
        models_by_id: dict[UUID, LLMModel] = {}
        if candidate_ids:
            resolved_models = await db.execute(
                select(LLMModel).where(
                    LLMModel.id.in_(candidate_ids),
                    LLMModel.tenant_id == tenant_id,
                    LLMModel.enabled.is_(True),
                )
            )
            models_by_id = {candidate.id: candidate for candidate in resolved_models.scalars().all()}
        current_primary_model = models_by_id.get(current_primary_model_id)
        current_fallback_model = models_by_id.get(current_fallback_model_id)
        configured_primary_unavailable = primary_model_unavailable(agent, current_primary_model)
        if configured_primary_unavailable:
            model = None
            fallback_model = None
        else:
            model, fallback_model = choose_runtime_model_pair(
                current_primary_model,
                current_fallback_model,
                None,
            )
        selected_model_id = _uuid_or_none(getattr(model, "id", None))
        selected_fallback_model_id = _uuid_or_none(getattr(fallback_model, "id", None))
        resolution_status = (
            "primary_unavailable"
            if configured_primary_unavailable
            else ("resolved" if selected_model_id is not None else "unavailable")
        )

        metadata.setdefault("original_model_id", _model_id_text(persisted_model_id))
        metadata.setdefault("original_fallback_model_id", _model_id_text(persisted_fallback_model_id))
        selected_model_text = _model_id_text(selected_model_id)
        selected_fallback_text = _model_id_text(selected_fallback_model_id)
        model_selection_changed = (
            selected_model_id != persisted_model_id or selected_fallback_model_id != persisted_fallback_model_id
        )
        if model_selection_changed:
            lineage = [dict(item) for item in metadata.get("model_resume_history", []) if isinstance(item, dict)]
            transition = {
                "from_model_id": _model_id_text(persisted_model_id),
                "to_model_id": selected_model_text,
                "from_fallback_model_id": _model_id_text(persisted_fallback_model_id),
                "to_fallback_model_id": selected_fallback_text,
                "reason": (
                    "configured_primary_unavailable"
                    if configured_primary_unavailable
                    else (
                        "current_agent_model_preferred"
                        if selected_model_id is not None
                        else "current_agent_model_unconfigured"
                    )
                ),
                "input_revision": int(metadata.get("input_revision") or 1),
                "claim_version": claim.claim_version,
                "resumed_at": datetime.now(timezone.utc).isoformat(),
            }
            lineage_keys = (
                "from_model_id",
                "to_model_id",
                "from_fallback_model_id",
                "to_fallback_model_id",
                "input_revision",
            )
            if not lineage or {key: lineage[-1].get(key) for key in lineage_keys} != {
                key: transition.get(key) for key in lineage_keys
            }:
                lineage.append(transition)
            metadata["model_resume_history"] = lineage[-20:]
            metadata["resumed_model_id"] = selected_model_text
            metadata["model_id"] = selected_model_text
            metadata["fallback_model_id"] = selected_fallback_text
        metadata["model_resolution"] = {
            "status": resolution_status,
            "current_primary_model_id": _model_id_text(current_primary_model_id),
            "current_fallback_model_id": _model_id_text(current_fallback_model_id),
            "persisted_model_id": _model_id_text(persisted_model_id),
            "persisted_fallback_model_id": _model_id_text(persisted_fallback_model_id),
            "selected_model_id": selected_model_text,
            "selected_fallback_model_id": selected_fallback_text,
            "claim_version": claim.claim_version,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        task.metadata_json = metadata
        await db.flush()
        return ClaimedSystemPlanExecution(
            plan=plan,
            agent=agent,
            model=model,
            fallback_model=fallback_model,
            claim=claim,
            seed_context=_durable_seed_context(metadata.get("seed_context")),
        )


async def _quarantine_claimed_system_plan(
    task_id: UUID,
    *,
    reason: str,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    factory = session_factory or async_session
    tenant_id = await resolve_tenant_for_runtime_task(task_id, session_factory=factory)
    if tenant_id is None:
        return
    async with tenant_scoped_session(
        tenant_id,
        session_factory=factory,
        require_tenant=True,
        source="system_plan_worker_quarantine",
    ) as db:
        task = (
            await db.execute(
                select(RuntimeTask)
                .where(RuntimeTask.id == task_id, RuntimeTask.tenant_id == tenant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            return
        metadata = dict(task.metadata_json or {})
        if task.status != "running" or isinstance(metadata.get("reconciliation_operation"), dict):
            return
        fence = current_runtime_task_fence()
        if fence is not None and (
            fence.task_id != task.id
            or fence.claim_version != int(task.claim_version or 0)
            or fence.worker_id != str(task.claimed_by or "")
        ):
            return
        task.status = "needs_reconciliation"
        task.claim_expires_at = None
        task.scheduled_at = None
        task.completed_at = None
        task.result_summary = reason[:20_000]
        metadata.update(
            {
                "needs_reconciliation": True,
                "reconciliation_status": "open",
                "reconciliation_reason": "system_plan_authority_invalid",
                "restart_resume_blocker": reason[:2_000],
                "side_effect_risk": "unknown",
            }
        )
        task.metadata_json = metadata
        plan = await _load_system_plan_notification_authority(db, task=task)
        if plan is not None:
            await _enqueue_system_plan_runtime_notification(
                db,
                task=task,
                plan=plan,
                root_user_id=_uuid_or_none(task.root_user_id),
                runtime_status="needs_reconciliation",
                reason="system_plan_authority_invalid",
            )
        await db.flush()


async def _wake_system_plan_worker(*, reason: str, task_id: UUID) -> None:
    try:
        from app.services.runtime_task_worker import notify_runtime_task_worker

        await notify_runtime_task_worker(reason=reason, runtime_task_id=task_id)
    except Exception as exc:  # noqa: BLE001 - bounded polling remains the durable fallback.
        logger.warning(
            "system_plan_worker_wakeup_failed",
            extra={"runtime_task_id": str(task_id), "reason": reason, "error": str(exc)},
        )


async def _execute_system_plan_authoring(
    *,
    plan: AgentPlanRequest,
    agent: Agent,
    model: LLMModel | None,
    fallback_model: LLMModel | None,
    claim: SystemPlanRuntimeClaim,
    seed_context: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.services.plan_mode_runtime_context import (
        reset_interactive_plan_mode,
        set_interactive_plan_mode,
    )

    plan_file_path = f"workspace/plans/{plan.id}.plan.md"
    seeded_scopes = seed_context.get("authorization_scopes")
    state = PlanModeState(
        active=True,
        plan_id=str(plan.id),
        intent_type=plan.intent_type,
        original_request=plan.original_request,
        plan_file_path=plan_file_path,
        source=SYSTEM_PLAN_RUN_SOURCE,
        reason="system_plan_run",
        authorization_scopes=[dict(scope) for scope in seeded_scopes if isinstance(scope, dict)]
        if isinstance(seeded_scopes, list)
        else None,
    )
    session_context = SessionContext(
        source=SYSTEM_PLAN_RUN_SOURCE,
        channel="internal",
        session_id=claim.session_id,
        plan_mode=state,
        metadata={
            "plan_id": str(plan.id),
            "runtime_task_id": claim.task_id.hex,
            "root_runtime_task_id": str(claim.root_runtime_task_id) if claim.root_runtime_task_id else None,
            "plan_root_runtime_task_id": str(claim.root_runtime_task_id) if claim.root_runtime_task_id else None,
            "claim_version": claim.claim_version,
            "claim_worker_id": claim.worker_id,
            "recovery_authority_type": "runtime_task",
            "recovery_authority_id": str(claim.task_id),
            "plan_authority_type": "agent_plan_request",
            "plan_authority_id": str(plan.id),
            "intent_type": plan.intent_type,
            "tenant_id": str(claim.tenant_id),
        },
    )
    session_context.metadata["plan_mode"] = state.to_metadata()

    async def _attempt() -> None:
        unsafe_events: list[dict[str, Any]] = []

        async def _on_runtime_event(event: dict[str, Any]) -> None:
            if not isinstance(event, dict) or not _requires_reconciliation(event):
                return
            unsafe_events.append(dict(event))
            await _project_system_plan_recovery_event(
                event,
                claim=claim,
                session_context=session_context,
                session_factory=session_factory,
            )

        token = set_interactive_plan_mode(state.to_metadata())
        result: Any | None = None
        invocation_error: Exception | None = None
        try:
            if plan.status in _AUTHORABLE_PLAN_STATUSES:
                if model is None:
                    raise RuntimeError("System Plan model is unavailable for durable resume")
                provision_agent_plan_file_slot(plan.agent_id, plan_file_path)
                request = AgentInvocationRequest(
                    model=model,
                    fallback_model=fallback_model,
                    messages=[
                        {
                            "role": "user",
                            "content": _build_launcher_user_prompt(plan, seed_context=seed_context),
                        }
                    ],
                    memory_messages=[{"role": "user", "content": plan.original_request or ""}],
                    agent_name=agent.name,
                    role_description=agent.role_description or "",
                    agent_id=plan.agent_id,
                    user_id=claim.root_user_id,
                    execution_identity=ExecutionIdentityRef(
                        identity_type="agent_bot",
                        identity_id=plan.agent_id,
                        label=f"Agent: {agent.name} (system plan run)",
                    ),
                    memory_session_id=claim.session_id,
                    session_context=session_context,
                    on_event=_on_runtime_event,
                    core_tools_only=False,
                    expand_tools=True,
                    max_tool_rounds=SYSTEM_PLAN_RUN_MAX_ROUNDS,
                )
                result = await invoke_agent(request)
        except Exception as exc:  # noqa: BLE001 - safe failure remains resumable; no planned work executes.
            invocation_error = exc
            logger.warning(
                "system_plan_run_failed",
                extra={"plan_id": str(plan.id), "agent_id": str(plan.agent_id), "error": str(exc)},
            )
        finally:
            reset_interactive_plan_mode(token)

        try:
            final_status = await _finalize_system_plan_runtime_task(
                plan,
                claim=claim,
                session_context=session_context,
                result=result,
                error=invocation_error,
                unsafe_events=unsafe_events,
                session_factory=session_factory,
            )
        except Exception as finalize_error:  # a newer claim/operator decision always wins.
            logger.error(
                "system_plan_run_terminal_projection_failed",
                extra={
                    "plan_id": str(plan.id),
                    "runtime_task_id": str(claim.task_id),
                    "error": str(finalize_error),
                },
            )
            return
        if final_status == "resumable":
            await _wake_system_plan_worker(reason="system_plan_run_resumable", task_id=claim.task_id)

    fence = current_runtime_task_fence()
    if fence is None:
        await run_claimed_runtime_task(
            _attempt(),
            task_id=claim.task_id,
            claim_version=claim.claim_version,
            worker_id=claim.worker_id,
            lease_seconds=SYSTEM_PLAN_RUN_LEASE_SECONDS,
            session_factory=session_factory,
        )
        return
    if (
        fence.task_id != claim.task_id
        or fence.claim_version != claim.claim_version
        or fence.worker_id != claim.worker_id
    ):
        raise SystemPlanRuntimeAuthorityError("System Plan authoring cannot run under a foreign claim fence")
    await _attempt()


async def execute_claimed_system_plan_run(
    task_id: UUID | str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    """Consume one worker-claimed System Plan from durable tenant state."""

    normalized_task_id = _uuid_or_none(task_id)
    if normalized_task_id is None:
        return False
    try:
        execution = await _load_claimed_system_plan_execution(
            normalized_task_id,
            session_factory=session_factory,
        )
    except SystemPlanRuntimeAuthorityError as exc:
        await _quarantine_claimed_system_plan(
            normalized_task_id,
            reason=f"System Plan restart authority is invalid: {exc}",
            session_factory=session_factory,
        )
        return False
    await _execute_system_plan_authoring(
        plan=execution.plan,
        agent=execution.agent,
        model=execution.model,
        fallback_model=execution.fallback_model,
        claim=execution.claim,
        seed_context=execution.seed_context,
        session_factory=session_factory,
    )
    return True


async def launch_system_plan_run(
    plan: AgentPlanRequest,
    *,
    seed_context: dict[str, Any] | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AgentPlanRequest:
    """Run a system-initiated Plan Mode pass that fills ``plan`` (a draft).

    Pre-arms Plan Mode with ``plan.id`` and runs the agent main loop; the agent's
    ``exit_plan_mode`` fills the draft and lands it ``awaiting_confirmation``. The
    caller passes a freshly created draft (``create_plan_request``) and re-loads
    the row afterwards to read the authored result.

    Fail-closed: if the agent cannot author a plan the row is left non-confirmable
    (this launcher never executes the work). The plan id is stable throughout.
    """
    model, fallback_model, agent = await _resolve_agent_models(
        plan.agent_id,
        session_factory=session_factory,
    )
    if agent is None:
        logger.warning("system_plan_run_agent_not_found", extra={"plan_id": str(plan.id)})
        return plan
    if model is None:
        logger.warning("system_plan_run_model_missing", extra={"plan_id": str(plan.id)})

    recovery_session_id = _system_plan_session_id(plan)
    durable_seed_context = _durable_seed_context(seed_context)
    configured_model_id, configured_fallback_model_id = _configured_agent_model_ids(agent)
    try:
        runtime_claim = await _claim_system_plan_runtime_task(
            plan,
            agent=agent,
            session_id=recovery_session_id,
            seed_context=durable_seed_context,
            seed_context_provided=seed_context is not None,
            model_id=configured_model_id,
            fallback_model_id=configured_fallback_model_id,
            session_factory=session_factory,
        )
    except Exception as exc:  # fail closed before exposing any tool-capable invocation
        logger.error(
            "system_plan_run_authority_failed",
            extra={"plan_id": str(plan.id), "agent_id": str(plan.agent_id), "error": str(exc)},
        )
        return plan
    if runtime_claim is None:
        logger.info(
            "system_plan_run_not_claimed",
            extra={"plan_id": str(plan.id), "agent_id": str(plan.agent_id)},
        )
        return plan
    await _wake_system_plan_worker(reason="system_plan_run_claimed", task_id=runtime_claim.task_id)
    await execute_claimed_system_plan_run(
        runtime_claim.task_id,
        session_factory=session_factory,
    )
    return plan


__all__ = [
    "SYSTEM_PLAN_RUN_SOURCE",
    "SYSTEM_PLAN_RUN_TASK_TYPE",
    "SYSTEM_PLAN_RUN_MAX_ROUNDS",
    "execute_claimed_system_plan_run",
    "system_plan_runtime_task_id",
    "launch_system_plan_run",
]
