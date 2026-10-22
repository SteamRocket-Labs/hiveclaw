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


async def capture_context_message_origins(db, *, tenant_id, agent_id, session_id, run_id, result_id, messages) -> None:
    """Persist only compiler-created origins, without declaring their leaves authorized."""
    from datetime import datetime, timezone
    from app.models.invocation_span import InvocationSpan
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import ModelRoundNeedsReconciliation

    if not any(isinstance(message, LLMMessage) and isinstance(message.context_origin, ContextMessageOrigin)
        for message in messages):
        return
    owner_id = await db.scalar(select(ChatSession.user_id).where(ChatSession.id == session_id,
        ChatSession.tenant_id == tenant_id, ChatSession.agent_id == agent_id))
    machine_actor = None
    if owner_id is None:
        from app.models.runtime_task import RuntimeTask
        from app.services.shared_group_authority import load_shared_group_runtime_actor

        session = await db.get(ChatSession, session_id)
        task = await db.get(RuntimeTask, run_id)
        if session is not None and session.audience_kind == "group" and task is not None:
            owner_id = (await load_shared_group_runtime_actor(db, task=task, session=session)).id
        elif task is not None and task.machine_credential_id is not None:
            from app.services.machine_runtime_authority import load_machine_runtime_actor

            machine_actor = await load_machine_runtime_actor(db, task=task, session=session)
    if owner_id is None and machine_actor is None:
        return
    for index, message in enumerate(messages):
        origin = message.context_origin if isinstance(message, LLMMessage) else None
        if not isinstance(origin, ContextMessageOrigin) or message.role not in {"system", "user"}:
            continue
        snapshot = message_snapshot(message)
        if _hash(snapshot) != origin.message_sha256:
            continue
        proof = {"schema": "hive.kernel_context_origin.v2", "tenant_id": str(tenant_id),
            "agent_id": str(agent_id), "session_id": str(session_id), "run_id": str(run_id),
            "model_result_id": str(result_id), "message_index": index, "kind": origin.kind,
            "message": snapshot, "sections": list(origin.sections), "dependencies": list(origin.sources),
            "dependency_coverage": {"complete": not origin.unbound, "unbound": list(origin.unbound)}}
        if machine_actor is not None:
            proof.update({'requester_external_principal_id': str(machine_actor.external_principal_id),
                'machine_credential_id': str(machine_actor.machine_credential_id)})
        proof["source_hash"] = _hash(proof)
        source_id = uuid.uuid5(result_id, f"kernel-context:{index}:{proof['source_hash']}")
        row = await db.get(InvocationSpan, source_id, populate_existing=True)
        if row is not None:
            if row.tenant_id != tenant_id or row.agent_id != agent_id or row.session_id != str(session_id) or (
                row.metadata_json or {}).get("context_origin") != proof:
                raise ModelRoundNeedsReconciliation("The captured context origin changed; reconcile before resuming.")
        else:
            db.add(InvocationSpan(id=source_id, tenant_id=tenant_id, agent_id=agent_id, user_id=owner_id,
                session_id=str(session_id), runtime_task_id=run_id, trace_id=f"session-model:{result_id}",
                span_id=f"kernel-context:{source_id}", span_type="kernel_context", name=origin.kind,
                status="captured", duration_ms=0, started_at=datetime.now(timezone.utc),
                metadata_json={"context_origin": proof}))
        message.source_ref = f"session-kernel-context:{source_id}"
    await db.flush()


