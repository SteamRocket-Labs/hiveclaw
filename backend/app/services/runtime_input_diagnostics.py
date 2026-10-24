"""Read-only mechanical source diagnostics for an operator-selected web run."""

import hashlib
import uuid

from sqlalchemy import select

from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionModelResult
from app.services.session_model_inputs import _hash, _stored_dependencies, load_context_message_origin, message_snapshot


def _source_ref(value):
    if not isinstance(value, str):
        return None
    kind, _, identity = value.partition(":")
    if kind not in {"session-input", "session-model-result", "session-tool-result", "session-tool-permission",
        "session-kernel-context", "session-compaction", "remote-chat-result"}:
        return None
    try:
        return f"{kind}:{uuid.UUID(identity)}"
    except ValueError:
        return None


def _model_view(result):
    raw_snapshot = result.model_request_snapshot_json
    snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    raw_bindings = snapshot.get("input_bindings")
    bindings = raw_bindings if isinstance(raw_bindings, dict) else {}
    request = snapshot.get("wire_request")
    wire = request.get("messages") if isinstance(request, dict) else []
    wire = wire if isinstance(wire, list) else []
    coverage = bindings.get("dependency_coverage")
    complete = coverage.get("complete") if isinstance(coverage, dict) else None
    entries = bindings.get("messages")
    incomplete = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            incomplete.append({"code": "binding_entry_format_unavailable", "source_ref": None})
            continue
        if entry.get("status") == "bound" and entry.get("dependency_status") == "resolved":
            continue
        index = entry.get("message_index")
        message = wire[index] if type(index) is int and 0 <= index < len(wire) else None
        incomplete.append({"message_index": index, "status": entry.get("status"), "code": entry.get("code"),
            "dependency_status": entry.get("dependency_status"), "source_ref": _source_ref(entry.get("source_ref")),
            "message_hash_matches": entry.get("message_hash") == _hash(message_snapshot(message))
                if isinstance(message, dict) else None})
    return {"model_result_id": str(result.id), "round_id": result.round_id, "state": result.state,
        "provider": snapshot.get("provider"), "model": snapshot.get("model"), "request_phase": snapshot.get("phase"),
        "request_hash_matches": isinstance(raw_snapshot, dict) and _hash(snapshot) == result.model_request_hash,
        "captured_binding_status": bindings.get("status"),
        "captured_dependency_coverage": {"complete": complete if type(complete) is bool else None},
        "incomplete_messages": incomplete}


def _message_shape(message):
    content = message.get("content")
    return {"role": message.get("role"), "content_type": type(content).__name__,
        "block_count": len(content) if isinstance(content, list) else None,
        "text_lengths": [len(block["text"]) for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)] if isinstance(content, list)
            else [len(content)] if isinstance(content, str) else [], "sha256": _hash(message_snapshot(message))}


def _context_view(ref, origin, origins, agent_id):
    from app.services.historical_model_inputs import _catalog_section

    def catalog_view(value):
        return {"available": value is not None, "chars": len(value) if value is not None else None,
            "sha256": hashlib.sha256(value.encode()).hexdigest() if value is not None else None}

    if origin is None:
        return {"source_ref": ref, "available": False}
    missing = origin["dependency_coverage"].get("unbound") or []
    paths = {value.removeprefix("skill_file_unbound:") for value in missing
        if value.startswith("skill_file_unbound:")}
    catalog = _catalog_section(origin)
    view = {"source_ref": ref, "available": True, "kind": origin.get("kind"),
        "message": _message_shape(origin["message"]),
        "dependencies_valid": _stored_dependencies(origin.get("dependencies")) is not None,
        "missing_skill_file_count": len(paths),
        "other_missing_count": sum(not value.startswith("skill_file_unbound:") for value in missing),
        "catalog": catalog_view(catalog), "selected_sections": [], "witnesses": []}
    for section in origin.get("sections") or []:
        if isinstance(section, dict) and section.get("selected") is True:
            view["selected_sections"].append({"is_skill_catalog": section.get("name") == "skill_catalog",
                **{key: section.get(key) if type(section.get(key)) is int else None
                    for key in ("render_order", "rendered_chars")}})
    for witness_ref, witness in origins:
        if witness.get("kind") != "dynamic_prompt":
            continue
        witness_catalog = _catalog_section(witness)
        public_paths = set()
        for leaf in _stored_dependencies(witness.get("dependencies")) or []:
            parts = leaf["source_id"].split("\n")
            if leaf["source_kind"] == "agent_platform_skill_file" and len(parts) == 3 and parts[0] == str(agent_id):
                public_paths.add(parts[1])
        view["witnesses"].append({"source_ref": witness_ref, "catalog": catalog_view(witness_catalog),
            "catalog_matches": catalog is not None and catalog == witness_catalog,
            "missing_skill_paths_covered": len(paths & public_paths)})
    return view


