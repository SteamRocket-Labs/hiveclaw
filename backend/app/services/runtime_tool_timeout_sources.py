"""Witness a native wait timeout without asserting that its tool had no effects."""

from datetime import datetime, timezone
import math
import uuid

from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.session_model_inputs import _hash

_KIND = "native_tool_timeout"


def _timeout_text(tool_name, seconds, *, legacy_format=False):
    from app.tools.result_envelope import render_tool_error

    return render_tool_error(
        tool_name=tool_name,
        error_class="timeout",
        message=f"{tool_name} exceeded the {int(seconds)} second time limit.",
        provider="runtime",
        retryable=legacy_format,
        actionable_hint=(
            "Try a simpler request, smaller input, or a more targeted operation."
            if legacy_format
            else "The execution outcome is unknown. Inspect the original tool's state or read back its output "
            "before requesting another effect."
        ),
    )


def _timeout_content_hash_matches(tool_name, seconds, content_hash):
    # Older receipts bind the exact former text. Validate those immutable
    # bytes without emitting their retry suggestion for a new execution.
    return any(
        _hash(_timeout_text(tool_name, seconds, legacy_format=legacy)) == content_hash for legacy in (False, True)
    )


def _unavailable():
    return ContentInputReceiptError(
        "native_tool_timeout_unverified", "Reconcile the original native timeout observation.", 503
    )


async def capture_native_tool_timeout(request, *, content, timeout_seconds):
    from app.services.explicit_memory_authority import _writer_input, audience_for_session

    seconds = float(timeout_seconds)
    if not math.isfinite(seconds) or seconds <= 0 or content != _timeout_text(request.tool_name, seconds):
        raise _unavailable()
    context = request.context
    async with tenant_scoped_session(context.tenant_id) as db:
        _tenant, _principal, actor, session, call, model, _dependencies, _args = await _writer_input(
            db, request, require_sources=False
        )
        proof = {
            "schema": "hive.native_tool_timeout.v1",
            "invocation_id": str(call.id),
            "model_result_id": str(model.id),
            "model_request_hash": model.model_request_hash,
            "actor_id": str(actor.id),
            "audience": audience_for_session(session),
            "tool_name": call.tool_name,
            "args_hash": call.effective_args_hash or call.args_hash,
            "authority_hash": call.authority_snapshot_hash,
            "content_hash": _hash(content),
            "timeout_seconds": seconds,
            "effect_outcome": "unknown",
        }
        identity = uuid.uuid5(call.id, _KIND)
        row = await db.get(InvocationSpan, identity)
        if row is None:
            db.add(
                InvocationSpan(
                    id=identity,
                    tenant_id=call.tenant_id,
                    agent_id=context.agent_id,
                    user_id=actor.id,
                    session_id=str(session.id),
                    runtime_task_id=call.run_id,
                    trace_id=f"session-tool:{call.id}",
                    span_id=f"native-timeout:{identity}",
                    span_type=_KIND,
                    name=call.tool_name,
                    status="captured",
                    duration_ms=0,
                    started_at=datetime.now(timezone.utc),
                    metadata_json={_KIND: proof},
                )
            )
            await db.commit()
        elif row.metadata_json != {_KIND: proof}:
            raise _unavailable()
    return {"status": "captured", "receipt_ref": f"{_KIND}:{identity}"}