async def load_context_message_origin(db, *, tenant_id, agent_id, source_id, session_id=None):
    from app.models.invocation_span import InvocationSpan

    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    proof = (row.metadata_json or {}).get("context_origin") if row is not None else None
    if row is None or row.tenant_id != tenant_id or row.agent_id != agent_id or row.span_type != "kernel_context" or row.status != "captured" or (
        session_id is not None and row.session_id != str(session_id)) or not isinstance(proof, dict):
        return None
    payload = {key: value for key, value in proof.items() if key != "source_hash"}
    coverage = proof.get("dependency_coverage")
    if proof.get("schema") == "hive.kernel_context_origin.v1":
        coverage_valid = coverage == {"complete": False, "reason_code": "context_sources_unbound"}
    elif proof.get("schema") == "hive.kernel_context_origin.v2" and isinstance(coverage, dict):
        missing = coverage.get("unbound")
        coverage_valid = (set(coverage) == {"complete", "unbound"} and isinstance(missing, list)
            and all(isinstance(value, str) and value for value in missing) and coverage.get("complete") is (not missing))
    else:
        coverage_valid = False
    if not coverage_valid or proof.get("tenant_id") != str(tenant_id) or (
        proof.get("agent_id") != str(agent_id) or proof.get("session_id") != row.session_id
        or proof.get("run_id") != str(row.runtime_task_id) or _hash(payload) != proof.get("source_hash")):
        return None
    if proof.get('machine_credential_id') is not None:
        from app.models.runtime_task import RuntimeTask

        task = await db.get(RuntimeTask, row.runtime_task_id, populate_existing=True)
        if (task is None or task.tenant_id != tenant_id or task.parent_agent_id != agent_id
            or task.parent_session_id != row.session_id or task.root_user_id is not None or row.user_id is not None
            or str(task.machine_credential_id) != proof['machine_credential_id']
            or str(task.root_external_principal_id) != proof.get('requester_external_principal_id')):
            return None
    message = proof.get("message")
    if not isinstance(message, dict) or set(message) != {"role", "content"} or message["role"] not in {"system", "user"} or not isinstance(message["content"], str):
        return None
    try:
        result_id = uuid.UUID(proof["model_result_id"])
        expected_id = uuid.uuid5(result_id, f"kernel-context:{proof['message_index']}:{proof['source_hash']}")
    except (KeyError, TypeError, ValueError):
        return None
    if row.id != expected_id or row.trace_id != f"session-model:{result_id}" or row.name != proof.get("kind"):
        return None
    return proof


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
        "source_authority": "not_evaluated", "messages": entries,
        "dependencies": [], "dependency_coverage": {"complete": False}}
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

    for ref in refs:
        if isinstance(ref, str) and ref.startswith("session-kernel-context:"):
            try:
                source_id = uuid.UUID(ref.split(":", 1)[1])
            except ValueError:
                continue
            origin = await load_context_message_origin(db, tenant_id=tenant_id, agent_id=agent_id,
                session_id=session_id, source_id=source_id)
            if origin is not None:
                canonical[ref] = origin["message"]

    for ref in refs:
        if isinstance(ref, str) and ref.startswith("session-compaction:"):
            try:
                source_id = uuid.UUID(ref.split(":", 1)[1])
            except ValueError:
                continue
            source = await load_compaction_source(db, tenant_id=tenant_id, agent_id=agent_id,
                session_id=session_id, source_id=source_id)
            if source is not None:
                canonical[ref] = source["message"]

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
    resolved = {}
    dependencies = []
    for entry in entries:
        if entry["status"] != "bound":
            entry["dependency_status"] = "incomplete"
            continue
        ref = entry["source_ref"]
        if ref not in resolved:
            resolved[ref] = await _origin_dependencies(db, tenant_id=tenant_id, agent_id=agent_id,
                ref=ref)
        refs, complete = resolved[ref]
        entry["dependencies"] = refs
        entry["dependency_status"] = "resolved" if complete else "incomplete"
        dependencies.extend(refs)
    manifest["dependencies"] = _unique_dependencies(dependencies)
    manifest["dependency_coverage"]["complete"] = bool(entries) and manifest["status"] == "resolved" and all(
        entry.get("dependency_status") == "resolved" for entry in entries)
    return manifest


def _unique_dependencies(values: list[dict[str, str]]) -> list[dict[str, str]]:
    return [dict(zip(("source_kind", "source_id", "source_version"), row)) for row in sorted({
        (v["source_kind"], v["source_id"], v["source_version"]) for v in values})]


