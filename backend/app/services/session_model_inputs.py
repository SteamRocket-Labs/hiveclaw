"""Bind selected model messages to canonical origins without granting source access."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.models.session_v2 import SessionModelResult, SessionToolInvocation, SessionTurnInput


def message_snapshot(message: Any) -> dict[str, Any]:
    fields = ("role", "content", "tool_calls", "tool_call_id", "reasoning_content", "reasoning_signature")
    result = {}
    for key in fields:
        value = message.get(key) if isinstance(message, dict) else getattr(message, key, None)
        if value is not None and (key in {"role", "content"} or value):
            result[key] = value
    return result


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


async def build_model_input_bindings(
    db: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, session_id: uuid.UUID,
    messages: list[Any], wire_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    from app.services.session_human_input import input_parts_to_runtime_messages
    from app.services.session_semantic_history import (
        SessionSemanticHistoryUnavailable, load_session_semantic_history,
    )
    from app.services.session_user_question import answer_projections

    refs = [m.get("source_ref") if isinstance(m, dict) else getattr(m, "source_ref", None) for m in messages]
    entries = [{"message_index": i, "message_hash": _hash(message_snapshot(m)),
        "status": "unresolved", "code": "source_origin_missing"} for i, m in enumerate(wire_messages)]
    manifest = {"schema": "hive.model_input_bindings.v1", "status": "incomplete",
        "source_authority": "not_evaluated", "messages": entries}
    if not any(isinstance(ref, str) and ref for ref in refs):
        return manifest
    session = await db.scalar(select(ChatSession.id).where(ChatSession.id == session_id,
        ChatSession.tenant_id == tenant_id, ChatSession.agent_id == agent_id))
    if session is None:
        for entry in entries:
            entry["code"] = "source_scope_mismatch"
        return manifest

    # Projection groups include sibling tool events; only the selected message's
    # canonical id is an origin. Group event ids are never dependency claims.
    try:
        history = await load_session_semantic_history(db, tenant_id=tenant_id,
            agent_id=agent_id, session_id=session_id)
    except SessionSemanticHistoryUnavailable:
        for entry in entries:
            entry["code"] = "canonical_source_unavailable"
        return manifest
    canonical = {}
    for message in history.messages:
        value = message_snapshot(message)
        if message.thinking is not None:
            value["reasoning_content"] = message.thinking
        if message.thinking_signature is not None:
            value["reasoning_signature"] = message.thinking_signature
        canonical[message.id] = message_snapshot(value)

    input_ids = []
    for ref in refs:
        if isinstance(ref, str) and ref.startswith("session-input:"):
            try:
                input_ids.append(uuid.UUID(ref.removeprefix("session-input:")))
            except ValueError:
                pass
    inputs = list((await db.scalars(select(SessionTurnInput).where(
        SessionTurnInput.tenant_id == tenant_id, SessionTurnInput.session_id == session_id,
        SessionTurnInput.id.in_(input_ids), SessionTurnInput.status.in_(["bound", "applied"]),
    ))).all()) if input_ids else []
    input_commands = {f"session-input:{row.id}": str(row.command_id) for row in inputs}
    for message in input_parts_to_runtime_messages(inputs, answer_projections=await answer_projections(db, inputs)):
        canonical.setdefault(f"session-input:{message['session_input_id']}", message_snapshot(message))

    provider_ids = {ref.split(":", 1)[1] for ref in refs if isinstance(ref, str)
        and ref.startswith(("session-provider-result:", "session-provider-tool-result:"))}
    results = list((await db.scalars(select(SessionModelResult).where(
        SessionModelResult.tenant_id == tenant_id, SessionModelResult.session_id == session_id,
        SessionModelResult.provider_request_id.in_(provider_ids),
    ))).all()) if provider_ids else []
    result_refs = {row.provider_request_id: f"session-model-result:{row.id}" for row in results}
    invocations = list((await db.scalars(select(SessionToolInvocation).where(
        SessionToolInvocation.tenant_id == tenant_id, SessionToolInvocation.session_id == session_id,
        SessionToolInvocation.provider_request_id.in_(provider_ids),
    ))).all()) if provider_ids else []
    tool_refs = {(row.provider_request_id, row.provider_tool_use_id): f"session-tool-result:{row.id}"
        for row in invocations}

    for i, entry in enumerate(entries):
        if i >= len(messages) or message_snapshot(messages[i]) != message_snapshot(wire_messages[i]):
            entry["code"] = "source_wire_mismatch"
            continue
        ref = refs[i]
        if not isinstance(ref, str) or not ref:
            continue
        if ref.startswith("session-provider-result:"):
            ref = result_refs.get(ref.split(":", 1)[1])
        elif ref.startswith("session-provider-tool-result:"):
            ref = tool_refs.get((ref.split(":", 1)[1], wire_messages[i].get("tool_call_id")))
        if not ref or ref not in canonical:
            entry["code"] = "source_origin_unavailable"
        elif canonical[ref] != message_snapshot(wire_messages[i]):
            entry["code"] = "source_content_mismatch"
        else:
            entry.update(status="bound", code="canonical_origin_matched", source_ref=ref)
            if ref in input_commands:
                entry["command_ref"] = f"session-command:{input_commands[ref]}"
    if entries and len(messages) == len(entries) and all(e["status"] == "bound" for e in entries):
        manifest["status"] = "resolved"
    return manifest
