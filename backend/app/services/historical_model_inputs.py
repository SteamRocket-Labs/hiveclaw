"""Read-only reconstruction of compiler gaps in immutable model requests."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from sqlalchemy import select


_RESOLUTION = ContextVar("historical_model_input_resolution", default=None)


@contextmanager
def source_resolution():
    existing = _RESOLUTION.get()
    if existing is not None:
        yield existing
        return
    token = _RESOLUTION.set({"active": set(), "models": {}, "origins": {}, "recovered": {}, "contexts": {}})
    try:
        yield _RESOLUTION.get()
    finally:
        _RESOLUTION.reset(token)


async def _complete_origins(db, *, tenant_id, agent_id, session_id):
    from app.models.invocation_span import InvocationSpan
    from app.services.session_model_inputs import load_context_message_origin

    state = _RESOLUTION.get()
    key = (tenant_id, agent_id, str(session_id))
    if key not in state["origins"]:
        ids = (await db.scalars(select(InvocationSpan.id).where(InvocationSpan.tenant_id == tenant_id,
            InvocationSpan.agent_id == agent_id, InvocationSpan.session_id == str(session_id),
            InvocationSpan.span_type == "kernel_context", InvocationSpan.status == "captured")
            .order_by(InvocationSpan.started_at, InvocationSpan.id))).all()
        origins = []
        for source_id in ids:
            proof = await load_context_message_origin(db, tenant_id=tenant_id, agent_id=agent_id,
                session_id=session_id, source_id=source_id)
            if proof is not None and proof["dependency_coverage"]["complete"] is True:
                origins.append((f"session-kernel-context:{source_id}", proof))
        state["origins"][key] = origins
    return state["origins"][key]


def _catalog_section(origin):
    """Read exact selected bytes using the compiler's recorded lengths and order."""
    from app.kernel.engine import _dynamic_suffix_notice

    message = origin.get("message") or {}
    prefix = _dynamic_suffix_notice("x").content[:-1]
    content = message.get("content")
    sections = origin.get("sections")
    if origin.get("kind") != "dynamic_prompt" or message.get("role") != "user" or (
        not isinstance(content, str) or not content.startswith(prefix) or not isinstance(sections, list)):
        return None
    selected = [section for section in sections if isinstance(section, dict) and section.get("selected") is True]
    if any(type(section.get("render_order")) is not int or type(section.get("rendered_chars")) is not int
        or section["rendered_chars"] <= 0 for section in selected):
        return None
    if len({section["render_order"] for section in selected}) != len(selected):
        return None
    body, cursor, catalog = content[len(prefix):], 0, None
    for section in sorted(selected, key=lambda item: item["render_order"]):
        end = cursor + section["rendered_chars"]
        if end > len(body):
            return None
        if section.get("name") == "skill_catalog":
            if catalog is not None:
                return None
            catalog = body[cursor:end]
        cursor = end
        if cursor < len(body):
            if body[cursor:cursor + 2] != "\n\n":
                return None
            cursor += 2
    return catalog if cursor == len(body) else None


async def recover_context_dependencies(db, *, tenant_id, agent_id, origin, dependencies):
    from app.services.session_model_inputs import _stored_dependencies, _unique_dependencies

    missing = origin["dependency_coverage"].get("unbound")
    if not missing or any(not value.startswith("skill_file_unbound:") for value in missing):
        return dependencies, False
    catalog = _catalog_section(origin)
    if catalog is None:
        return dependencies, False
    paths = {value.removeprefix("skill_file_unbound:") for value in missing}
    for ref, witness in await _complete_origins(db, tenant_id=tenant_id, agent_id=agent_id, session_id=origin["session_id"]):
        if _catalog_section(witness) != catalog:
            continue
        leaves = _stored_dependencies(witness.get("dependencies"))
        if leaves is None:
            continue
        # Only the publicly packaged defaults have an independent source here.
        # Matching a private registry description does not recover its old file.
        public = [leaf for leaf in leaves if leaf["source_kind"] == "agent_platform_skill_file"
            and len(leaf["source_id"].split("\n")) == 3 and leaf["source_id"].split("\n")[0] == str(agent_id)
            and leaf["source_id"].split("\n")[1] in paths]
        if {leaf["source_id"].split("\n")[1] for leaf in public} == paths:
            _RESOLUTION.get()["contexts"][origin["source_hash"]] = {"original_context_hash": origin["source_hash"],
                "witness_ref": ref, "witness_hash": witness["source_hash"], "recovered_paths": sorted(paths)}
            return _unique_dependencies([*dependencies, *public]), True
    return dependencies, False


async def _reconciled_commit_ref(db, *, tenant_id, agent_id, result):
    """Recognize only the canonical old committed-then-ambiguous-prepare shape."""
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_semantic_history import _event_payload

    if (result.state != "needs_reconciliation"
        or result.reconciliation_owner != "session_model_round:ambiguous_prepare"
        or not isinstance(result.seal_json, dict) or not result.seal_json
        or result.round_committed_event_id is None or result.tenant_id != tenant_id):
        return None
    event = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.id == result.round_committed_event_id, ChatTranscriptEvent.tenant_id == tenant_id,
        ChatTranscriptEvent.agent_id == agent_id, ChatTranscriptEvent.session_id == result.session_id,
        ChatTranscriptEvent.run_id == result.run_id, ChatTranscriptEvent.result_id == result.id,
        ChatTranscriptEvent.item_id == result.id, ChatTranscriptEvent.schema_version == 2,
        ChatTranscriptEvent.item_kind == "result_commit", ChatTranscriptEvent.lifecycle == "round_committed"))
    if event is None or (event.scope_json or {}).get("round_id") != result.round_id:
        return None
    payload = _event_payload(event)
    response_ref = result.seal_json.get("provider_response_ref")
    if (payload.get("provider_request_id") != result.provider_request_id
        or not isinstance(response_ref, str) or not response_ref or payload.get("provider_response_ref") != response_ref):
        return None
    return f"session-event:{event.id}"