def _stored_dependencies(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or any(not isinstance(row, dict)
        or set(row) != {"source_kind", "source_id", "source_version"}
        or any(not isinstance(v, str) or not v for v in row.values()) for row in value):
        return None
    return _unique_dependencies(value)


def _model_dependencies(result) -> tuple[list[dict[str, str]], bool]:
    if result is None or result.state not in {"sealed", "round_committed"}:
        return [], False
    snapshot = result.model_request_snapshot_json
    if not isinstance(snapshot, dict) or _hash(snapshot) != result.model_request_hash:
        return [], False
    bindings = snapshot.get("input_bindings")
    if not isinstance(bindings, dict) or bindings.get("schema") != "hive.model_input_bindings.v1":
        return [], False
    dependencies = _stored_dependencies(bindings.get("dependencies"))
    coverage = bindings.get("dependency_coverage")
    return dependencies or [], dependencies is not None and isinstance(coverage, dict) and coverage.get("complete") is True


async def load_compaction_source(db, *, tenant_id, agent_id, source_id, session_id=None):
    from app.models.invocation_span import InvocationSpan

    statement = select(InvocationSpan).where(InvocationSpan.tenant_id == tenant_id, InvocationSpan.agent_id == agent_id,
        InvocationSpan.span_id == f"compaction-source:{source_id}", InvocationSpan.span_type == "compaction",
        InvocationSpan.name == "compaction_checkpoint_installed", InvocationSpan.status == "ok")
    if session_id is not None:
        statement = statement.where(InvocationSpan.session_id == str(session_id))
    rows = (await db.scalars(statement.limit(2).execution_options(populate_existing=True))).all()
    if len(rows) != 1:
        return None
    source = (rows[0].metadata_json or {}).get("compaction_source")
    if not isinstance(source, dict) or source.get("schema") != "hive.compaction_source.v1" or source.get("source_id") != str(source_id):
        return None
    if not all(key in source for key in ("message", "inputs", "input_bindings")) or (
        source.get("source_hash") != _hash({key: source[key] for key in ("message", "inputs", "input_bindings")})
    ):
        return None
    return source


async def _expired_permission_result_bound(db, *, invocation, event, content):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_semantic_history import _event_payload

    receipt = f'session-permission:{invocation.permission_item_id}:expired'
    expected = {'schema': 'hive.tool_permission_result.v1', 'status': 'aborted',
        'reason_code': 'tool_permission_request_expired', 'permission_item_id': str(invocation.permission_item_id),
        'invocation_id': str(invocation.id)}
    try:
        if (json.loads(content) != expected or invocation.permission_state != 'expired'
            or invocation.effect_state != 'prepared_not_started' or invocation.permission_item_id is None
            or invocation.permission_receipt_ref != receipt or invocation.receipt_ref != receipt
            or event.id != invocation.result_event_id or event.tenant_id != invocation.tenant_id
            or event.session_id != invocation.session_id or event.run_id != invocation.run_id
            or event.invocation_id != invocation.id or event.item_kind != 'tool_result' or event.lifecycle != 'completed'
            or _event_payload(event).get('content_or_error_ref') != receipt):
            return False
    except (ValueError, TypeError):
        return False
    expired = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == invocation.tenant_id, ChatTranscriptEvent.agent_id == event.agent_id,
        ChatTranscriptEvent.session_id == invocation.session_id, ChatTranscriptEvent.run_id == invocation.run_id,
        ChatTranscriptEvent.result_id == event.result_id, ChatTranscriptEvent.invocation_id == invocation.id,
        ChatTranscriptEvent.item_id == invocation.permission_item_id, ChatTranscriptEvent.item_kind == 'tool_permission',
        ChatTranscriptEvent.lifecycle == 'cancelled').order_by(ChatTranscriptEvent.sequence).limit(1))
    payload = _event_payload(expired) if expired else {}
    return (payload.get('receipt_ref') == receipt and payload.get('reason_code') == 'tool_permission_request_expired'
        and payload.get('invocation_id') == str(invocation.id)
        and payload.get('permission_request_version') == invocation.permission_request_version
        and payload.get('provider_tool_use_id') == invocation.provider_tool_use_id)


