"""Memory system hook handler registration.

Phase 0: logging-only for SESSION_START, POST_COMPACTION, MEMORY_EXTRACTED.
Phase 1: T0 session ledger segment boundaries for TURN_STOP/TURN_ABORT,
         fallback SESSION_CLOSE/IDLE, plus
         user-work runtime session seals for TRIGGER_END/DELEGATION_END and
         system audit events for HEARTBEAT_TICK_END/DREAM_END.
Phase 2: session projection on RESPONSE_COMPLETE/PRE_COMPACTION; canonical
         T0->T2 Segment Package build after T0 segment seal.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from contextvars import Context
from pathlib import Path

from app.runtime.hooks import (
    HookContext,
    HookEvent,
    describe_registration_specs,
    hook_registry,
    load_registration_specs,
)
from app.memory.t0.ledger import append_t0_session_event, seal_t0_session_segment
from app.services.pending_reply_service import OUTBOUND_TOOL_NAMES
from app.services.session_memory import (
    build_session_memory_payload_with_llm,
    update_session_memory,
    write_compaction_summary,
)

logger = logging.getLogger(__name__)
_DEFAULT_HOOK_REGISTRY = hook_registry
_T2_BACKGROUND_TASKS: dict[str, asyncio.Task[None]] = {}
_REQUIRED_TURN_BOUNDARY_MARKER = "required_terminal_boundary_projected"
_REQUIRED_RESPONSE_COMPLETE_MARKER = "required_response_complete_projected"


# ── Logging-only handlers (Phase 0, kept for events without active handler) ──


async def _log_session_start(ctx: HookContext) -> None:
    model = ctx.metadata.get("model", "?")
    logger.info(
        "[Hooks] SESSION_START: agent=%s source=%s model=%s",
        ctx.agent_id,
        ctx.source,
        model,
    )


async def _log_post_compaction(ctx: HookContext) -> None:
    trigger = ctx.metadata.get("trigger", "?")
    summary_len = len(ctx.metadata.get("summary", ""))
    logger.info(
        "[Hooks] POST_COMPACTION: agent=%s trigger=%s summary_len=%d",
        ctx.agent_id,
        trigger,
        summary_len,
    )
    agent_id = _parse_agent_id(ctx)
    if agent_id and ctx.metadata.get("summary"):
        write_compaction_summary(
            agent_id,
            str(ctx.metadata.get("summary", "")),
            original_message_count=ctx.metadata.get("before_msgs"),
            kept_message_count=ctx.metadata.get("after_msgs"),
        )


async def _log_memory_extracted(ctx: HookContext) -> None:
    logger.info("[Hooks] MEMORY_EXTRACTED: agent=%s", ctx.agent_id)


async def _audit_permission_denied(ctx: HookContext) -> None:
    """PERMISSION_DENIED → observe-only audit log for governed denials.

    Tool governance and the chat-session permission path both emit this event
    live when a capability/approval decision blocks execution. This consumer is
    deliberately observe-only: it records the denial for audit/monitoring and
    never returns a HookResult, so it cannot alter the already-final decision.
    """
    capability = ctx.metadata.get("capability")
    reason = ctx.metadata.get("reason") or ctx.error or ""
    logger.info(
        "[Hooks] PERMISSION_DENIED: agent=%s tool=%s capability=%s mode=%s reason=%s",
        ctx.agent_id,
        ctx.tool_name,
        capability,
        ctx.metadata.get("permission_mode"),
        str(reason)[:200],
    )


# ── Session projection handlers (Phase 2) ──


def _has_committed_response_receipt(ctx: HookContext) -> bool:
    receipt = ctx.metadata.get("response_commit")
    return bool(
        isinstance(receipt, dict)
        and receipt.get("schema") == "hive.response_commit.v1"
        and receipt.get("committed") is True
        and str(receipt.get("commit_kind") or "").strip()
        and str(receipt.get("idempotency_key") or "").strip()
        and any(str(ref).strip() for ref in receipt.get("source_refs") or [])
    )


async def _project_on_response(ctx: HookContext) -> dict[str, object] | None:
    """RESPONSE_COMPLETE → update volatile session projection only.

    Durable T2 is now built only from sealed T0 segments. This avoids the old
    direct messages -> learnings/*.md path and keeps T2 one-to-one with T0
    segment source ranges.
    """
    if ctx.metadata.get(_REQUIRED_RESPONSE_COMPLETE_MARKER):
        return None
    if not _has_committed_response_receipt(ctx):
        logger.warning("[Hooks] RESPONSE_COMPLETE held: missing committed response receipt")
        return None
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return None
    turn = ctx.metadata.get("turn_count", "?")
    logger.info(
        "[Hooks] RESPONSE_COMPLETE: agent=%s source=%s turn=%s projection_only=true", ctx.agent_id, ctx.source, turn
    )
    return await _project_committed_response_session_memory(ctx, agent_id, ctx.messages or [])


def _agent_data_root() -> Path:
    from app.config import get_settings

    return Path(get_settings().AGENT_DATA_DIR)


def _session_memory_metadata(ctx: HookContext) -> dict:
    metadata = dict(ctx.metadata or {})
    if ctx.session_id and not metadata.get("session_id"):
        metadata["session_id"] = str(ctx.session_id)
    metadata.setdefault("source", ctx.source or str(ctx.event))
    return metadata


def _committed_response_base_ref(ctx: HookContext) -> str:
    receipt = ctx.metadata.get("response_commit")
    refs = [str(ref).strip() for ref in (receipt or {}).get("source_refs") or [] if str(ref).strip()]
    if isinstance(receipt, dict) and receipt.get("commit_kind") == "runtime_terminal_boundary":
        boundary_id = str(ctx.metadata.get("terminal_boundary_id") or "").strip()
        idempotency_key = str(ctx.metadata.get("terminal_boundary_idempotency_key") or "").strip()
        runtime_task_id = str(ctx.metadata.get("runtime_task_id") or "").strip()
        if (
            str(receipt.get("terminal_boundary_id") or "").strip() != boundary_id
            or str(receipt.get("idempotency_key") or "").strip() != idempotency_key
            or str(receipt.get("runtime_task_id") or "").strip() != runtime_task_id
        ):
            return ""
        try:
            canonical = f"runtime-terminal-boundary://{uuid.UUID(boundary_id)}"
        except ValueError:
            return ""
        return canonical if canonical in refs else ""
    prefix = "session-run-outcome://"
    for ref in refs:
        if not ref.startswith(prefix):
            continue
        identifier = ref.removeprefix(prefix)
        if "/" in identifier:
            continue
        try:
            return f"{prefix}{uuid.UUID(identifier)}"
        except ValueError:
            continue
    return ""


async def _project_committed_response_session_memory(
    ctx: HookContext,
    agent_id: uuid.UUID,
    messages: list[dict],
) -> dict[str, object]:
    """Commit the volatile session projection exactly once per response receipt."""

    from app.services.agent_asset_transaction import AgentAssetTransaction

    response_commit = dict(ctx.metadata.get("response_commit") or {})
    response_idempotency_key = str(response_commit.get("idempotency_key") or "").strip()
    if not response_idempotency_key:
        raise ValueError("committed session projection requires response_commit.idempotency_key")
    evidence_refs = [str(ref).strip() for ref in response_commit.get("source_refs") or [] if str(ref).strip()]
    response_base_ref = _committed_response_base_ref(ctx)
    if not response_base_ref:
        raise ValueError("committed session projection requires a canonical source ref")
    data_root = _agent_data_root()
    agent_root = data_root / str(agent_id)
    with AgentAssetTransaction(
        agent_root,
        operation="response_complete_session_projection",
        idempotency_key=f"session-memory:{response_idempotency_key}",
        evidence_refs=evidence_refs,
    ) as transaction:
        if transaction.is_replay:
            receipt = transaction.commit()
            logger.info(
                "[Hooks] RESPONSE_COMPLETE session projection replay agent=%s session=%s revision=%d",
                agent_id,
                ctx.session_id,
                receipt.revision,
            )
            return {
                "source_ref": response_base_ref,
                "transaction_id": receipt.transaction_id,
                "revision": receipt.revision,
                "idempotent_replay": True,
            }
        payload = await build_session_memory_payload_with_llm(
            messages,
            metadata=_session_memory_metadata(ctx),
            agent_id=agent_id,
            tenant_id=ctx.metadata.get("tenant_id"),
        )
        update_session_memory(
            agent_id,
            payload,
            data_root=data_root,
            transaction=transaction,
        )
        receipt = transaction.commit()
        logger.info(
            "[Hooks] RESPONSE_COMPLETE session projection committed agent=%s session=%s revision=%d",
            agent_id,
            ctx.session_id,
            receipt.revision,
        )
        return {
            "source_ref": response_base_ref,
            "transaction_id": receipt.transaction_id,
            "revision": receipt.revision,
            "idempotent_replay": False,
        }


def _t0_safe_metadata(metadata: dict | None) -> dict:
    from app.runtime.context import runtime_assembly_metadata

    safe = dict(metadata or {})
    assembly_state = runtime_assembly_metadata(safe)
    if assembly_state:
        assembly_state.pop("activation_events", None)
        safe["runtime_assembly_state"] = assembly_state
    return safe


async def _update_session_memory_projection(ctx: HookContext, agent_id: uuid.UUID, messages: list[dict]) -> None:
    payload = await build_session_memory_payload_with_llm(
        messages,
        metadata=_session_memory_metadata(ctx),
        agent_id=agent_id,
        tenant_id=ctx.metadata.get("tenant_id"),
    )
    update_session_memory(agent_id, payload)


async def _run_fast_reflection_learning_brain(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: str,
    messages: list[dict],
    metadata: dict,
) -> dict[str, object] | None:
    """Use the post-turn learning brain for fast-reflection signal routing.

    Failure returns None and leaves the synchronous service without a learning
    candidate. This hook runs after RESPONSE_COMPLETE in a background task, so
    it does not add latency to the user response.
    """
    from app.services.fast_reflection_learning_brain import classify_fast_reflection_signal_with_learning_brain

    return await classify_fast_reflection_signal_with_learning_brain(
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        messages=messages,
        metadata=metadata,
        data_root=_agent_data_root(),
    )


def schedule_fast_reflection_candidate(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    messages: list[dict],
    metadata: dict,
) -> dict[str, str]:
    async def _run() -> None:
        try:
            await _create_fast_reflection_candidate(
                data_root=data_root,
                agent_id=agent_id,
                session_id=session_id,
                messages=messages,
                metadata=metadata,
            )
        except Exception as exc:
            logger.debug("[FastReflection] skipped for %s: %s", agent_id, exc)

    asyncio.create_task(_run())
    return {"status": "scheduled"}


async def _create_fast_reflection_candidate(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    messages: list[dict],
    metadata: dict,
) -> dict[str, object]:
    """Commit one idempotent fast-reflection candidate before returning."""

    from app.services.fast_reflection_service import create_fast_reflection_candidate

    return await asyncio.to_thread(
        create_fast_reflection_candidate,
        data_root=data_root,
        agent_id=agent_id,
        session_id=session_id,
        messages=messages,
        metadata=metadata,
    )


async def _load_fast_reflection_disposition(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    response_idempotency_key: str,
) -> dict[str, object] | None:
    """Read a committed disposition before any auxiliary provider call."""

    from app.services.fast_reflection_service import load_fast_reflection_disposition

    return await asyncio.to_thread(
        load_fast_reflection_disposition,
        data_root=data_root,
        agent_id=agent_id,
        session_id=session_id,
        response_idempotency_key=response_idempotency_key,
    )


def _fast_reflection_source_ref(ctx: HookContext) -> str:
    base_ref = _committed_response_base_ref(ctx)
    if not base_ref:
        raise ValueError("committed fast reflection requires a canonical SessionRunOutcome source ref")
    return base_ref


async def _fast_reflection_on_response(ctx: HookContext) -> dict[str, object] | None:
    """RESPONSE_COMPLETE → durable candidate creation for strong correction signals."""
    if ctx.metadata.get(_REQUIRED_RESPONSE_COMPLETE_MARKER):
        return None
    if not _has_committed_response_receipt(ctx):
        logger.warning("[Hooks] fast reflection held: missing committed response receipt")
        return None
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        raise ValueError("committed fast reflection requires an Agent UUID")
    messages = ctx.messages or []
    if not messages:
        result: dict[str, object] = {"status": "skipped", "reason": "no_messages"}
        return {**result, "source_ref": _fast_reflection_source_ref(ctx)}
    metadata = dict(ctx.metadata or {})
    metadata.setdefault("source", ctx.source or "web")
    response_commit = metadata.get("response_commit") if isinstance(metadata.get("response_commit"), dict) else {}
    response_idempotency_key = str(response_commit.get("idempotency_key") or "").strip()
    replay = await _load_fast_reflection_disposition(
        data_root=_agent_data_root(),
        agent_id=agent_id,
        session_id=str(ctx.session_id or ""),
        response_idempotency_key=response_idempotency_key,
    )
    if replay is not None:
        return {**replay, "source_ref": _fast_reflection_source_ref(ctx)}
    tenant_id = metadata.get("tenant_id")
    classification = await _run_fast_reflection_learning_brain(
        agent_id=agent_id,
        tenant_id=uuid.UUID(str(tenant_id)) if tenant_id else None,
        session_id=str(ctx.session_id or ""),
        messages=messages,
        metadata=metadata,
    )
    if classification is not None:
        metadata["fast_reflection_classification"] = classification
    result = await _create_fast_reflection_candidate(
        data_root=_agent_data_root(),
        agent_id=agent_id,
        session_id=str(ctx.session_id or ""),
        messages=messages,
        metadata=metadata,
    )
    source_ref = _fast_reflection_source_ref(ctx)
    logger.info(
        "[Hooks] fast reflection projected agent=%s session=%s status=%s source_ref=%s",
        agent_id,
        ctx.session_id,
        str(result.get("status") or "unknown"),
        source_ref,
    )
    return {**result, "source_ref": source_ref}


def _required_response_projection_sha256(
    *,
    session_projection: dict[str, object],
    fast_reflection: dict[str, object],
) -> str:
    """Hash a replay-stable, prose-free receipt for required consumers."""

    receipt = {
        "session_projection": {
            "transaction_id": str(session_projection.get("transaction_id") or ""),
            "revision": int(session_projection.get("revision") or 0),
        },
        "fast_reflection": {
            "status": str(fast_reflection.get("status") or "unknown"),
            "reason": str(fast_reflection.get("reason") or ""),
            "candidate_id": str(fast_reflection.get("candidate_id") or ""),
            "signal_type": str(fast_reflection.get("signal_type") or ""),
            "classification_method": str(fast_reflection.get("classification_method") or ""),
        },
    }
    encoded = json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def project_committed_response_complete(ctx: HookContext) -> dict[str, object]:
    """Await the two required RESPONSE_COMPLETE projections.

    ``HookRegistry.emit`` intentionally treats observer failures as advisory and
    records them instead of raising.  A durable terminal-boundary outbox cannot
    acknowledge work on that basis, so its processor calls this narrow helper
    and lets either required projection failure propagate for retry.
    """

    if ctx.event != HookEvent.RESPONSE_COMPLETE or not _has_committed_response_receipt(ctx):
        raise ValueError("committed RESPONSE_COMPLETE projection requires an authoritative receipt")
    if _parse_agent_id(ctx) is None or not str(ctx.session_id or "").strip():
        raise ValueError("committed RESPONSE_COMPLETE projection requires Agent and session authority")
    session_projection = await _project_on_response(ctx)
    fast_reflection = await _fast_reflection_on_response(ctx)
    if not isinstance(session_projection, dict) or not isinstance(fast_reflection, dict):
        raise RuntimeError("committed RESPONSE_COMPLETE projection did not produce required receipts")
    source_refs = [str(receipt.get("source_ref") or "").strip() for receipt in (session_projection, fast_reflection)]
    response_base_ref = _committed_response_base_ref(ctx)
    if not response_base_ref or source_refs != [response_base_ref, response_base_ref]:
        raise RuntimeError("committed RESPONSE_COMPLETE projection did not produce canonical source refs")
    ctx.metadata[_REQUIRED_RESPONSE_COMPLETE_MARKER] = True
    return {
        "source_refs": [response_base_ref],
        "receipt_sha256": _required_response_projection_sha256(
            session_projection=session_projection,
            fast_reflection=fast_reflection,
        ),
    }


async def _project_on_pre_compaction(ctx: HookContext) -> None:
    """PRE_COMPACTION → create a T0 resume checkpoint before context is compacted."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    trigger = ctx.metadata.get("trigger", "?")
    logger.info(
        "[Hooks] PRE_COMPACTION: agent=%s trigger=%s msgs=%d checkpoint=true",
        ctx.agent_id,
        trigger,
        len(ctx.messages or []),
    )
    await _update_session_memory_projection(ctx, agent_id, ctx.messages or [])
    if not ctx.session_id:
        return
    sealed = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=str(ctx.session_id),
        reason=f"pre_compaction:{trigger}",
        metadata={
            **_t0_safe_metadata(ctx.metadata),
            "source": ctx.source or "web",
            "trigger": trigger,
            "checkpoint_kind": ctx.metadata.get("checkpoint_kind") or "pre_compaction_checkpoint",
        },
    )
    if not sealed:
        return
    ctx.metadata.setdefault("checkpoint_kind", "pre_compaction_checkpoint")
    ctx.metadata.setdefault("boundary_event_id", sealed.event_id)
    logger.info(
        "[Hooks] PRE_COMPACTION: sealed T0 checkpoint agent=%s session=%s segment=%s seq=%d",
        agent_id,
        ctx.session_id,
        sealed.segment_id,
        sealed.sequence,
    )
    await _build_t2_for_sealed_segment(ctx=ctx, agent_id=agent_id, segment_id=sealed.segment_id)


# ── T0 writers (Phase 1) ──


def _parse_agent_id(ctx: HookContext) -> uuid.UUID | None:
    """Parse agent_id from HookContext, return None on failure."""
    try:
        return uuid.UUID(str(ctx.agent_id))
    except (ValueError, AttributeError):
        logger.warning("[T0] Invalid agent_id: %s", ctx.agent_id)
        return None


def _is_reportable_session(messages: list[dict], metadata: dict) -> bool:
    del messages
    if metadata.get("loop_guard_triggered") or metadata.get("failed") or metadata.get("partial_failure"):
        return True
    if metadata.get("commit") or metadata.get("deployment") or metadata.get("external_action"):
        return True
    return False


def _safe_t0_session_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)


def _runtime_event_session_id(ctx: HookContext, event_type: str) -> str:
    if ctx.session_id:
        return _safe_t0_session_id(ctx.session_id)
    for key in (
        "session_id",
        "runtime_task_id",
        "task_id",
        "run_id",
        "trigger_run_id",
        "trigger_id",
        "delegation_id",
        "tick_id",
        "dream_id",
    ):
        value = ctx.metadata.get(key)
        if value:
            return f"{event_type}-{_safe_t0_session_id(value)}"
    return f"{event_type}-{uuid.uuid4().hex}"


def _runtime_event_content(messages: list[dict] | None, fallback: str) -> str:
    rendered: list[str] = []
    for message in messages or []:
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        role = str(message.get("role") or "").strip()
        rendered.append(f"{role}: {content}" if role and len(messages or []) > 1 else content)
    return "\n".join(rendered) if rendered else fallback


def _append_and_seal_runtime_t0_event(
    *,
    agent_id: uuid.UUID,
    ctx: HookContext,
    event_type: str,
    boundary_reason: str,
    fallback_content: str,
    include_messages: bool = False,
) -> tuple[str, str | None]:
    session_id = _runtime_event_session_id(ctx, event_type)
    source = ctx.source or event_type
    metadata = {**_t0_safe_metadata(ctx.metadata), "source": source, "hook_event": str(ctx.event)}
    append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type=event_type,
        role="system",
        content=_runtime_event_content(ctx.messages, fallback_content) if include_messages else fallback_content,
        actor_id=agent_id,
        source=source,
        metadata=metadata,
    )
    sealed = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason=boundary_reason,
        metadata=metadata,
    )
    return session_id, sealed.segment_id if sealed else None


def _seal_existing_runtime_t0_segment(
    *,
    agent_id: uuid.UUID,
    ctx: HookContext,
    event_type: str,
    boundary_reason: str,
) -> tuple[str, str | None]:
    """Seal an already-written user-work transcript without fabricating evidence.

    Trigger/delegation transcript events must be appended at acceptance,
    tool-call, and assistant-final points. The end hook is only a physical
    segment boundary. If no transcript exists, returning no segment is safer
    than producing a summary-only T0 packet that cannot support resume.
    """

    session_id = _runtime_event_session_id(ctx, event_type)
    source = ctx.source or event_type
    sealed = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason=boundary_reason,
        metadata={**_t0_safe_metadata(ctx.metadata), "source": source, "hook_event": str(ctx.event)},
    )
    if not sealed:
        logger.warning(
            "[Hooks] %s has no open T0 transcript to seal; refusing to fabricate summary evidence agent=%s session=%s",
            str(ctx.event),
            agent_id,
            session_id,
        )
        return session_id, None
    return session_id, sealed.segment_id


def _with_t0_runtime_session(ctx: HookContext, session_id: str) -> HookContext:
    return HookContext(
        event=ctx.event,
        agent_id=ctx.agent_id,
        session_id=session_id,
        tool_name=ctx.tool_name,
        tool_args=ctx.tool_args,
        tool_result=ctx.tool_result,
        error=ctx.error,
        metadata=ctx.metadata,
        messages=ctx.messages,
        source=ctx.source,
        _evidence_db=ctx._evidence_db,
    )


def _session_lineage_metadata(ctx: HookContext) -> dict[str, object] | None:
    """Extract branch/checkpoint lineage metadata for T2 source-bundle construction."""

    keys = (
        "root_session_id",
        "parent_session_id",
        "branch_session_id",
        "branch_mode",
        "source_session_id",
        "anchor_event_id",
        "anchor_sequence",
        "visible_prefix_end",
        "regenerate_from_event_id",
        "regenerate_prompt_source_event_id",
        "edit_from_event_id",
        "rollback_strategy",
        "command",
        "turn_id",
        "intent_id",
        "checkpoint_kind",
        "turn_stop_event_id",
        "turn_abort_event_id",
        "user_prompt_submit_event_id",
        "boundary_event_id",
        "runtime_task_id",
        "request_id",
        "trace_id",
    )
    payload = {key: ctx.metadata[key] for key in keys if key in ctx.metadata and ctx.metadata[key] not in (None, "")}
    if ctx.session_id:
        payload.setdefault("session_id", str(ctx.session_id))
    return payload or None


async def _t0_turn_stop(ctx: HookContext, *, required_t2: bool = False) -> None:
    """TURN_STOP → seal the completed user turn and start canonical T2 packaging."""
    if ctx.metadata.get(_REQUIRED_TURN_BOUNDARY_MARKER):
        return
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    messages = ctx.messages or []
    reason = str(ctx.metadata.get("reason") or "turn_stop")
    logger.info("[Hooks] TURN_STOP: agent=%s reason=%s msgs=%d", ctx.agent_id, reason, len(messages))
    if messages:
        await _update_session_memory_projection(ctx, agent_id, messages)
    if not ctx.session_id:
        return
    metadata = {
        **_t0_safe_metadata(ctx.metadata),
        "source": ctx.source or "web",
        "checkpoint_kind": ctx.metadata.get("checkpoint_kind") or "user_turn_stop",
        "semantic_memory_eligible": ctx.metadata.get("semantic_memory_eligible", True),
    }
    sealed = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=str(ctx.session_id),
        reason=reason,
        metadata=metadata,
        boundary_id=ctx.metadata.get("terminal_boundary_id"),
        idempotency_key=ctx.metadata.get("terminal_boundary_idempotency_key"),
        expected_runtime_task_id=ctx.metadata.get("runtime_task_id"),
        expected_turn_id=ctx.metadata.get("turn_id"),
    )
    if not sealed:
        return
    ctx.metadata.setdefault("checkpoint_kind", metadata["checkpoint_kind"])
    ctx.metadata.setdefault("boundary_event_id", sealed.event_id)
    ctx.metadata.setdefault("turn_stop_event_id", sealed.event_id)
    logger.info(
        "[Hooks] TURN_STOP: sealed T0 segment agent=%s session=%s segment=%s seq=%d",
        agent_id,
        ctx.session_id,
        sealed.segment_id,
        sealed.sequence,
    )
    await _build_t2_for_sealed_segment(
        ctx=ctx,
        agent_id=agent_id,
        segment_id=sealed.segment_id,
        required=required_t2,
    )


async def _t0_turn_abort(ctx: HookContext) -> None:
    """TURN_ABORT → seal a dirty/aborted turn without semantic T2 packaging."""
    if ctx.metadata.get(_REQUIRED_TURN_BOUNDARY_MARKER):
        return
    agent_id = _parse_agent_id(ctx)
    if not agent_id or not ctx.session_id:
        return
    reason = str(ctx.metadata.get("reason") or ctx.error or "turn_abort")
    logger.info("[Hooks] TURN_ABORT: agent=%s reason=%s", ctx.agent_id, reason)
    metadata = {
        **_t0_safe_metadata(ctx.metadata),
        "source": ctx.source or "web",
        "checkpoint_kind": ctx.metadata.get("checkpoint_kind") or "turn_abort",
        "semantic_memory_eligible": False,
    }
    sealed = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=str(ctx.session_id),
        reason=reason,
        metadata=metadata,
        boundary_id=ctx.metadata.get("terminal_boundary_id"),
        idempotency_key=ctx.metadata.get("terminal_boundary_idempotency_key"),
        expected_runtime_task_id=ctx.metadata.get("runtime_task_id"),
        expected_turn_id=ctx.metadata.get("turn_id"),
    )
    if sealed:
        logger.info(
            "[Hooks] TURN_ABORT: sealed T0 segment agent=%s session=%s segment=%s seq=%d",
            agent_id,
            ctx.session_id,
            sealed.segment_id,
            sealed.sequence,
        )


async def _t0_session_close(ctx: HookContext) -> None:
    """SESSION_CLOSE → seal the append-only T0 segment and start canonical T2 packaging."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    reason = ctx.metadata.get("reason", "unknown")
    messages = ctx.messages or []
    logger.info("[Hooks] SESSION_CLOSE: agent=%s reason=%s msgs=%d", ctx.agent_id, reason, len(messages))
    if messages:
        await _update_session_memory_projection(ctx, agent_id, messages)
    if ctx.session_id:
        sealed = seal_t0_session_segment(
            agent_id=agent_id,
            session_id=str(ctx.session_id),
            reason=str(reason or "session_close"),
            metadata={
                **_t0_safe_metadata(ctx.metadata),
                "source": ctx.source or "web",
                "checkpoint_kind": ctx.metadata.get("checkpoint_kind") or "session_close_fallback",
            },
        )
        if sealed:
            ctx.metadata.setdefault("checkpoint_kind", "session_close_fallback")
            ctx.metadata.setdefault("boundary_event_id", sealed.event_id)
            logger.info(
                "[Hooks] SESSION_CLOSE: sealed T0 segment agent=%s session=%s segment=%s seq=%d",
                agent_id,
                ctx.session_id,
                sealed.segment_id,
                sealed.sequence,
            )
            await _build_t2_for_sealed_segment(ctx=ctx, agent_id=agent_id, segment_id=sealed.segment_id)
    if _is_reportable_session(messages, ctx.metadata):
        try:
            from app.config import get_settings
            from app.services.reflection_service import create_reportable_reflection

            create_reportable_reflection(
                data_root=Path(get_settings().AGENT_DATA_DIR),
                agent_id=agent_id,
                session_id=str(ctx.session_id or ""),
                reason=str(ctx.metadata.get("reason") or "session_close_reportable"),
                messages=messages,
                metadata=ctx.metadata,
            )
        except Exception as exc:
            logger.debug("[Hooks] reportable reflection skipped for %s: %s", agent_id, exc)


async def _t0_session_idle(ctx: HookContext) -> None:
    """SESSION_IDLE → seal the active T0 segment as a resume boundary.

    RESPONSE_COMPLETE only updates volatile session projection. Durable T2 is
    built here from the sealed T0 source range; no historical T0 events are
    rewritten.
    """
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    idle_s = ctx.metadata.get("idle_seconds", "?")
    if not ctx.session_id:
        return
    sealed = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=str(ctx.session_id),
        reason="session_idle",
        metadata={
            **_t0_safe_metadata(ctx.metadata),
            "source": ctx.source or "web",
            "idle_seconds": idle_s,
            "checkpoint_kind": ctx.metadata.get("checkpoint_kind") or "session_idle_fallback",
        },
    )
    if sealed:
        ctx.metadata.setdefault("checkpoint_kind", "session_idle_fallback")
        ctx.metadata.setdefault("boundary_event_id", sealed.event_id)
        logger.info(
            "[Hooks] SESSION_IDLE: sealed T0 segment agent=%s session=%s idle=%ss segment=%s seq=%d",
            ctx.agent_id,
            ctx.session_id,
            idle_s,
            sealed.segment_id,
            sealed.sequence,
        )
        await _build_t2_for_sealed_segment(ctx=ctx, agent_id=agent_id, segment_id=sealed.segment_id)


async def _build_t2_for_sealed_segment(
    *,
    ctx: HookContext,
    agent_id: uuid.UUID,
    segment_id: str,
    required: bool = False,
) -> None:
    """Kick canonical T0 -> T2 package creation for a sealed semantic session segment."""

    if not ctx.session_id:
        return
    eligible, reason = _t0_segment_t2_eligible(ctx)
    if not eligible:
        logger.info("[Hooks] T0->T2 skipped segment=%s reason=%s", segment_id, reason)
        return
    tenant_id_raw = ctx.metadata.get("tenant_id")
    try:
        metadata_tenant_id = uuid.UUID(str(tenant_id_raw)) if tenant_id_raw else None
    except (TypeError, ValueError) as exc:
        if required:
            raise ValueError("required T0->T2 projection has invalid tenant authority metadata") from exc
        metadata_tenant_id = None
        logger.warning("[Hooks] Ignoring invalid T0->T2 tenant metadata for agent=%s", agent_id)
    try:
        from app.services.tenant_resolver import resolve_tenant_for_agent

        tenant_id = await resolve_tenant_for_agent(agent_id)
        if tenant_id is None:
            raise LookupError(f"Agent tenant authority not found for {agent_id}")
        if metadata_tenant_id is not None and metadata_tenant_id != tenant_id:
            if required:
                raise ValueError("required T0->T2 tenant metadata disagrees with Agent authority")
            logger.warning("[Hooks] T0->T2 tenant metadata disagrees with Agent authority for agent=%s", agent_id)
    except Exception as exc:  # noqa: BLE001 - the durable job below preserves evidence for later repair
        if required:
            raise
        tenant_id = None
        logger.warning("[Hooks] T0->T2 tenant resolution failed for agent=%s: %s", agent_id, exc)
    try:
        from app.memory.t2.segment_package import enqueue_t2_segment_package_job

        lineage = _session_lineage_metadata(ctx)
        receipt = enqueue_t2_segment_package_job(
            data_root=_agent_data_root(),
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=str(ctx.session_id),
            t0_segment_id=segment_id,
            session_lineage=lineage,
        )
        logger.info(
            "[Hooks] T0->T2 job %s agent=%s session=%s segment=%s job=%s path=%s",
            receipt.status,
            agent_id,
            ctx.session_id,
            segment_id,
            receipt.job_id,
            receipt.package_dir,
        )
        accepted_statuses = {"queued", "running", "committed", "held", "not_applicable"}
        if required and receipt.status not in accepted_statuses:
            raise RuntimeError(f"required T0->T2 job was not durably accepted: {receipt.status}")
        if receipt.status == "queued":
            _schedule_t2_segment_package_job(
                data_root=_agent_data_root(),
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=str(ctx.session_id),
                t0_segment_id=segment_id,
                job_id=receipt.job_id,
                session_lineage=lineage,
            )
            # Give an immediately-completing job one event-loop turn without
            # waiting on model or provider I/O. Production work remains
            # detached from the terminal RuntimeTask claim.
            await asyncio.sleep(0)
    except Exception as exc:  # noqa: BLE001
        if required:
            raise
        logger.warning("[Hooks] T0->T2 package build failed for agent=%s segment=%s: %s", agent_id, segment_id, exc)


def _schedule_t2_segment_package_job(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: str,
    t0_segment_id: str,
    job_id: str,
    session_lineage: dict[str, object] | None,
) -> None:
    """Run a durably queued T2 job outside any user RuntimeTask fence."""

    key = f"{agent_id}:{job_id}"
    existing = _T2_BACKGROUND_TASKS.get(key)
    if existing is not None and not existing.done():
        return

    async def _run() -> None:
        from app.memory.t2.segment_package import run_t2_segment_package_job

        result = await run_t2_segment_package_job(
            data_root=data_root,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            t0_segment_id=t0_segment_id,
            job_id=job_id,
            session_lineage=dict(session_lineage or {}),
        )
        logger.info(
            "[Hooks] Detached T0->T2 job %s agent=%s session=%s segment=%s job=%s path=%s",
            result.status,
            agent_id,
            session_id,
            t0_segment_id,
            result.job_id,
            result.package_dir,
        )

    task = asyncio.create_task(
        _run(),
        name=f"t0-to-t2:{agent_id}:{job_id}",
        context=Context(),
    )
    _T2_BACKGROUND_TASKS[key] = task

    def _on_done(done_task: asyncio.Task[None]) -> None:
        if _T2_BACKGROUND_TASKS.get(key) is done_task:
            _T2_BACKGROUND_TASKS.pop(key, None)
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.info("[Hooks] Detached T0->T2 job cancelled; durable manifest remains job=%s", job_id)
        except Exception as exc:  # noqa: BLE001 - manifest + sweep own recovery.
            logger.warning("[Hooks] Detached T0->T2 job crashed job=%s: %s", job_id, exc)

    task.add_done_callback(_on_done)


def _t0_segment_t2_eligible(ctx: HookContext) -> tuple[bool, str]:
    """Return whether a sealed T0 segment should enter semantic T2 packaging."""

    explicit = ctx.metadata.get("semantic_memory_eligible")
    if explicit is not None:
        if isinstance(explicit, bool):
            return explicit, "explicit_semantic_memory_eligible" if explicit else "explicit_non_semantic"
        normalized = str(explicit).strip().lower()
        if normalized in {"0", "false", "no", "n", "off"}:
            return False, "explicit_non_semantic"
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True, "explicit_semantic"

    source = (ctx.source or "web").strip().lower()
    if source in {"heartbeat", "heartbeat_reflection", "dream", "distiller", "eval", "platform"}:
        return False, f"system_source:{source}"
    return True, f"source:{source or 'web'}"


async def _t0_trigger_end(ctx: HookContext) -> None:
    """TRIGGER_END → seal the already-written runtime transcript, then build T2."""
    if ctx.metadata.get(_REQUIRED_TURN_BOUNDARY_MARKER):
        return
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    logger.info("[Hooks] TRIGGER_END: agent=%s trigger=%s", ctx.agent_id, ctx.metadata.get("trigger_name", "?"))
    session_id, segment_id = _seal_existing_runtime_t0_segment(
        agent_id=agent_id,
        ctx=ctx,
        event_type="trigger_run",
        boundary_reason="trigger_end",
    )
    if segment_id:
        await _build_t2_for_sealed_segment(
            ctx=_with_t0_runtime_session(ctx, session_id),
            agent_id=agent_id,
            segment_id=segment_id,
        )


async def _t0_delegation_end(ctx: HookContext) -> None:
    """DELEGATION_END → seal the already-written runtime transcript, then build T2."""
    if ctx.metadata.get(_REQUIRED_TURN_BOUNDARY_MARKER):
        return
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    logger.info("[Hooks] DELEGATION_END: agent=%s", ctx.agent_id)
    session_id, segment_id = _seal_existing_runtime_t0_segment(
        agent_id=agent_id,
        ctx=ctx,
        event_type="delegation_run",
        boundary_reason="delegation_end",
    )
    if segment_id:
        await _build_t2_for_sealed_segment(
            ctx=_with_t0_runtime_session(ctx, session_id),
            agent_id=agent_id,
            segment_id=segment_id,
        )


async def _t0_heartbeat_tick_end(ctx: HookContext) -> None:
    """HEARTBEAT_TICK_END → append and seal a T0 runtime-session ledger."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    logger.info("[Hooks] HEARTBEAT_TICK_END: agent=%s", ctx.agent_id)
    _append_and_seal_runtime_t0_event(
        agent_id=agent_id,
        ctx=ctx,
        event_type="heartbeat_tick",
        boundary_reason="heartbeat_tick_end",
        fallback_content="heartbeat_tick_end",
    )


async def _evolution_maintenance_on_heartbeat(ctx: HookContext) -> None:
    """HEARTBEAT_TICK_END → schedule peripheral evolution maintenance."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    tenant_raw = ctx.metadata.get("tenant_id")
    tenant_id: uuid.UUID | None = None
    if tenant_raw:
        try:
            tenant_id = uuid.UUID(str(tenant_raw))
        except ValueError:
            logger.debug("[Hooks] Invalid heartbeat tenant_id for evolution maintenance: %s", tenant_raw)

    # Dream cadence evidence and admission are durable before slow peripheral
    # maintenance detaches from the hook boundary.
    try:
        from app.services.evolution_daemon import record_and_enqueue_heartbeat_dream

        await record_and_enqueue_heartbeat_dream(
            agent_id=agent_id,
            tenant_id=tenant_id,
            outcome_type=str(ctx.metadata.get("outcome") or ""),
        )
    except Exception as exc:
        logger.debug("[Hooks] Durable Dream admission failed for %s: %s", agent_id, exc)

    async def _run() -> None:
        try:
            from app.services.evolution_daemon import run_heartbeat_evolution_maintenance

            await run_heartbeat_evolution_maintenance(
                agent_id=agent_id,
                tenant_id=tenant_id,
                outcome_type=str(ctx.metadata.get("outcome") or ""),
                current_session_id=str(ctx.session_id or ctx.metadata.get("session_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Hooks] Evolution maintenance after heartbeat failed for %s: %s", agent_id, exc)

    asyncio.create_task(_run(), name=f"heartbeat_evolution_maintenance:{agent_id}")


async def _t0_dream_end(ctx: HookContext) -> None:
    """DREAM_END → append/seal a T0 runtime-session ledger + reset heartbeat session."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    logger.info("[Hooks] DREAM_END: agent=%s", ctx.agent_id)
    _append_and_seal_runtime_t0_event(
        agent_id=agent_id,
        ctx=ctx,
        event_type="dream_run",
        boundary_reason="dream_end",
        fallback_content="dream_end",
    )
    # Phase 5: Reset heartbeat KAIROS session after dream completes
    # so next heartbeat tick starts fresh with updated T3 memory.
    from app.services.heartbeat import _reset_heartbeat_session

    _reset_heartbeat_session(agent_id)


def _unique_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


async def _summarize_activation_feedback_on_turn_stop(ctx: HookContext) -> None:
    """TURN_STOP → summarize activation feedback before T0 sealing."""
    if ctx.metadata.get(_REQUIRED_TURN_BOUNDARY_MARKER):
        return
    from app.runtime.context import runtime_assembly_metadata

    raw_events = runtime_assembly_metadata(ctx.metadata).get("activation_events")
    events = (
        [dict(item) for item in raw_events if isinstance(item, dict)] if isinstance(raw_events, list | tuple) else []
    )
    if not events:
        return
    success_count = sum(1 for event in events if event.get("event_type") == "tool_success")
    failure_count = sum(1 for event in events if event.get("event_type") == "tool_failure")
    credits: list[float] = []
    for event in events:
        feedback = event.get("feedback") if isinstance(event.get("feedback"), dict) else {}
        try:
            credits.append(float(feedback.get("credit") or 0.0))
        except (TypeError, ValueError):
            credits.append(0.0)
    ctx.metadata["activation_feedback_summary"] = {
        "schema": "hive.ccplus.activation_feedback_summary.v1",
        "event_count": len(events),
        "tool_success_count": success_count,
        "tool_failure_count": failure_count,
        "credit_total": round(sum(credits), 6),
        "candidate_ids": _unique_text([str(event.get("candidate_id") or "") for event in events]),
        "query_ids": _unique_text([str(event.get("query_id") or "") for event in events]),
        "event_ids": _unique_text([str(event.get("event_id") or "") for event in events]),
    }


async def project_required_turn_boundary(ctx: HookContext) -> None:
    """Await the required terminal T0 projection without advisory hook semantics.

    The generic hook registry records handler failures and continues.  A claimed
    terminal-boundary outbox row must instead stay retryable until its stable
    run/turn boundary is sealed and the durable T2 job is accepted.
    """

    required_metadata = (
        "terminal_boundary_id",
        "terminal_boundary_idempotency_key",
        "runtime_task_id",
        "turn_id",
    )
    missing = [key for key in required_metadata if ctx.metadata.get(key) in (None, "")]
    if missing:
        raise ValueError(f"required terminal boundary metadata missing: {', '.join(missing)}")
    if ctx.event == HookEvent.TURN_STOP:
        await _summarize_activation_feedback_on_turn_stop(ctx)
        await _t0_turn_stop(ctx, required_t2=True)
        ctx.metadata[_REQUIRED_TURN_BOUNDARY_MARKER] = True
        return
    if ctx.event == HookEvent.TURN_ABORT:
        await _t0_turn_abort(ctx)
        ctx.metadata[_REQUIRED_TURN_BOUNDARY_MARKER] = True
        return
    raise ValueError("required terminal boundary projection only accepts TURN_STOP or TURN_ABORT")


async def _capture_pending_reply(ctx: HookContext) -> None:
    """POST_TOOL_USE → auto-capture pending reply context for outbound messages."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return

    tool_result = ctx.tool_result or ""
    # Skip failed tool calls — error results start with known failure prefixes
    if tool_result.startswith(("❌", "⚠️", "[Tool execution error]", "Blocked by hook")):
        return
    if not tool_result:
        return

    from app.services.pending_reply_service import (
        capture_pending_reply,
        extract_recipient_info,
    )

    if not extract_recipient_info(ctx.tool_name or "", ctx.tool_args or {}):
        return

    from app.channel_message_contracts import extract_sender_label_from_message

    # Extract originator info from conversation messages
    originator_name = ""
    originator_identity = ""
    messages = ctx.messages or []
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                originator_name = extract_sender_label_from_message(content) or originator_name
                # No fallback to agent_name — that's the bot, not the human originator

    try:
        from app.database import tenant_scoped_session
        from app.models.chat_session import ChatSession
        from app.services.pending_reply_service import sender_identity_from_session
        from app.services.tenant_resolver import resolve_tenant_for_agent
        from sqlalchemy import select

        # POST_TOOL_USE may fire from a daemon/background path with no request GUC.
        # Resolve the owning tenant so the SELECT and the pending_reply_contexts
        # INSERT survive the stage-3 non-owner role flip (a bare session
        # fail-closes → silently drops the captured reply context).
        tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as db:
            if ctx.session_id:
                try:
                    session_result = await db.execute(
                        select(ChatSession).where(ChatSession.id == uuid.UUID(str(ctx.session_id)))
                    )
                    session_obj = session_result.scalar_one_or_none()
                except Exception:
                    session_obj = None
                if session_obj:
                    if not originator_name:
                        delivery_target = getattr(session_obj, "delivery_target_json", None) or {}
                        originator_name = (
                            str(delivery_target.get("user_label") or delivery_target.get("username") or "").strip()
                            or originator_name
                        )
                    originator_identity = sender_identity_from_session(session_obj)
            await capture_pending_reply(
                db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                tool_name=ctx.tool_name or "",
                tool_args=ctx.tool_args or {},
                messages=messages,
                originator_name=originator_name,
                originator_identity=originator_identity,
            )
            await db.commit()
    except Exception as exc:
        logger.warning("[PendingReply] Failed to capture: %s", exc)


def register_memory_hooks() -> None:
    """Register all memory system hook handlers.

    Called from main.py lifespan during startup.
    Phase 0: logging-only for SESSION_START, POST_COMPACTION, MEMORY_EXTRACTED.
    Phase 1: T0 session-ledger boundaries and runtime-session ledger events.
    Phase 2: session projection for RESPONSE_COMPLETE/PRE_COMPACTION; canonical T0->T2 after seal.
    Phase 3: Pending reply capture for outbound messages.
    """
    from app.runtime import hooks as hooks_mod

    registry = hook_registry
    if registry is _DEFAULT_HOOK_REGISTRY and hooks_mod.hook_registry is not _DEFAULT_HOOK_REGISTRY:
        registry = hooks_mod.hook_registry

    registry.register_many(_MEMORY_HOOK_REGISTRATIONS)

    logger.info(
        "[Hooks] Memory hooks registered: %d handlers "
        "(3 log + 1 permission_denied_audit + 2 projection + 1 fast_reflection + 6 T0 + 1 evolution + 1 pending_reply)",
        len(_MEMORY_HOOK_REGISTRATIONS),
    )


def export_memory_hook_plan() -> list[dict[str, object]]:
    """Return the declarative memory-hook registration plan for observability."""
    return describe_registration_specs(_MEMORY_HOOK_REGISTRATIONS)


_MEMORY_HOOK_REGISTRATIONS = [
    # Loaded from the declarative config below via load_registration_specs().
]

_MEMORY_HOOK_HANDLERS = {
    "log_session_start": _log_session_start,
    "log_post_compaction": _log_post_compaction,
    "log_memory_extracted": _log_memory_extracted,
    "audit_permission_denied": _audit_permission_denied,
    "project_on_response": _project_on_response,
    "fast_reflection_on_response": _fast_reflection_on_response,
    "project_on_pre_compaction": _project_on_pre_compaction,
    "t0_turn_stop": _t0_turn_stop,
    "t0_turn_abort": _t0_turn_abort,
    "t0_session_close": _t0_session_close,
    "t0_session_idle": _t0_session_idle,
    "t0_trigger_end": _t0_trigger_end,
    "t0_delegation_end": _t0_delegation_end,
    "t0_heartbeat_tick_end": _t0_heartbeat_tick_end,
    "evolution_maintenance_on_heartbeat": _evolution_maintenance_on_heartbeat,
    "t0_dream_end": _t0_dream_end,
    "summarize_activation_feedback_on_turn_stop": _summarize_activation_feedback_on_turn_stop,
    "capture_pending_reply": _capture_pending_reply,
}

_MEMORY_HOOK_CONFIGURATION = [
    {"event": HookEvent.SESSION_START.value, "handler": "log_session_start", "key": "memory.session_start.log"},
    {
        "event": HookEvent.POST_COMPACTION.value,
        "handler": "log_post_compaction",
        "key": "memory.post_compaction.log",
    },
    {
        "event": HookEvent.MEMORY_EXTRACTED.value,
        "handler": "log_memory_extracted",
        "key": "memory.extracted.log",
    },
    {
        "event": HookEvent.PERMISSION_DENIED.value,
        "handler": "audit_permission_denied",
        "key": "governance.permission_denied.audit",
    },
    {
        "event": HookEvent.RESPONSE_COMPLETE.value,
        "handler": "project_on_response",
        "key": "memory.response_complete.session_projection",
    },
    {
        "event": HookEvent.RESPONSE_COMPLETE.value,
        "handler": "fast_reflection_on_response",
        "key": "memory.response_complete.fast_reflection",
    },
    {
        "event": HookEvent.PRE_COMPACTION.value,
        "handler": "project_on_pre_compaction",
        "key": "memory.pre_compaction.t0_checkpoint",
    },
    {
        "event": HookEvent.TURN_STOP.value,
        "handler": "summarize_activation_feedback_on_turn_stop",
        "key": "activation.turn_stop.feedback_summary",
    },
    {"event": HookEvent.TURN_STOP.value, "handler": "t0_turn_stop", "key": "memory.turn_stop.t0"},
    {"event": HookEvent.TURN_ABORT.value, "handler": "t0_turn_abort", "key": "memory.turn_abort.t0"},
    {"event": HookEvent.SESSION_CLOSE.value, "handler": "t0_session_close", "key": "memory.session_close.t0"},
    {"event": HookEvent.SESSION_IDLE.value, "handler": "t0_session_idle", "key": "memory.session_idle.t0"},
    {"event": HookEvent.TRIGGER_END.value, "handler": "t0_trigger_end", "key": "memory.trigger_end.t0"},
    {
        "event": HookEvent.DELEGATION_END.value,
        "handler": "t0_delegation_end",
        "key": "memory.delegation_end.t0",
    },
    {
        "event": HookEvent.HEARTBEAT_TICK_END.value,
        "handler": "t0_heartbeat_tick_end",
        "key": "memory.heartbeat_tick_end.t0",
    },
    {
        "event": HookEvent.HEARTBEAT_TICK_END.value,
        "handler": "evolution_maintenance_on_heartbeat",
        "key": "evolution.heartbeat_tick_end.maintenance",
    },
    {"event": HookEvent.DREAM_END.value, "handler": "t0_dream_end", "key": "memory.dream_end.t0"},
    {
        "event": HookEvent.POST_TOOL_USE.value,
        "handler": "capture_pending_reply",
        "key": "pending_reply.post_tool_use.capture",
        "tool_names": list(OUTBOUND_TOOL_NAMES),
    },
]

_MEMORY_HOOK_REGISTRATIONS = load_registration_specs(_MEMORY_HOOK_CONFIGURATION, _MEMORY_HOOK_HANDLERS)