async def recover_model_dependencies(db, *, tenant_id, agent_id, result):
    from app.services.session_model_inputs import (
        _hash, _model_dependencies, _model_snapshot_dependencies, _stored_dependencies, _unique_dependencies,
        build_model_input_bindings, message_snapshot,
    )
    from app.services.prompt_cache import match_cache_hint_presentation

    original, complete = _model_dependencies(result)
    commit_ref = await _reconciled_commit_ref(db, tenant_id=tenant_id, agent_id=agent_id,
        result=result) if result is not None and result.state == "needs_reconciliation" else None
    if commit_ref is not None:
        original, complete = _model_snapshot_dependencies(result)
        if complete and _RESOLUTION.get() is not None:
            _RESOLUTION.get()["recovered"][str(result.id)] = {"model_result_id": str(result.id),
                "model_request_hash": result.model_request_hash, "canonical_commit_ref": commit_ref}
    if complete or result is None or (result.state not in {"sealed", "round_committed"} and commit_ref is None):
        return original, complete
    snapshot = result.model_request_snapshot_json
    if not isinstance(snapshot, dict) or _hash(snapshot) != result.model_request_hash:
        return original, False
    bindings = snapshot.get("input_bindings")
    request = snapshot.get("wire_request")
    wire = request.get("messages") if isinstance(request, dict) else None
    entries = bindings.get("messages") if isinstance(bindings, dict) else None
    if not isinstance(bindings, dict) or bindings.get("schema") != "hive.model_input_bindings.v1" or not isinstance(wire, list) or (
        not wire or not isinstance(entries, list) or len(wire) != len(entries)
        or _stored_dependencies(bindings.get("dependencies")) is None
        or bindings.get("dependency_coverage") != {"complete": False}):
        return original, False
    state = _RESOLUTION.get()
    key = (tenant_id, agent_id, result.id, result.model_request_hash)
    if key in state["models"]:
        return state["models"][key]
    if key in state["active"]:
        return original, False
    state["active"].add(key)
    try:
        messages = []
        for index, (message, entry) in enumerate(zip(wire, entries)):
            if not isinstance(message, dict) or not isinstance(entry, dict) or entry.get("message_index") != index or (
                entry.get("message_hash") != _hash(message_snapshot(message))):
                return original, False
            selected = deepcopy(message)
            if entry.get("status") == "bound" and isinstance(entry.get("source_ref"), str):
                selected["source_ref"] = entry["source_ref"]
            elif index == 0 and message.get("role") == "system" and entry.get("code") == "source_origin_missing":
                matches = []
                for ref, proof in await _complete_origins(db, tenant_id=tenant_id, agent_id=agent_id,
                    session_id=result.session_id):
                    if proof["kind"] == "frozen_prompt" and (proof["message"] == message or
                        match_cache_hint_presentation(proof["message"], message) is not None):
                        matches.append(ref)
                if matches:
                    selected["source_ref"] = matches[0]
            elif message.get("role") == "assistant" and entry.get("code") == "source_content_mismatch":
                from app.models.chat_transcript_event import ChatTranscriptEvent
                from app.services.session_semantic_history import load_session_semantic_history

                prepared_sequence = await db.scalar(select(ChatTranscriptEvent.sequence).where(
                    ChatTranscriptEvent.tenant_id == tenant_id, ChatTranscriptEvent.agent_id == agent_id,
                    ChatTranscriptEvent.session_id == result.session_id, ChatTranscriptEvent.result_id == result.id,
                    ChatTranscriptEvent.item_kind == "result_commit", ChatTranscriptEvent.lifecycle == "prepared",
                    ChatTranscriptEvent.content_hash == result.model_request_hash).order_by(ChatTranscriptEvent.sequence).limit(1))
                history = await load_session_semantic_history(db, tenant_id=tenant_id, agent_id=agent_id,
                    session_id=result.session_id)
                matches = []
                for item in history.messages:
                    if item.role != "assistant" or prepared_sequence is None or item.sequence_end >= prepared_sequence:
                        continue
                    canonical = message_snapshot(item)
                    if item.thinking is not None:
                        canonical["reasoning_content"] = item.thinking
                    if item.thinking_signature is not None:
                        canonical["reasoning_signature"] = item.thinking_signature
                    if match_cache_hint_presentation(canonical, message) is not None:
                        matches.append(item.id)
                if len(matches) == 1:
                    selected["source_ref"] = matches[0]
            messages.append(selected)
        reconstructed = await build_model_input_bindings(db, tenant_id=tenant_id, agent_id=agent_id,
            session_id=result.session_id, messages=messages, wire_messages=wire)
        complete = reconstructed["dependency_coverage"]["complete"] is True
        dependencies = _unique_dependencies([*original, *reconstructed["dependencies"]])
        state["models"][key] = dependencies, complete
        if complete:
            state["recovered"][str(result.id)] = {"model_result_id": str(result.id),
                "model_request_hash": result.model_request_hash,
                "messages": reconstructed["messages"], "dependencies": dependencies}
            if commit_ref is not None:
                state["recovered"][str(result.id)]["canonical_commit_ref"] = commit_ref
        return dependencies, complete
    finally:
        state["active"].remove(key)