async def _origin_dependencies(db, *, tenant_id, agent_id, ref):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionCommand
    from app.services.content_input_receipts import ContentInputReceiptError, load_workspace_input_evidence
    from app.services.knowledge_provenance import KNOWLEDGE_TOOL_NAMES, build_knowledge_provenance
    from app.services.session_semantic_history import _event_payload

    try:
        kind, raw_id = ref.split(":", 1)
        source_id = uuid.UUID(raw_id)
    except (ValueError, TypeError):
        return [], False
    if kind == "session-kernel-context":
        # Complete inventory is separate from the live leaf-authority readers.
        origin = await load_context_message_origin(db, tenant_id=tenant_id, agent_id=agent_id, source_id=source_id)
        dependencies = _stored_dependencies(origin.get("dependencies")) if origin is not None else None
        return dependencies or [], dependencies is not None and origin["dependency_coverage"]["complete"] is True
    if kind == "session-compaction":
        source = await load_compaction_source(db, tenant_id=tenant_id, agent_id=agent_id, source_id=source_id)
        catalog = source.get("input_bindings") if source is not None else None
        if not isinstance(catalog, dict) or catalog.get("schema") != "hive.model_input_bindings.v1":
            return [], False
        dependencies = _stored_dependencies(catalog.get("dependencies"))
        coverage = catalog.get("dependency_coverage")
        return dependencies or [], dependencies is not None and isinstance(coverage, dict) and coverage.get("complete") is True
    if kind == "session-input":
        row = await db.scalar(select(SessionTurnInput).join(SessionCommand, SessionCommand.id == SessionTurnInput.command_id)
            .join(ChatSession, ChatSession.id == SessionTurnInput.session_id).where(
                SessionTurnInput.tenant_id == tenant_id, SessionTurnInput.id == source_id,
                SessionCommand.tenant_id == tenant_id, SessionCommand.session_id == SessionTurnInput.session_id,
                ChatSession.tenant_id == tenant_id,
                ChatSession.agent_id == agent_id))
        command = await db.get(SessionCommand, row.command_id) if row is not None else None
        if command is None or command.request_hash != _hash({"command_kind": command.command_kind, "request": command.request_json}) or (
            command.target_hash != _hash(command.target_json)
        ):
            return [], False
        from app.services.trigger_instruction_sources import native_trigger_input_sources

        try:
            scheduled, complete = await native_trigger_input_sources(db, tenant_id=tenant_id, agent_id=agent_id, row=row, command=command)
        except ContentInputReceiptError:
            scheduled, complete = [], False
        from app.services.team_task_context import input_sources

        try:
            shared = await input_sources(db, tenant_id=tenant_id, row=row)
        except ContentInputReceiptError:
            shared, complete = [], False
        return [{"source_kind": "session_command", "source_id": str(command.id), "source_version": command.request_hash}, *scheduled, *shared], complete
    if kind == "remote-chat-result":
        from app.models.runtime_result import RuntimeResultObject

        result = await db.get(RuntimeResultObject, source_id, populate_existing=True)
        if result is None or result.tenant_id != tenant_id or result.source_kind != "remote_a2a":
            return [], False
        return [{"source_kind": "remote_a2a_result", "source_id": str(result.id),
            "source_version": result.sha256}], True
    if kind == "session-model-result":
        result = await db.scalar(select(SessionModelResult).join(ChatSession, ChatSession.id == SessionModelResult.session_id)
            .where(SessionModelResult.id == source_id, SessionModelResult.tenant_id == tenant_id,
                ChatSession.tenant_id == tenant_id, ChatSession.agent_id == agent_id))
        return _model_dependencies(result)
    if kind not in {"session-tool-result", "session-tool-permission"}:
        return [], False
    invocation = await db.scalar(select(SessionToolInvocation).join(ChatSession, ChatSession.id == SessionToolInvocation.session_id)
        .where(SessionToolInvocation.id == source_id, SessionToolInvocation.tenant_id == tenant_id,
            ChatSession.tenant_id == tenant_id, ChatSession.agent_id == agent_id))
    if invocation is None:
        return [], False
    if kind == "session-tool-permission":
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.session_id == invocation.session_id, SessionModelResult.run_id == invocation.run_id,
            SessionModelResult.round_id == invocation.round_id,
            SessionModelResult.provider_request_id == invocation.provider_request_id))
        dependencies, complete = _model_dependencies(model)
        waiting = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.tenant_id == tenant_id,
            ChatTranscriptEvent.agent_id == agent_id, ChatTranscriptEvent.session_id == invocation.session_id,
            ChatTranscriptEvent.run_id == invocation.run_id, ChatTranscriptEvent.invocation_id == invocation.id,
            ChatTranscriptEvent.result_id == (model.id if model else None),
            ChatTranscriptEvent.item_id == invocation.permission_item_id, ChatTranscriptEvent.item_kind == "tool_permission",
            ChatTranscriptEvent.lifecycle == "waiting").order_by(ChatTranscriptEvent.sequence).limit(1))
        request = _event_payload(waiting).get("permission_request") if waiting else None
        effective = invocation.effective_arguments_json
        valid = bool(model and waiting and isinstance(request, dict) and isinstance(effective, dict)
            and invocation.id == uuid.uuid5(model.id, f"tool-invocation:{invocation.provider_tool_use_id}")
            and invocation.args_hash == _hash({"tool_name": invocation.tool_name, "arguments": invocation.provider_arguments_json})
            and invocation.effective_args_hash == _hash({"tool_name": invocation.tool_name, "arguments": effective})
            and invocation.permission_authority_snapshot_hash == invocation.authority_snapshot_hash
            and request.get("permission_request_id") == str(invocation.permission_item_id)
            and request.get("runtime_task_id") == str(invocation.run_id)
            and request.get("session_id") == str(invocation.session_id)
            and request.get("tool_call_id") == invocation.provider_tool_use_id
            and request.get("tool_name") == invocation.tool_name and request.get("arguments") == effective)
        return dependencies, complete and valid
    if invocation.result_event_id is None:
        return [], False
    event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.id == invocation.result_event_id,
        ChatTranscriptEvent.tenant_id == tenant_id, ChatTranscriptEvent.agent_id == agent_id,
        ChatTranscriptEvent.session_id == invocation.session_id, ChatTranscriptEvent.run_id == invocation.run_id))
    if event is None:
        return [], False
    payload = _event_payload(event)
    content = payload.get("content")
    if not isinstance(content, str) or payload.get("content_hash") != _hash(content):
        return [], False
    model = await db.scalar(select(SessionModelResult).where(SessionModelResult.tenant_id == tenant_id,
        SessionModelResult.session_id == invocation.session_id, SessionModelResult.run_id == invocation.run_id,
        SessionModelResult.provider_request_id == invocation.provider_request_id))
    dependencies, inherited_complete = _model_dependencies(model)
    # The call's own audience remains a source, including empty searches and private query arguments.
    dependencies = [*dependencies, {"source_kind": "session_tool_invocation", "source_id": str(invocation.id),
        "source_version": payload["content_hash"]}]
    if invocation.permission_state == 'expired':
        valid = await _expired_permission_result_bound(db, invocation=invocation, event=event, content=content)
        return _unique_dependencies(dependencies), inherited_complete and valid
    from app.services.explicit_memory_tool_sources import MEMORY_RESULT_TOOLS, memory_result_sources

    if invocation.tool_name in {"save_skill", "reconcile_skill_candidate", "inspect_reviewed_skill"}:
        from app.services.learned_skill_sources import candidate_result_sources
        from app.services.skill_candidate_operations import operation_result_sources

        sources, complete = (await candidate_result_sources(db, tenant_id=tenant_id, agent_id=agent_id,
            invocation=invocation, content=content)) if invocation.tool_name == "save_skill" else ([], False)
        if not complete:
            sources, complete = await operation_result_sources(db, tenant_id=tenant_id, agent_id=agent_id,
                invocation=invocation, model=model, content=content)
        return _unique_dependencies([*dependencies, *sources]), inherited_complete and complete
    if invocation.tool_name in MEMORY_RESULT_TOOLS:
        sources, complete = await memory_result_sources(db, tenant_id=tenant_id, agent_id=agent_id,
            invocation=invocation, model=model, content_hash=payload["content_hash"])
        return _unique_dependencies([*dependencies, *sources]), inherited_complete and complete
    from app.services.feishu_read_receipts import FEISHU_RESULT_TOOLS, load_feishu_result

    if invocation.tool_name in FEISHU_RESULT_TOOLS and invocation.effect_state in {"effect_committed", "failed"}:
        receipt = await load_feishu_result(db, tenant_id=tenant_id, agent_id=agent_id, invocation=invocation,
            content_hash=payload["content_hash"])
        return _unique_dependencies(dependencies), inherited_complete and receipt is not None
    if invocation.tool_name == "read_project_resource":
        from app.services.project_context import project_failure_receipt, project_tool_receipt

        proof = await project_tool_receipt(db, invocation=invocation, payload=payload)
        if proof is not None:
            dependencies.append({"source_kind": "project_resource", "source_id": str(invocation.id),
                "source_version": payload["content_hash"]})
        return _unique_dependencies(dependencies), inherited_complete and (proof is not None or project_failure_receipt(payload))
    try:
        handoff = json.loads(content)
    except (ValueError, TypeError):
        handoff = None
    if isinstance(handoff, dict) and handoff.get("status") == "approval_required":
        from app.models.audit import ApprovalRequest
        from app.services.session_approval import invocation_enterprise_approval

        try:
            ticket = await db.get(ApprovalRequest, uuid.UUID(str(handoff.get("approval_id"))))
        except (ValueError, TypeError):
            ticket = None
        approval = await invocation_enterprise_approval(db, invocation=invocation, agent_id=agent_id,
            decision={"approval_id": str(ticket.id), "decision_id": ticket.decision_id},
            input_hash=invocation.effective_args_hash) if ticket else None
        return _unique_dependencies(dependencies), inherited_complete and approval is not None
    if invocation.permission_state == "denied":
        from app.models.session_v2 import SessionControlInput

        try:
            denial = json.loads(content)
            control = await db.get(SessionControlInput, uuid.UUID(denial["control_id"]))
            valid = (denial.get("schema") == "hive.tool_permission_result.v1" and denial.get("status") == "denied"
                and denial.get("invocation_id") == str(invocation.id) and control is not None
                and control.tenant_id == tenant_id and control.session_id == invocation.session_id
                and control.expected_run_id == invocation.run_id and control.request_item_id == invocation.permission_item_id
                and control.authority_snapshot_hash == invocation.permission_authority_snapshot_hash
                and control.kind == "permission_response" and control.status == "applied"
                and control.response_payload_json == {"decision": "deny"}
                and control.response_payload_hash == _hash(control.response_payload_json))
        except (ValueError, TypeError, KeyError):
            valid = False
        return _unique_dependencies(dependencies), inherited_complete and valid
    if invocation.tool_name == "ask_user_question":
        question = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.tenant_id == tenant_id, ChatTranscriptEvent.session_id == invocation.session_id,
            ChatTranscriptEvent.run_id == invocation.run_id, ChatTranscriptEvent.invocation_id == invocation.id,
            ChatTranscriptEvent.result_id == model.id, ChatTranscriptEvent.item_kind == "user_question",
            ChatTranscriptEvent.item_id == uuid.uuid5(invocation.id, "user-question"),
            ChatTranscriptEvent.lifecycle == "waiting").order_by(ChatTranscriptEvent.sequence).limit(1)) if model else None
        question_payload = dict((question.metadata_json or {}).get("v2_payload") or {}) if question else {}
        from app.models.runtime_task import RuntimeTask
        from app.services.session_user_question import question_requester_binding, question_requester_matches

        task = await db.get(RuntimeTask, invocation.run_id)
        requester_matches = task is None or task.machine_credential_id is None or question_requester_matches(task, question_payload)
        for field in question_requester_binding(task):
            question_payload.pop(field, None)
        try:
            complete = invocation.effect_state == "effect_committed" and bool(question) and requester_matches and question_payload == json.loads(content)
        except (ValueError, TypeError):
            complete = False
        return _unique_dependencies(dependencies), inherited_complete and complete
    if invocation.tool_name == "create_employee_from_request":
        from app.services.employee_creation_sources import creation_tool_result_bound

        complete = await creation_tool_result_bound(db, tenant_id=tenant_id, agent_id=agent_id, invocation=invocation, content=content)
        return _unique_dependencies(dependencies), inherited_complete and complete
    if invocation.tool_name == "read_context_resource":
        dependency = _channel_context_dependency(agent_id, content,
            invocation.effective_arguments_json or invocation.provider_arguments_json)
        return _unique_dependencies([*dependencies, *([dependency] if dependency else [])]), bool(dependency) and inherited_complete
    arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
    if invocation.tool_name in {"read_file", "read_document", "fs_read"} and "original_attachment_id" in arguments:
        from app.services.original_tool_sources import original_tool_result_sources

        sources, complete = await original_tool_result_sources(db, tenant_id=tenant_id, agent_id=agent_id,
            invocation=invocation, content_hash=payload["content_hash"])
        return _unique_dependencies([*dependencies, *sources]), inherited_complete and complete
    if invocation.tool_name == "read_runtime_result":
        from app.services.runtime_result_sources import runtime_result_tool_sources

        dependency = await runtime_result_tool_sources(db, agent_id=agent_id, invocation=invocation, content=content)
        return _unique_dependencies([*dependencies, *([dependency] if dependency else [])]), bool(dependency) and inherited_complete
    if invocation.tool_name in {"write_file", "fs_write"}:
        from app.services.content_input_receipts import load_workspace_execution_evidence

        arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
        try:
            _ref, hashes = await load_workspace_execution_evidence(db, agent_id=agent_id, invocation=invocation,
                model_result=model, arguments=arguments, content_hash=payload["content_hash"])
            return _unique_dependencies(dependencies), inherited_complete and arguments.get("path") in hashes
        except (ContentInputReceiptError, ValueError, TypeError):
            return _unique_dependencies(dependencies), False
    if invocation.tool_name in {"run_command", "execute_code", "office_document_create", "office_document_apply"}:
        from app.services.content_input_receipts import load_workspace_execution_input_evidence, load_workspace_execution_evidence

        try:
            arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
            _ref, sources = await load_workspace_execution_input_evidence(db, agent_id=agent_id, invocation=invocation,
                model_result=model, arguments=arguments,
                content_hash=payload["content_hash"])
            if invocation.tool_name in {"office_document_create", "office_document_apply"}:
                _ref, hashes = await load_workspace_execution_evidence(db, agent_id=agent_id, invocation=invocation,
                    model_result=model, arguments=arguments, content_hash=payload["content_hash"])
                if (arguments.get("output_path") or arguments.get("path")) not in hashes:
                    return _unique_dependencies(dependencies), False
            return _unique_dependencies([*dependencies, *sources]), inherited_complete
        except (ContentInputReceiptError, ValueError, TypeError):
            return _unique_dependencies(dependencies), False
    if invocation.tool_name == "list_triggers":
        from app.services.trigger_read_sources import trigger_read_result_sources

        sources, complete = await trigger_read_result_sources(db, tenant_id=tenant_id, agent_id=agent_id,
            invocation=invocation, content_hash=payload["content_hash"])
        return _unique_dependencies([*dependencies, *sources]), inherited_complete and complete
    if invocation.tool_name in {"set_trigger", "update_trigger", "schedule_wakeup"}:
        from app.services.trigger_context_sources import trigger_tool_result_sources

        sources, complete = await trigger_tool_result_sources(db, tenant_id=tenant_id, agent_id=agent_id, invocation=invocation)
        return _unique_dependencies([*dependencies, *sources]), inherited_complete and complete
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return _unique_dependencies(dependencies), False
    if invocation.tool_name in {"read_file", "read_document", "fs_read"}:
        from app.services.content_input_receipts import load_skill_input_evidence

        pointer = metadata.get("skill_input_receipt")
        if isinstance(pointer, dict) and pointer.get("sources"):
            try:
                bound = await load_skill_input_evidence(db, agent_id=agent_id, invocation=invocation, model_result=model,
                    arguments=invocation.effective_arguments_json or invocation.provider_arguments_json,
                    content_hash=payload["content_hash"], capture={"status": "captured", "receipt_ref": pointer.get("evidence_ref")})
                return _unique_dependencies([*dependencies, *bound["sources"]]), inherited_complete and (
                    bound == pointer and not bound["unbound"] and bool(bound["sources"]))
            except (ContentInputReceiptError, ValueError, TypeError):
                return _unique_dependencies(dependencies), False
    if invocation.tool_name in {"load_skill", "run_skill_tool", "tool_search"}:
        from app.services.content_input_receipts import load_skill_input_evidence

        pointer = metadata.get("skill_input_receipt")
        if not isinstance(pointer, dict) or pointer.get("status") != "verified":
            return _unique_dependencies(dependencies), False
        try:
            bound = await load_skill_input_evidence(db, agent_id=agent_id, invocation=invocation, model_result=model,
                arguments=invocation.effective_arguments_json or invocation.provider_arguments_json,
                content_hash=payload["content_hash"], capture={"status": "captured", "receipt_ref": pointer.get("evidence_ref")})
            if bound != pointer:
                return _unique_dependencies(dependencies), False
            return _unique_dependencies([*dependencies, *bound["sources"]]), inherited_complete and (
                bool(bound["sources"]) or invocation.tool_name == "tool_search" or bound.get("control_only") is True) and not bound["unbound"]
        except (ContentInputReceiptError, ValueError, TypeError):
            return _unique_dependencies(dependencies), False
    pointer = metadata.get("workspace_input_receipt")
    if isinstance(pointer, dict):
        if pointer.get("status") != "verified":
            return _unique_dependencies(dependencies), False
        try:
            bound = await load_workspace_input_evidence(db, agent_id=agent_id, invocation=invocation,
                model_result=model, arguments=invocation.effective_arguments_json or invocation.provider_arguments_json,
                content_hash=payload["content_hash"], capture={"status": "captured", "receipt_ref": pointer.get("evidence_ref")})
            if bound != pointer:
                return _unique_dependencies(dependencies), False
            receipt = await db.get(ContentInputReceipt, uuid.UUID(bound["content_input_receipt_id"]))
            inherited = _stored_dependencies(receipt.dependencies_json)
            if inherited is None:
                return _unique_dependencies(dependencies), False
            # Keep upstream refs behind their exact carrier's audience and recursive source reader.
            dependencies.append({"source_kind": receipt.carrier_kind, "source_id": str(receipt.id),
                "source_version": receipt.content_sha256})
            return _unique_dependencies(dependencies), inherited_complete
        except (ContentInputReceiptError, ValueError, TypeError, AttributeError):
            return _unique_dependencies(dependencies), False
    if invocation.tool_name not in KNOWLEDGE_TOOL_NAMES:
        return _unique_dependencies(dependencies), False
    provenance = build_knowledge_provenance(invocation.tool_name, content)
    if not isinstance(provenance, dict) or metadata.get("knowledge_provenance") != provenance:
        return _unique_dependencies(dependencies), False
    complete = provenance.get("status") == "ok" and (provenance.get("coverage") or {}).get("complete") is True
    for source in provenance.get("sources") or []:
        version = source.get("source_version")
        source_id = source.get("publication_id") or source.get("document_id")
        if not source_id or not isinstance(version, str) or len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
            complete = False
            continue
        try:
            source_id = str(uuid.UUID(str(source_id)))
        except ValueError:
            complete = False
            continue
        dependencies.append({"source_kind": "company_knowledge_publication" if source.get("publication_id") else "knowledge_document",
            "source_id": source_id, "source_version": version})
    return _unique_dependencies(dependencies), complete and inherited_complete


def _channel_context_dependency(agent_id, content, arguments):
    # Other context resources still need their own upstream source and audience readers.
    try:
        page = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(page, dict) or set(page) != {"schema", "status", "ref", "context_ref", "source_ref",
        "sha256", "offset", "next_offset", "complete", "total_chars", "content"} or not isinstance(arguments, dict):
        return None
    body = page["content"]
    if (arguments.get("ref") != "channels" or page["schema"] != "hive.agent_context_resource_page.v1"
        or page["status"] != "ok" or page["ref"] != "channels" or page["complete"] is not True
        or page["context_ref"] != "agent-context://channels" or page["source_ref"] != "agent-context://channels"
        or not isinstance(body, str) or any(type(page[key]) is not int for key in ("offset", "next_offset", "total_chars"))
        or page["offset"] != 0 or page["next_offset"] != len(body) or page["total_chars"] != len(body)
        or page["sha256"] != hashlib.sha256(body.encode("utf-8")).hexdigest()):
        return None
    return {"source_kind": "agent_context_resource", "source_id": f"{agent_id}\nchannels", "source_version": page["sha256"]}