async def load_native_tool_timeout_evidence(db, *, agent_id, invocation, model_result, content_hash, capture):
    from app.services.explicit_memory_authority import audience_for_session

    identity = uuid.uuid5(invocation.id, _KIND)
    row = await db.get(InvocationSpan, identity, populate_existing=True)
    proof = (row.metadata_json or {}).get(_KIND) if row else None
    session = await db.get(ChatSession, invocation.session_id, populate_existing=True)
    valid = (
        isinstance(capture, dict)
        and capture.get("status") == "captured"
        and capture.get("receipt_ref") == f"{_KIND}:{identity}"
        and isinstance(proof, dict)
        and proof.get("schema") == "hive.native_tool_timeout.v1"
        and row.tenant_id == invocation.tenant_id
        and row.agent_id == agent_id
        and row.session_id == str(invocation.session_id)
        and row.runtime_task_id == invocation.run_id
        and row.span_type == _KIND
        and row.name == invocation.tool_name
        and row.status == "captured"
        and proof.get("invocation_id") == str(invocation.id)
        and proof.get("tool_name") == invocation.tool_name
        and proof.get("actor_id") == str(row.user_id)
        and proof.get("content_hash") == content_hash
        and proof.get("args_hash") == (invocation.effective_args_hash or invocation.args_hash)
        and proof.get("authority_hash") == invocation.authority_snapshot_hash
        and proof.get("effect_outcome") == "unknown"
        and invocation.effect_state in {"effect_started", "failed", "needs_reconciliation"}
        and model_result is not None
        and model_result.tenant_id == invocation.tenant_id
        and model_result.session_id == invocation.session_id
        and model_result.run_id == invocation.run_id
        and model_result.provider_request_id == invocation.provider_request_id
        and model_result.state in {"sealed", "round_committed"}
        and proof.get("model_result_id") == str(model_result.id)
        and proof.get("model_request_hash") == model_result.model_request_hash
        and _hash(model_result.model_request_snapshot_json) == model_result.model_request_hash
        and invocation.id == uuid.uuid5(model_result.id, f"tool-invocation:{invocation.provider_tool_use_id}")
        and session is not None
        and session.tenant_id == invocation.tenant_id
        and session.agent_id == agent_id
        and (session.audience_kind not in {None, "private"} or session.user_id == row.user_id)
    )
    try:
        seconds = float(proof["timeout_seconds"]) if valid else 0
        valid = (
            valid
            and math.isfinite(seconds)
            and seconds > 0
            and _timeout_content_hash_matches(invocation.tool_name, seconds, content_hash)
            and proof.get("audience") == audience_for_session(session)
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise _unavailable()
    return {
        "schema": "hive.native_tool_timeout_receipt.v1",
        "status": "verified",
        "evidence_ref": f"{_KIND}:{identity}",
        "content_hash": content_hash,
        "effect_outcome": "unknown",
    }


async def native_tool_timeout_bound(db, *, agent_id, invocation, model_result=None, payload=None):
    if invocation.effect_state not in {"failed", "needs_reconciliation"}:
        return False
    from sqlalchemy import select
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionModelResult
    from app.services.session_semantic_history import _event_payload

    if payload is None:
        event = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.id == invocation.result_event_id,
                ChatTranscriptEvent.tenant_id == invocation.tenant_id,
                ChatTranscriptEvent.agent_id == agent_id,
                ChatTranscriptEvent.session_id == invocation.session_id,
                ChatTranscriptEvent.run_id == invocation.run_id,
                ChatTranscriptEvent.invocation_id == invocation.id,
            )
        )
        payload = _event_payload(event) if event else {}
    pointer = (payload.get("metadata") or {}).get("native_tool_timeout_receipt")
    if (
        not isinstance(pointer, dict)
        or pointer.get("status") != "verified"
        or payload.get("outcome") != "failed"
        or invocation.effect_state not in {"failed", "needs_reconciliation"}
        or not isinstance(payload.get("content"), str)
        or _hash(payload["content"]) != payload.get("content_hash")
    ):
        return False
    if model_result is None:
        model_result = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.tenant_id == invocation.tenant_id,
                SessionModelResult.session_id == invocation.session_id,
                SessionModelResult.run_id == invocation.run_id,
                SessionModelResult.provider_request_id == invocation.provider_request_id,
            )
        )
    try:
        return pointer == await load_native_tool_timeout_evidence(
            db,
            agent_id=agent_id,
            invocation=invocation,
            model_result=model_result,
            content_hash=payload["content_hash"],
            capture={"status": "captured", "receipt_ref": pointer.get("evidence_ref")},
        )
    except (ContentInputReceiptError, ValueError, TypeError):
        return False