async def runtime_input_diagnostics(db, *, task):
    report = {"schema": "hive.runtime_input_sources.v1", "status": "unavailable",
        "source_authority": "not_evaluated", "models": [], "first_dependency_chain": []}
    if task.task_type != "web_chat_turn":
        return {**report, "reason_code": "task_type_not_supported"}
    try:
        session_id = uuid.UUID(str(task.parent_session_id))
    except (ValueError, TypeError):
        return {**report, "reason_code": "session_scope_missing"}
    scoped = await db.scalar(select(ChatSession.id).where(ChatSession.id == session_id,
        ChatSession.tenant_id == task.tenant_id, ChatSession.agent_id == task.parent_agent_id))
    if scoped is None:
        return {**report, "reason_code": "session_scope_unavailable"}
    rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.tenant_id == task.tenant_id,
        SessionModelResult.session_id == session_id, SessionModelResult.run_id == task.id)
        .order_by(SessionModelResult.round_id, SessionModelResult.id)))
    report.update(status="available" if rows else "empty", models=[_model_view(row) for row in rows])
    first = next((row for row, view in zip(rows, report["models"]) if not view["request_hash_matches"]
        or view["incomplete_messages"]), None)
    visited = set()
    while first is not None and first.id not in visited:
        visited.add(first.id)
        view = _model_view(first)
        report["first_dependency_chain"].append(view)
        if not view["request_hash_matches"] or not view["incomplete_messages"]:
            break
        ref = view["incomplete_messages"][0]["source_ref"]
        if not ref or not ref.startswith("session-model-result:"):
            break
        ancestor = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.id == uuid.UUID(ref.split(":", 1)[1]), SessionModelResult.tenant_id == task.tenant_id,
            SessionModelResult.session_id == session_id))
        if ancestor is None:
            report["chain_blocker"] = "source_model_unavailable"
            break
        if ancestor.id in visited:
            report["chain_blocker"] = "source_cycle"
            break
        first = ancestor
    if first is None or not report["first_dependency_chain"][-1]["request_hash_matches"]:
        return report
    wire = (first.model_request_snapshot_json.get("wire_request") or {}).get("messages") or []
    if not wire or not isinstance(wire[0], dict):
        return report
    from app.services.prompt_cache import match_cache_hint_presentation

    witnesses = {"model_result_id": str(first.id), "first_message": _message_shape(wire[0]),
        "complete_origins": 0, "exact_message_matches": 0, "cache_presentation_matches": 0, "origins": []}
    ids = list(await db.scalars(select(InvocationSpan.id).where(InvocationSpan.tenant_id == task.tenant_id,
        InvocationSpan.agent_id == task.parent_agent_id, InvocationSpan.session_id == str(session_id),
        InvocationSpan.span_type == "kernel_context", InvocationSpan.status == "captured",
        InvocationSpan.name.in_(("frozen_prompt", "dynamic_prompt")))
        .order_by(InvocationSpan.started_at, InvocationSpan.id)))
    origins = []
    for source_id in ids:
        origin = await load_context_message_origin(db, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
            session_id=session_id, source_id=source_id)
        if origin is None or origin["dependency_coverage"].get("complete") is not True:
            continue
        origins.append((f"session-kernel-context:{source_id}", origin))
        if origin.get("kind") != "frozen_prompt":
            continue
        exact = origin["message"] == wire[0]
        cached = match_cache_hint_presentation(origin["message"], wire[0]) is not None
        witnesses["complete_origins"] += 1
        witnesses["exact_message_matches"] += int(exact)
        witnesses["cache_presentation_matches"] += int(cached)
        witnesses["origins"].append({"source_ref": f"session-kernel-context:{source_id}",
            "message": _message_shape(origin["message"]), "exact_message_match": exact, "cache_match": cached})
    report["compiler_witnesses"] = witnesses
    context_refs = dict.fromkeys(entry["source_ref"]
        for entry in report["first_dependency_chain"][-1]["incomplete_messages"]
        if (entry.get("source_ref") or "").startswith("session-kernel-context:"))
    report["context_sources"] = []
    for ref in context_refs:
        origin = await load_context_message_origin(db, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
            session_id=session_id, source_id=uuid.UUID(ref.split(":", 1)[1]))
        report["context_sources"].append(_context_view(ref, origin, origins, task.parent_agent_id))
    return report
