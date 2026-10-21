"""Deliver scheduled work through the selected local executor and its real receipts."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.local_agent_channel import LocalAgentChannelMessage, LocalAgentChannelSession
from app.models.runtime_budget import RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.services.content_input_receipts import ContentInputReceiptError, _authorize_session_input_source, _validate_input_dependencies, group_source_read
from app.services.personal_knowledge_access import AgentRuntimePrincipal
from app.services.session_model_inputs import _hash


def _unavailable(code="local_trigger_origin_unavailable", status=409):
    return HTTPException(status, detail={"code": code})


def _principal(wrapper, session_id):
    return AgentRuntimePrincipal(agent_id=wrapper.parent_agent_id, requester_user_id=wrapper.root_user_id,
        session_id=str(session_id), runtime_task_id=str(wrapper.id), purpose="autonomous_agent", autonomous=True)


async def require_local_trigger_executor(db, *, agent, triggers):
    from app.services.local_agent_channel_service import active_snapshot_for_agent
    from app.services.local_bridge_service import require_local_agent_capability_policy
    from app.services.trigger_preflight import collect_trigger_runtime_options

    options = collect_trigger_runtime_options(triggers)
    unsupported = [key for key in ("workdir", "allowed_tool_names", "excluded_tool_names") if options.get(key)]
    if any((trigger.config or {}).get("model_id") for trigger in triggers):
        unsupported.append("model_id")
    if unsupported:
        raise HTTPException(409, detail={"code": "local_agent_trigger_control_unavailable", "unsupported_controls": unsupported})
    host = agent.owner_user_id or agent.creator_id
    if agent.agent_type != "local_agent" or host is None:
        raise _unavailable("local_agent_executor_unavailable")
    channel, snapshot = await active_snapshot_for_agent(db, tenant_id=agent.tenant_id, owner_user_id=host,
        agent_id=agent.id, capability="execute")
    await require_local_agent_capability_policy(db, tenant_id=agent.tenant_id, agent_id=agent.id,
        capability="local_agent.execute")
    return {"execution_target": "local_agent", "channel_id": str(channel.id),
        "capability_snapshot_hash": snapshot.snapshot_hash, "runtime_kind": channel.runtime_kind,
        "usage": {"status": "unknown", "tokens": None, "cost_usd": None, "budget_scope": "platform_dispatch"}}


async def _budget(db, wrapper, trigger_ids, *, for_effect):
    budget = await db.get(RuntimeBudgetRun, wrapper.budget_run_id, populate_existing=True) if wrapper.budget_run_id else None
    if (budget is None or budget.tenant_id != wrapper.tenant_id or budget.root_agent_id != wrapper.parent_agent_id
        or budget.root_user_id != wrapper.root_user_id
        or budget.root_runtime_task_id != (wrapper.root_runtime_task_id or wrapper.id)):
        raise _unavailable("local_trigger_budget_unavailable")
    if for_effect:
        from app.services.runtime_budget_service import RuntimeBudgetPolicyLookup, RuntimeBudgetService

        if budget.status not in {"active", "resuming"} or (budget.expires_at and budget.expires_at <= datetime.now(timezone.utc)):
            raise _unavailable("local_trigger_budget_inactive")
        policy = await RuntimeBudgetService().resolve_policy(RuntimeBudgetPolicyLookup(
            tenant_id=wrapper.tenant_id, source="trigger", profile=budget.profile, agent_id=wrapper.parent_agent_id,
            trigger_id=trigger_ids[0] if len(trigger_ids) == 1 else None))
        # External model/tool usage is unknown. An explicit requirement for a
        # control the bridge cannot enforce cannot be satisfied by prompt text.
        required = set((policy.policy_json or {}).get("local_agent_required_controls") or [])
        required.update(((budget.policy_snapshot or {}).get("policy_json") or {}).get("local_agent_required_controls") or [])
        unsupported = required - {"dispatch_count", "recipient", "approval", "idempotency"}
        if unsupported:
            raise HTTPException(409, detail={"code": "local_agent_budget_control_unavailable",
                "unsupported_controls": sorted(unsupported)})
        if policy.enforcement_mode == "enforce" and policy.max_background_tasks is not None and policy.max_background_tasks <= 0:
            raise _unavailable("local_trigger_dispatch_budget_exhausted")
    return budget


async def _context_entries(db, *, wrapper, session_id, refs):
    from app.services.native_trigger_context import _messages, _source_session
    from app.services.session_model_inputs import build_model_input_bindings, message_snapshot

    principal = _principal(wrapper, session_id)
    entries = []
    for ref in refs:
        source = await _source_session(db, wrapper, ref)
        await _authorize_session_input_source(db, wrapper.tenant_id, source, user=None, runtime_request=principal)
        messages = await _messages(db, wrapper, source)
        wire = [message_snapshot(message) for message in messages]
        bindings = await build_model_input_bindings(db, tenant_id=wrapper.tenant_id, agent_id=wrapper.parent_agent_id,
            session_id=source.id, messages=messages, wire_messages=wire)
        if messages and not bindings["dependency_coverage"]["complete"]:
            raise _unavailable("local_trigger_context_incomplete")
        with group_source_read():
            await _validate_input_dependencies(db, wrapper.tenant_id, bindings["dependencies"], runtime_request=principal)
        entries.append({"session_id": str(source.id), "title": source.title or str(source.id),
            "messages": [{"source_ref": message["source_ref"], "message": snapshot} for message, snapshot in zip(messages, wire)],
            "dependencies": bindings["dependencies"]})
    return entries


async def enqueue_local_trigger(db, *, agent, user, session, wrapper_id, triggers, content, reflection):
    from app.services.local_agent_channel_service import enqueue_channel_message
    from app.services.native_trigger_context import _render
    from app.services.trigger_context_sources import capture_trigger_context
    from app.services.trigger_daemon import _trigger_child_run_id
    from app.services.trigger_input_authority import authorize_trigger_origin
    from app.services.trigger_preflight import collect_trigger_runtime_options

    wrapper = await db.get(RuntimeTask, uuid.UUID(str(wrapper_id)), populate_existing=True)
    if (wrapper is None or wrapper.task_type != "trigger" or wrapper.tenant_id != agent.tenant_id
        or wrapper.parent_agent_id != agent.id or wrapper.root_user_id != user.id or session.user_id != user.id
        or session.agent_id != agent.id or session.tenant_id != agent.tenant_id or wrapper.status != "running"):
        raise _unavailable()
    await require_local_trigger_executor(db, agent=agent, triggers=triggers)
    trigger_ids = [trigger.id for trigger in triggers]
    await authorize_trigger_origin(db, wrapper=wrapper, trigger_ids=trigger_ids)
    budget = await _budget(db, wrapper, trigger_ids, for_effect=True)
    await _authorize_session_input_source(db, agent.tenant_id, session, user=None, runtime_request=_principal(wrapper, session.id))
    host = agent.owner_user_id or agent.creator_id
    local = await db.scalar(select(LocalAgentChannelSession).where(
        LocalAgentChannelSession.tenant_id == agent.tenant_id, LocalAgentChannelSession.owner_user_id == host,
        LocalAgentChannelSession.source_agent_id == agent.id, LocalAgentChannelSession.chat_session_id == session.id,
        LocalAgentChannelSession.status == "active").order_by(LocalAgentChannelSession.created_at, LocalAgentChannelSession.id).limit(1))
    if local is None:
        local = LocalAgentChannelSession(id=_trigger_child_run_id(wrapper.id, effect="local_session", discriminator=str(session.id)),
            tenant_id=agent.tenant_id, owner_user_id=host, source_agent_id=agent.id, chat_session_id=session.id,
            source="trigger", status="active")
        db.add(local)
        await db.flush()
    key = f"trigger-local:{wrapper.id}:{session.id}"
    existing = await db.scalar(select(LocalAgentChannelMessage).where(
        LocalAgentChannelMessage.tenant_id == agent.tenant_id, LocalAgentChannelMessage.idempotency_key == key))
    if existing is not None:
        proof = await load_local_trigger_message(db, message=existing, require_terminal=False)
        if proof is None or proof["session_id"] != str(session.id):
            raise _unavailable()
        await db.commit()
        return str(existing.id)
    options = collect_trigger_runtime_options(triggers)
    references = [await capture_trigger_context(db, trigger=trigger, session_id=session.id, runtime_task_id=wrapper.id)
        for trigger in triggers]
    if any(ref is None for ref in references):
        raise _unavailable("local_trigger_sources_unavailable")
    entries = await _context_entries(db, wrapper=wrapper, session_id=session.id, refs=options["context_from"])
    if entries:
        content += "\n\n" + _render({"entries": entries})
    payload = await enqueue_channel_message(db, session_id=local.id, owner_user_id=host, sender_user_id=user.id,
        sender_agent_id=agent.id, content=content, metadata={"source": "trigger", "local_trigger_wrapper_id": str(wrapper.id)},
        idempotency_key=key, action_source="trigger", commit=False)
    message = await db.get(LocalAgentChannelMessage, uuid.UUID(payload["id"]))
    identity = uuid.uuid5(wrapper.id, f"local-trigger-input:{message.id}")
    proof = {"schema": "hive.local_trigger_input.v1", "wrapper_id": str(wrapper.id), "message_id": str(message.id),
        "session_id": str(session.id), "local_session_id": str(local.id), "request_hash": message.request_hash,
        "content_sha256": _hash(message.content), "attachments_sha256": _hash(message.attachments_json),
        "trigger_ids": list(map(str, trigger_ids)), "configuration_sources": references,
        "context_refs": options["context_from"], "context_entries": entries,
        "delivery": "reflection" if reflection else "same_session", "budget_run_id": str(budget.id),
        "authority_generations": wrapper.metadata_json.get("trigger_authority_generations")}
    db.add(InvocationSpan(id=identity, tenant_id=agent.tenant_id, agent_id=agent.id, user_id=user.id,
        session_id=str(session.id), runtime_task_id=wrapper.id, trace_id=f"trigger:{wrapper.id}",
        span_id=f"local-trigger-input:{message.id}", span_type="local_trigger_input", name="scheduled_input",
        status="captured", duration_ms=0, started_at=datetime.now(timezone.utc), metadata_json={"local_trigger_input": proof}))
    budget.metadata_json = {**dict(budget.metadata_json or {}), "external_execution": {
        "target": "local_agent", "usage_status": "unknown", "tokens": None, "cost_usd": None,
        "budget_scope": "platform_dispatch", "message_id": str(message.id)}}
    wrapper.metadata_json = {**dict(wrapper.metadata_json or {}), "local_trigger_delivery_prepared": {
        "message_id": str(message.id), "session_id": str(session.id)}}
    await db.commit()
    return str(message.id)


async def recover_local_trigger_delivery(db, *, wrapper):
    """Recover the committed local input; never repeat an unconfirmed effect."""
    from app.models.trigger import AgentTrigger
    from app.services.trigger_daemon import _deliver_batch_to_source_session

    prepared = (wrapper.metadata_json or {}).get("local_trigger_delivery_prepared") or {}
    try:
        message = await db.get(LocalAgentChannelMessage, uuid.UUID(prepared["message_id"]))
        if message is None:
            raise _unavailable()
        proof = await load_local_trigger_message(db, message=message, require_terminal=False)
        if (proof is None or proof["wrapper_id"] != str(wrapper.id) or proof["session_id"] != prepared["session_id"]
            or set(proof["trigger_ids"]) != set(wrapper.metadata_json.get("trigger_ids") or [])):
            raise _unavailable()
        triggers = list(await db.scalars(select(AgentTrigger).where(AgentTrigger.tenant_id == wrapper.tenant_id,
            AgentTrigger.agent_id == wrapper.parent_agent_id, AgentTrigger.id.in_([uuid.UUID(value) for value in proof["trigger_ids"]]))))
        await db.commit()
        return await _deliver_batch_to_source_session(wrapper.parent_agent_id, triggers,
            source_session_id=proof["session_id"], runtime_task_id=str(wrapper.id),
            reflection=proof["delivery"] == "reflection", wake_content=message.content)
    except (ValueError, KeyError, TypeError) as exc:
        raise _unavailable() from exc


async def load_local_trigger_message(db, *, message, for_effect=True, require_terminal=True):
    pointer = (message.metadata_json or {}).get("local_trigger_wrapper_id")
    if pointer is None:
        return None
    from app.services.trigger_input_authority import authorize_trigger_origin

    try:
        wrapper_id = uuid.UUID(pointer)
        wrapper = await db.get(RuntimeTask, wrapper_id, populate_existing=True)
        span = await db.get(InvocationSpan, uuid.uuid5(wrapper_id, f"local-trigger-input:{message.id}"), populate_existing=True)
        proof = (span.metadata_json or {}).get("local_trigger_input") if span else None
        local = await db.get(LocalAgentChannelSession, message.session_id, populate_existing=True)
        session = await db.get(ChatSession, local.chat_session_id, populate_existing=True) if local else None
        if (wrapper is None or span is None or not isinstance(proof, dict) or session is None
            or wrapper.task_type != "trigger" or wrapper.tenant_id != message.tenant_id
            or wrapper.parent_agent_id != message.source_agent_id or wrapper.root_user_id != message.sender_user_id
            or message.sender_agent_id != message.source_agent_id or local.tenant_id != message.tenant_id
            or local.source_agent_id != message.source_agent_id or local.owner_user_id != message.owner_user_id
            or session.user_id != wrapper.root_user_id or session.tenant_id != message.tenant_id or session.agent_id != message.source_agent_id
            or span.tenant_id != message.tenant_id or span.agent_id != message.source_agent_id
            or span.user_id != wrapper.root_user_id or span.runtime_task_id != wrapper.id or span.session_id != str(session.id)
            or span.span_type != "local_trigger_input" or span.status != "captured"
            or proof.get("schema") != "hive.local_trigger_input.v1" or proof.get("wrapper_id") != str(wrapper.id)
            or proof.get("message_id") != str(message.id) or proof.get("session_id") != str(session.id)
            or proof.get("local_session_id") != str(local.id) or proof.get("request_hash") != message.request_hash
            or proof.get("content_sha256") != _hash(message.content) or proof.get("attachments_sha256") != _hash(message.attachments_json)
            or proof.get("authority_generations") != wrapper.metadata_json.get("trigger_authority_generations")
            or message.idempotency_key != f"trigger-local:{wrapper.id}:{session.id}"):
            raise _unavailable()
        trigger_ids = [uuid.UUID(value) for value in proof["trigger_ids"]]
        if (not trigger_ids or len(set(trigger_ids)) != len(trigger_ids)
            or not set(map(str, trigger_ids)).issubset(set(wrapper.metadata_json.get("trigger_ids") or []))
            or len(proof["configuration_sources"]) != len(trigger_ids)):
            raise _unavailable()
        if proof["delivery"] == "reflection" and (session.runtime_task_id != wrapper.id
            or session.session_kind != "trigger_run" or session.runtime_source != "trigger"):
            raise _unavailable()
        if require_terminal:
            if wrapper.status in {"pending", "running", "resumable"}:
                raise _unavailable("local_trigger_delivery_pending")
            if (wrapper.status != "completed" or wrapper.metadata_json.get("delivery") != proof["delivery"]
                or wrapper.metadata_json.get("local_message_id") != str(message.id)
                or wrapper.metadata_json.get("source_session_id") != str(session.id)
                or any((wrapper.metadata_json.get("trigger_settlement_overrides") or {}).get(str(key)) != "success" for key in trigger_ids)):
                raise _unavailable()
        await _budget(db, wrapper, trigger_ids, for_effect=for_effect)
        if for_effect:
            from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason, resolve_live_effect_principal
            from app.services.native_trigger_context import _messages, _source_session
            from app.services.session_model_inputs import message_snapshot

            agent = await db.get(Agent, message.source_agent_id, populate_existing=True)
            if agent is None or agent.agent_type != "local_agent" or (agent.owner_user_id or agent.creator_id) != message.owner_user_id:
                raise _unavailable("local_agent_executor_changed", 403)
            if (get_agent_lifecycle_block_reason(agent) or agent.status in {"expired", "stopped", "error", "archived"}
                or (await resolve_live_effect_principal(db, agent)).block_reason):
                raise _unavailable("local_agent_executor_unavailable", 403)
            await authorize_trigger_origin(db, wrapper=wrapper, trigger_ids=trigger_ids)
            principal = _principal(wrapper, session.id)
            await _authorize_session_input_source(db, wrapper.tenant_id, session, user=None, runtime_request=principal)
            with group_source_read():
                await _validate_input_dependencies(db, wrapper.tenant_id, proof["configuration_sources"], runtime_request=principal)
            if len(proof["context_refs"]) != len(proof["context_entries"]):
                raise _unavailable()
            for ref, entry in zip(proof["context_refs"], proof["context_entries"]):
                source = await _source_session(db, wrapper, ref)
                if str(source.id) != entry["session_id"]:
                    raise _unavailable()
                await _authorize_session_input_source(db, wrapper.tenant_id, source, user=None, runtime_request=principal)
                current = {item["source_ref"]: message_snapshot(item) for item in await _messages(db, wrapper, source, require_complete=False)}
                if any(current.get(item["source_ref"]) != item["message"] for item in entry["messages"]):
                    raise _unavailable("local_trigger_context_changed", 403)
                with group_source_read():
                    await _validate_input_dependencies(db, wrapper.tenant_id, entry["dependencies"], runtime_request=principal)
        return proof
    except ContentInputReceiptError as exc:
        raise _unavailable(exc.code, exc.status_code) from exc
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise _unavailable() from exc
