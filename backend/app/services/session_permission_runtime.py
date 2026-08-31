"""Native Session V2 permission resolution and same-Run continuation."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.runtime.ccplus_contracts import build_permission_profile
from app.services.approval_ticket import hash_tool_input
from app.services.session_v2_persistence import AuthenticatedSessionAuthority, SessionEventDraft, append_session_events


@dataclass(frozen=True, slots=True)
class SessionPermissionResolutionReceipt:
    schema: str
    status: str
    permission_request_id: str
    invocation_id: str
    control_id: str
    run_id: str
    run_status: str
    result_event_id: str | None
    retryable: bool
    recovery_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tool_call_parts(call: Any) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(call, dict):
        raise RuntimeError("provider_tool_call_shape_invalid")
    provider_id = str(call.get("id") or "").strip()
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    tool_name = str(function.get("name") or "").strip()
    raw_arguments = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("provider_tool_call_arguments_invalid") from exc
    if not provider_id or not tool_name or not isinstance(arguments, dict):
        raise RuntimeError("provider_tool_call_binding_invalid")
    return provider_id, tool_name, arguments


def _execution_evidence(trace: dict[str, Any]) -> dict[str, Any]:
    decision = trace.get("tool_decision")
    frame = trace.get("tool_execution_frame")
    frame_status = str(frame.get("status") or "") if isinstance(frame, dict) else ""
    return {
        "schema": "hive.tool_execution_evidence.v1",
        "status": "settled" if frame_status in {"completed", "failed"} else "unavailable",
        "retryable": frame_status == "failed",
        "tool_decision": dict(decision) if isinstance(decision, dict) else None,
        "effective_arguments": (
            dict(trace["effective_arguments"]) if isinstance(trace.get("effective_arguments"), dict) else None
        ),
        "execution_frame": dict(frame) if isinstance(frame, dict) else None,
        "decision_id": trace.get("decision_id"),
        "authority_snapshot_hash": trace.get("authority_snapshot_hash"),
        "policy_snapshot_hash": trace.get("policy_snapshot_hash"),
        "capability_snapshot_hash": trace.get("capability_snapshot_hash"),
        "effect_idempotency_key": trace.get("idempotency_key"),
    }


def _result_text(value: Any) -> str:
    text = getattr(value, "text", None)
    return str(text if text is not None else value)


def _pre_effect_abort_evidence(*, receipt_ref: str) -> dict[str, Any]:
    """Prove that execution authority never crossed the durable effect fence."""

    return {
        "schema": "hive.tool_execution_evidence.v1",
        "status": "aborted",
        "retryable": False,
        "pre_effect_fence_ref": receipt_ref,
        "tool_decision": None,
        "execution_frame": None,
    }


def _typed_execution_abort(*, invocation: SessionToolInvocation, reason_code: str) -> str:
    return json.dumps(
        {
            "schema": "hive.tool_permission_execution_abort.v1",
            "status": "aborted",
            "reason_code": reason_code,
            "invocation_id": str(invocation.id),
            "provider_tool_use_id": invocation.provider_tool_use_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _source_result(db: AsyncSession, invocation: SessionToolInvocation) -> SessionModelResult:
    result = await db.scalar(
        select(SessionModelResult).where(
            SessionModelResult.tenant_id == invocation.tenant_id,
            SessionModelResult.session_id == invocation.session_id,
            SessionModelResult.run_id == invocation.run_id,
            SessionModelResult.provider_request_id == invocation.provider_request_id,
        )
    )
    if result is None or result.state != "round_committed":
        raise RuntimeError("permission_source_model_result_not_committed")
    return result


async def _settle_unstarted_batch_siblings(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    invocation: SessionToolInvocation,
) -> tuple[SessionModelResult, bool]:
    """Close never-started siblings truthfully; preserve other open approvals."""

    from app.services.session_tool_runtime import complete_tool_invocation, prepare_tool_invocation

    result = await _source_result(db, invocation)
    response = dict((result.seal_json or {}).get("response") or {})
    calls = list(response.get("tool_calls") or [])
    if not calls:
        raise RuntimeError("permission_source_tool_batch_missing")
    existing = list(
        (
            await db.execute(
                select(SessionToolInvocation)
                .where(
                    SessionToolInvocation.tenant_id == tenant_id,
                    SessionToolInvocation.session_id == session_id,
                    SessionToolInvocation.run_id == invocation.run_id,
                    SessionToolInvocation.provider_request_id == invocation.provider_request_id,
                )
                .with_for_update()
            )
        ).scalars()
    )
    by_provider_id = {row.provider_tool_use_id: row for row in existing}
    all_settled = True
    for call in calls:
        provider_id, tool_name, arguments = _tool_call_parts(call)
        sibling = by_provider_id.get(provider_id)
        if sibling is None:
            sibling = await prepare_tool_invocation(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=invocation.run_id,
                provider_request_id=invocation.provider_request_id,
                provider_tool_use_id=provider_id,
                tool_name=tool_name,
                arguments=arguments,
            )
            by_provider_id[provider_id] = sibling
        if sibling.result_event_id is not None:
            continue
        if sibling.permission_state == "waiting":
            all_settled = False
            continue
        if sibling.effect_state in {"effect_started", "needs_reconciliation"}:
            raise RuntimeError("permission_batch_has_uncertain_effect")
        aborted = {
            "schema": "hive.tool_batch_abort.v1",
            "status": "aborted",
            "reason_code": "session_permission_batch_suspended_before_sibling_start",
            "retryable": True,
            "provider_tool_use_id": provider_id,
        }
        await complete_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=sibling.id,
            provider_result_content=json.dumps(aborted, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            execution_evidence={
                "schema": "hive.tool_execution_evidence.v1",
                "status": "aborted",
                "retryable": True,
                "pre_effect_fence_ref": f"session-permission-batch-abort:{provider_id}",
                "tool_decision": None,
                "execution_frame": None,
            },
            effective_arguments=arguments,
        )
    return result, all_settled


async def _queue_same_run_continuation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    invocation: SessionToolInvocation,
    source_result: SessionModelResult,
) -> RuntimeTask:
    task = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == invocation.run_id,
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
        )
        .with_for_update()
    )
    if task is None:
        raise RuntimeError("permission_runtime_task_missing")
    metadata = dict(task.metadata_json or {})
    existing_resume = dict(metadata.get("session_permission_resume") or {})
    if str(existing_resume.get("source_result_id") or "") == str(source_result.id):
        # Resolution is idempotent at the provider Round boundary.  Multiple
        # permission cards from one tool batch still resume that Round once.
        return task
    if task.status not in {"running", "suspended", "resumable"}:
        raise RuntimeError("permission_runtime_task_not_resumable")
    metadata.update(
        {
            "interactive_pause": None,
            "session_permission_resume": {
                "schema": "hive.session_permission_resume.v1",
                "source_result_id": str(source_result.id),
                "provider_request_id": source_result.provider_request_id,
                "permission_invocation_id": str(invocation.id),
            },
            "session_resume_round_index": int(str(source_result.round_id).split(":round:", 1)[1].split(":", 1)[0]),
        }
    )
    task.status = "resumable"
    task.result_summary = None
    task.completed_at = None
    task.claimed_by = None
    task.claim_expires_at = None
    task.scheduled_at = datetime.now(timezone.utc)
    task.metadata_json = metadata
    await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=task.id,
                item_kind="run",
                lifecycle="queued",
                scope={
                    "level": "run",
                    "session_id": str(session_id),
                    "thread_id": str(session_id),
                    "turn_id": source_result.turn_id,
                    "run_id": str(task.id),
                },
                actor={"type": "runtime"},
                payload={
                    "reason_code": "session_permission_resolved",
                    "source_result_id": str(source_result.id),
                    "invocation_id": str(invocation.id),
                },
            )
        ],
    )
    return task


async def _quarantine_same_run_for_reconciliation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    invocation: SessionToolInvocation,
    source_result: SessionModelResult,
    reason_code: str,
) -> RuntimeTask:
    """Freeze the original Run when effect settlement cannot be proven."""

    task = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == invocation.run_id,
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
        )
        .with_for_update()
    )
    if task is None:
        raise RuntimeError("permission_runtime_task_missing")
    metadata = dict(task.metadata_json or {})
    existing = dict(metadata.get("session_permission_reconciliation") or {})
    if task.status == "needs_reconciliation" and str(existing.get("invocation_id") or "") == str(invocation.id):
        from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal

        await settle_and_enqueue_runtime_task_terminal(
            db,
            task,
            terminal_source="session_permission_runtime:tool_effect_settlement",
            root_reason_code=reason_code,
        )
        return task
    metadata.update(
        {
            "interactive_pause": None,
            "needs_reconciliation": True,
            "session_permission_reconciliation": {
                "schema": "hive.session_permission_reconciliation.v1",
                "invocation_id": str(invocation.id),
                "source_result_id": str(source_result.id),
                "reason_code": reason_code,
                "recovery_owner": "session_permission_runtime:tool_effect_settlement",
            },
        }
    )
    task.status = "needs_reconciliation"
    task.result_summary = None
    task.completed_at = datetime.now(timezone.utc)
    task.claim_version = int(task.claim_version or 0) + 1
    task.claimed_by = None
    task.claim_expires_at = None
    task.metadata_json = metadata
    await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=task.id,
                item_kind="run",
                lifecycle="needs_reconciliation",
                scope={
                    "level": "run",
                    "session_id": str(session_id),
                    "thread_id": str(session_id),
                    "turn_id": source_result.turn_id,
                    "run_id": str(task.id),
                },
                actor={"type": "runtime"},
                payload={
                    "reason_code": reason_code,
                    "source_result_id": str(source_result.id),
                    "invocation_id": str(invocation.id),
                    "recovery_owner": "session_permission_runtime:tool_effect_settlement",
                },
            )
        ],
    )
    from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal

    await settle_and_enqueue_runtime_task_terminal(
        db,
        task,
        terminal_source="session_permission_runtime:tool_effect_settlement",
        root_reason_code=reason_code,
    )
    return task


async def expire_stale_session_permission_requests(
    *,
    db: AsyncSession,
    now: datetime | None = None,
    limit: int = 500,
) -> int:
    """Expire canonical pre-effect permission requests and resume their Run.

    This scans the versioned invocation aggregate, never a bounded suffix of
    prose/events.  Expiration is a mechanical request lifecycle outcome: the
    tool effect was never authorized, so the matching Provider result is an
    evidence-backed ``aborted`` result rather than a platform-written answer.
    """

    from app.services.session_tool_runtime import complete_tool_invocation

    current = now or datetime.now(timezone.utc)
    candidate_ids = list(
        (
            await db.execute(
                select(SessionToolInvocation.id)
                .where(
                    SessionToolInvocation.permission_state == "waiting",
                    SessionToolInvocation.effect_state == "prepared_not_started",
                    SessionToolInvocation.permission_expires_at.is_not(None),
                    SessionToolInvocation.permission_expires_at <= current,
                )
                .order_by(SessionToolInvocation.permission_expires_at, SessionToolInvocation.id)
                .limit(max(1, int(limit)))
            )
        ).scalars()
    )
    expired = 0
    for invocation_id in candidate_ids:
        try:
            invocation = await db.scalar(
                select(SessionToolInvocation)
                .where(
                    SessionToolInvocation.id == invocation_id,
                    SessionToolInvocation.permission_state == "waiting",
                    SessionToolInvocation.effect_state == "prepared_not_started",
                    SessionToolInvocation.permission_expires_at <= current,
                )
                .with_for_update(skip_locked=True)
            )
            if invocation is None:
                await db.rollback()
                continue
            task = await db.scalar(
                select(RuntimeTask)
                .where(
                    RuntimeTask.id == invocation.run_id,
                    RuntimeTask.tenant_id == invocation.tenant_id,
                    RuntimeTask.parent_session_id == str(invocation.session_id),
                )
                .with_for_update()
            )
            if task is None or task.parent_agent_id is None:
                raise RuntimeError("permission_runtime_task_missing")
            source_result = await _source_result(db, invocation)
            if invocation.permission_item_id is None:
                raise RuntimeError("permission_item_id_missing")
            receipt_ref = f"session-permission:{invocation.permission_item_id}:expired"
            await append_session_events(
                db,
                tenant_id=invocation.tenant_id,
                agent_id=task.parent_agent_id,
                session_id=invocation.session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=invocation.permission_item_id,
                        item_kind="tool_permission",
                        lifecycle="cancelled",
                        scope={
                            "level": "round",
                            "session_id": str(invocation.session_id),
                            "thread_id": str(invocation.session_id),
                            "turn_id": source_result.turn_id,
                            "run_id": str(invocation.run_id),
                            "round_id": source_result.round_id,
                        },
                        actor={"type": "runtime"},
                        payload={
                            "invocation_id": str(invocation.id),
                            "provider_tool_use_id": invocation.provider_tool_use_id,
                            "permission_item_id": str(invocation.permission_item_id),
                            "permission_request_version": invocation.permission_request_version,
                            "receipt_ref": receipt_ref,
                            "reason_code": "tool_permission_request_expired",
                        },
                        result_id=source_result.id,
                        invocation_id=invocation.id,
                        provider_tool_use_id=invocation.provider_tool_use_id,
                    )
                ],
            )
            invocation.permission_state = "expired"
            invocation.permission_receipt_ref = receipt_ref
            invocation.recovery_owner = None
            invocation.version = int(invocation.version) + 1
            expired_payload = {
                "schema": "hive.tool_permission_result.v1",
                "status": "aborted",
                "reason_code": "tool_permission_request_expired",
                "permission_item_id": str(invocation.permission_item_id),
                "invocation_id": str(invocation.id),
            }
            await complete_tool_invocation(
                db,
                tenant_id=invocation.tenant_id,
                agent_id=task.parent_agent_id,
                session_id=invocation.session_id,
                invocation_id=invocation.id,
                provider_result_content=json.dumps(
                    expired_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                execution_evidence={
                    "schema": "hive.tool_execution_evidence.v1",
                    "status": "aborted",
                    "retryable": False,
                    "pre_effect_fence_ref": receipt_ref,
                    "tool_decision": None,
                    "execution_frame": None,
                },
                effective_arguments=dict(
                    invocation.effective_arguments_json or invocation.provider_arguments_json or {}
                ),
            )
            source_result, batch_settled = await _settle_unstarted_batch_siblings(
                db,
                tenant_id=invocation.tenant_id,
                agent_id=task.parent_agent_id,
                session_id=invocation.session_id,
                invocation=invocation,
            )
            if batch_settled:
                await _queue_same_run_continuation(
                    db,
                    tenant_id=invocation.tenant_id,
                    agent_id=task.parent_agent_id,
                    session_id=invocation.session_id,
                    invocation=invocation,
                    source_result=source_result,
                )
            await db.commit()
            expired += 1
        except Exception:
            await db.rollback()
            logger.exception(
                "Session permission expiry reconciliation failed: invocation_id={}",
                invocation_id,
            )
    return expired


async def resolve_session_tool_permission(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    permission_request_id: uuid.UUID,
    decision: str,
) -> SessionPermissionResolutionReceipt:
    """Resolve, execute if allowed, and continue the original RuntimeTask."""

    from app.services.agent_tools import execute_session_permission_tool
    from app.services.session_control_input import (
        accept_tool_permission_response,
        apply_permission_response_control_input,
    )
    from app.services.session_tool_runtime import complete_tool_invocation, mark_tool_effect_started
    from app.tools.registry import is_destructive_tool

    invocation = await db.scalar(
        select(SessionToolInvocation)
        .where(
            SessionToolInvocation.tenant_id == authority.tenant_id,
            SessionToolInvocation.session_id == authority.session_id,
            SessionToolInvocation.permission_item_id == permission_request_id,
        )
        .with_for_update()
    )
    if invocation is None:
        raise ValueError("pending_session_permission_not_found")
    if decision == "allow_session" and is_destructive_tool(invocation.tool_name):
        raise ValueError("destructive_permission_must_be_allow_once")
    control_id = uuid.uuid5(permission_request_id, f"permission-response:{decision}")
    await accept_tool_permission_response(
        db,
        authority=authority,
        control_id=control_id,
        idempotency_key=f"permission:{permission_request_id}:{decision}",
        invocation_id=invocation.id,
        permission_item_id=permission_request_id,
        permission_request_version=invocation.permission_request_version,
        permission_authority_snapshot_hash=str(invocation.permission_authority_snapshot_hash or ""),
        expected_run_id=invocation.run_id,
        decision=decision,
        response_schema="hive.tool_permission_response.v1",
    )
    await apply_permission_response_control_input(db, authority=authority, control_id=control_id)
    await db.commit()
    await db.refresh(invocation)

    effective_arguments = dict(invocation.effective_arguments_json or invocation.provider_arguments_json or {})
    input_hash = hash_tool_input(invocation.tool_name, effective_arguments)
    session = await db.get(ChatSession, authority.session_id)
    if session is None:
        raise RuntimeError("permission_chat_session_missing")
    session_grants = [
        dict(item)
        for item in (session.transcript_metadata_json or {}).get("session_permission_grants", [])
        if isinstance(item, dict)
    ]
    exact_grant = {
        "scope": "session",
        "status": "active",
        "tool_name": invocation.tool_name,
        "input_hash": input_hash,
        "control_id": str(control_id),
        "authority_snapshot_hash": invocation.permission_authority_snapshot_hash,
    }
    if decision == "allow_session" and exact_grant not in session_grants:
        session_grants.append(exact_grant)
        session_metadata = dict(session.transcript_metadata_json or {})
        session_metadata["session_permission_grants"] = session_grants
        session.transcript_metadata_json = session_metadata
        await db.commit()

    if decision in {"allow_once", "allow_session"} and invocation.result_event_id is None:
        trace: dict[str, Any] = {}
        invocation_id = invocation.id

        async def _pre_effect(payload: dict[str, Any]) -> None:
            await mark_tool_effect_started(
                db,
                tenant_id=authority.tenant_id,
                agent_id=authority.agent_id,
                session_id=authority.session_id,
                invocation_id=invocation.id,
                effective_arguments=(
                    payload.get("arguments") if isinstance(payload.get("arguments"), dict) else effective_arguments
                ),
                permission_control_id=control_id,
            )
            await db.commit()

        profile = build_permission_profile(
            {
                "mode": "default",
                "session_grant_scope": "once",
                "session_grant_tool_name": invocation.tool_name,
                "session_grant_input_hash": input_hash,
                "session_grants": tuple(session_grants),
            }
        )
        try:
            tool_result = await execute_session_permission_tool(
                invocation.tool_name,
                effective_arguments,
                agent_id=authority.agent_id,
                user_id=authority.principal_id,
                session_id=str(authority.session_id),
                permission_profile=profile,
                tool_call_id=invocation.provider_tool_use_id,
                turn_id=(await _source_result(db, invocation)).turn_id,
                runtime_task_id=str(invocation.run_id),
                origin_channel=getattr(session, "source_channel", None) or "web",
                round_state={"trace_id": f"permission:{control_id}"},
                pre_effect_callback=_pre_effect,
                trace_metadata_sink=trace,
            )
        except Exception as exc:
            # The callback is the only path that releases effect authority and
            # commits that fact before executor entry.  If its durable state is
            # still prepared, an aborted matching result is provable.  Once the
            # fence crossed, an escaped exception is effect-uncertain: never
            # guess a failure receipt and never replay the effect.
            await db.rollback()
            invocation = await db.scalar(
                select(SessionToolInvocation)
                .where(
                    SessionToolInvocation.id == invocation_id,
                    SessionToolInvocation.tenant_id == authority.tenant_id,
                    SessionToolInvocation.session_id == authority.session_id,
                )
                .with_for_update()
            )
            if invocation is None:
                raise RuntimeError("tool_invocation_not_found") from exc
            if invocation.effect_state == "prepared_not_started":
                abort_ref = f"session-permission:{control_id}:effect-authority-not-released"
                await complete_tool_invocation(
                    db,
                    tenant_id=authority.tenant_id,
                    agent_id=authority.agent_id,
                    session_id=authority.session_id,
                    invocation_id=invocation.id,
                    provider_result_content=_typed_execution_abort(
                        invocation=invocation,
                        reason_code="approved_tool_execution_not_released",
                    ),
                    execution_evidence=_pre_effect_abort_evidence(receipt_ref=abort_ref),
                    effective_arguments=effective_arguments,
                )
            else:
                await complete_tool_invocation(
                    db,
                    tenant_id=authority.tenant_id,
                    agent_id=authority.agent_id,
                    session_id=authority.session_id,
                    invocation_id=invocation.id,
                    provider_result_content="",
                    execution_evidence=None,
                    effective_arguments=effective_arguments,
                )
            logger.exception(
                "Session-approved tool execution interrupted: invocation_id={} effect_state={}",
                invocation.id,
                invocation.effect_state,
            )
        else:
            await db.refresh(invocation)
            evidence = _execution_evidence(trace)
            typed_decision = evidence.get("tool_decision")
            outcome = str(typed_decision.get("outcome") or "") if isinstance(typed_decision, dict) else ""
            # A second approval request (or an untyped stop) after the exact
            # control was applied must not create an approval loop.  Because
            # the durable fence still proves no effect authority was released,
            # settle it as an explicit abort and let the model interpret that
            # typed result in the original Run.
            if invocation.effect_state == "prepared_not_started" and outcome not in {
                "deny",
                "unavailable",
            }:
                frame = evidence.get("execution_frame")
                frame_status = str(frame.get("status") or "") if isinstance(frame, dict) else ""
                if outcome not in {"allow", "allow_prepare_only"} or frame_status not in {
                    "completed",
                    "failed",
                }:
                    abort_ref = f"session-permission:{control_id}:approved-effect-not-released"
                    evidence = _pre_effect_abort_evidence(receipt_ref=abort_ref)
                    tool_result = _typed_execution_abort(
                        invocation=invocation,
                        reason_code="approved_permission_not_consumed",
                    )
            await complete_tool_invocation(
                db,
                tenant_id=authority.tenant_id,
                agent_id=authority.agent_id,
                session_id=authority.session_id,
                invocation_id=invocation.id,
                provider_result_content=_result_text(tool_result),
                execution_evidence=evidence,
                effective_arguments=(
                    evidence.get("effective_arguments")
                    if isinstance(evidence.get("effective_arguments"), dict)
                    else effective_arguments
                ),
            )
        await db.commit()
        await db.refresh(invocation)

        if invocation.result_event_id is None:
            source_result = await _source_result(db, invocation)
            task = await _quarantine_same_run_for_reconciliation(
                db,
                tenant_id=authority.tenant_id,
                agent_id=authority.agent_id,
                session_id=authority.session_id,
                invocation=invocation,
                source_result=source_result,
                reason_code="approved_tool_effect_settlement_uncertain",
            )
            await db.commit()
            return SessionPermissionResolutionReceipt(
                schema="hive.session_permission_resolution.v2",
                status="needs_reconciliation",
                permission_request_id=str(permission_request_id),
                invocation_id=str(invocation.id),
                control_id=str(control_id),
                run_id=str(invocation.run_id),
                run_status=str(task.status),
                result_event_id=None,
                retryable=False,
                recovery_action="reconcile_tool_effect",
            )

    source_result, batch_settled = await _settle_unstarted_batch_siblings(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        invocation=invocation,
    )
    task = await db.get(RuntimeTask, invocation.run_id)
    if task is None:
        raise RuntimeError("permission_runtime_task_missing")
    if batch_settled:
        task = await _queue_same_run_continuation(
            db,
            tenant_id=authority.tenant_id,
            agent_id=authority.agent_id,
            session_id=authority.session_id,
            invocation=invocation,
            source_result=source_result,
        )
    await db.commit()
    await db.refresh(invocation)
    return SessionPermissionResolutionReceipt(
        schema="hive.session_permission_resolution.v2",
        status=("resolved" if batch_settled else "waiting_for_sibling_permissions"),
        permission_request_id=str(permission_request_id),
        invocation_id=str(invocation.id),
        control_id=str(control_id),
        run_id=str(invocation.run_id),
        run_status=str(task.status),
        result_event_id=str(invocation.result_event_id) if invocation.result_event_id else None,
        retryable=False,
        recovery_action=("resume_same_runtime_task" if batch_settled else "resolve_remaining_permission_items"),
    )


__all__ = [
    "SessionPermissionResolutionReceipt",
    "expire_stale_session_permission_requests",
    "resolve_session_tool_permission",
]
