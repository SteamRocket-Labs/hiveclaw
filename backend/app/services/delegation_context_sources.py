"""Versioned runtime facts for delegated framing and permission configuration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.session_model_inputs import _hash, _model_dependencies, _origin_dependencies, _unique_dependencies


PERMISSION_INPUT_KEYS = ("permission_profile", "approval_policy", "permission_mode", "network_access",
    "network_policy", "allowed_tools", "writable_roots", "denied_reads", "denied_actions",
    "request_permission_tool_enabled", "plan_mode", "skill_execution_plans", "pending_skill_handoffs")


def _permission_inputs(metadata):
    return {key: deepcopy(metadata.get(key)) for key in PERMISSION_INPUT_KEYS}


@dataclass(frozen=True)
class DelegationContextSource:
    reference: dict
    permission_inputs: dict
    allowed_tool_names: tuple[str, ...]

    def matches(self, metadata, allowed_tool_names):
        return self.permission_inputs == _permission_inputs(metadata) and self.allowed_tool_names == tuple(allowed_tool_names)


@dataclass(frozen=True)
class NativeRecoveryContextSource:
    checkpoint: dict
    sources: tuple
    unbound: tuple

    def matches(self, result, session):
        return (result is not None and result.loaded and result.authority is not None
            and self.checkpoint == {"authority_hash": result.authority.digest,
                "manifest_hash": result.envelope_sha256, "session_id": str(session.session_id)})


async def _recovery_inputs(db, *, task, provider_request_id):
    model = await db.scalar(select(SessionModelResult).where(SessionModelResult.tenant_id == task.tenant_id,
        SessionModelResult.run_id == task.id, SessionModelResult.session_id == uuid.UUID(task.child_session_id),
        SessionModelResult.provider_request_id == provider_request_id, SessionModelResult.state == "round_committed"))
    sources, complete = _model_dependencies(model) if model else ([], False)
    tools = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.tenant_id == task.tenant_id,
        SessionToolInvocation.run_id == task.id, SessionToolInvocation.provider_request_id == provider_request_id).order_by(SessionToolInvocation.provider_tool_use_id)))
    for tool in tools:
        refs, bound = await _origin_dependencies(db, tenant_id=task.tenant_id, agent_id=task.child_agent_id,
            ref=f"session-tool-result:{tool.id}")
        sources.extend(refs)
        complete = complete and bound and tool.effect_state == "effect_committed"
    return model, _unique_dependencies(sources), complete


async def capture_native_recovery_context(db, *, task, session, provider_request_id):
    checkpoint = (session.metadata or {}).get("_native_recovery_checkpoint")
    if (not isinstance(checkpoint, dict) or set(checkpoint) != {"authority_hash", "manifest_hash", "session_id"}
        or checkpoint["session_id"] != str(uuid.UUID(task.child_session_id))):
        return
    model, sources, complete = await _recovery_inputs(db, task=task, provider_request_id=provider_request_id)
    if model is None:
        return
    proof = {"schema": "hive.native_recovery_context.v1", "checkpoint": checkpoint,
        "model_result_id": str(model.id), "model_request_hash": model.model_request_hash,
        "provider_request_id": provider_request_id, "sources": sources, "complete": complete}
    digest = _hash(proof)
    source_id = uuid.uuid5(task.id, f"native-recovery-context:{digest}")
    row = await db.get(InvocationSpan, source_id)
    if row is None:
        db.add(InvocationSpan(id=source_id, tenant_id=task.tenant_id, agent_id=task.child_agent_id,
            user_id=task.root_user_id, session_id=str(uuid.UUID(task.child_session_id)), runtime_task_id=task.id,
            trace_id=f"session-run:{task.id}", span_id=f"native-recovery:{source_id}", span_type="native_recovery_context",
            name="recovery_input_sources", status="captured", duration_ms=0, started_at=datetime.now(timezone.utc),
            metadata_json={"native_recovery_context": proof}))
    elif row.metadata_json != {"native_recovery_context": proof}:
        return
    task.metadata_json = {**(task.metadata_json or {}), "native_recovery_context_source": {"id": str(source_id), "hash": digest}}
    session.metadata["_native_recovery_context_source"] = NativeRecoveryContextSource(checkpoint, tuple(sources),
        () if complete else ("native_recovery_inputs_incomplete",))
    await db.flush()


async def bind_native_recovery_context(db, *, task, session):
    session.metadata.pop("_native_recovery_context_source", None)
    pointer = (task.metadata_json or {}).get("native_recovery_context_source")
    if not isinstance(pointer, dict):
        return
    try:
        source_id = uuid.UUID(pointer["id"])
    except (ValueError, KeyError, TypeError):
        return
    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    proof = (row.metadata_json or {}).get("native_recovery_context") if row else None
    if (row is None or row.tenant_id != task.tenant_id or row.agent_id != task.child_agent_id
        or row.user_id != task.root_user_id or row.session_id != str(uuid.UUID(task.child_session_id))
        or row.runtime_task_id != task.id or row.span_type != "native_recovery_context" or row.status != "captured"
        or not isinstance(proof, dict) or proof.get("schema") != "hive.native_recovery_context.v1"
        or _hash(proof) != pointer.get("hash") or source_id != uuid.uuid5(task.id, f"native-recovery-context:{pointer['hash']}")):
        return
    model, sources, complete = await _recovery_inputs(db, task=task, provider_request_id=proof.get("provider_request_id"))
    if (model is None or str(model.id) != proof.get("model_result_id") or model.model_request_hash != proof.get("model_request_hash")
        or sources != proof.get("sources") or complete is not proof.get("complete")):
        return
    session.metadata["_native_recovery_context_source"] = NativeRecoveryContextSource(proof["checkpoint"], tuple(sources),
        () if complete else ("native_recovery_inputs_incomplete",))


async def _facts(db, task):
    from app.agents.orchestrator import _build_delegated_worker_prompt, _permission_profile_mode, _resolve_delegation_tool_profile

    metadata = task.metadata_json or {}
    if not isinstance(metadata, dict) or not task.child_session_id or not task.parent_session_id:
        return None
    authority = metadata.get("authority_snapshot")
    receipt = metadata.get("execution_receipt") or {}
    if not isinstance(receipt, dict):
        return None
    try:
        session_id = uuid.UUID(task.child_session_id)
    except (ValueError, TypeError):
        return None
    session = await db.get(ChatSession, session_id, populate_existing=True)
    parent = await db.get(Agent, task.parent_agent_id, populate_existing=True)
    if (not isinstance(authority, dict) or _hash(authority) != metadata.get("authority_snapshot_hash")
        or receipt.get("capability_snapshot_hash") != _hash(authority) or session is None
        or session.tenant_id != task.tenant_id or session.agent_id != task.child_agent_id
        or session.runtime_task_id != task.id or session.user_id != task.root_user_id
        or session.peer_agent_id != task.parent_agent_id or str(session.parent_session_id).replace("-", "") != task.parent_session_id.replace("-", "")
        or parent is None or parent.tenant_id != task.tenant_id or parent.deleted_at is not None
        or (metadata.get("parent_agent_name") and metadata["parent_agent_name"] != parent.name)
        or (authority.get("permission_profile") or {}) != (metadata.get("permission_profile") or {})):
        return None
    profile = _resolve_delegation_tool_profile(str(metadata.get("tool_profile") or "worker_safe"))
    permissions = {key: None for key in PERMISSION_INPUT_KEYS}
    permissions["permission_profile"] = metadata.get("permission_profile")
    permissions["permission_mode"] = _permission_profile_mode(metadata.get("permission_profile")) or None
    return {"schema": "hive.delegation_context_facts.v1", "tenant_id": str(task.tenant_id),
        "agent_id": str(task.child_agent_id), "session_id": str(session.id), "run_id": str(task.id),
        "owner_id": str(task.root_user_id), "authority_snapshot_hash": _hash(authority),
        "permission_inputs": permissions, "allowed_tool_names": list(profile.allowed_tools),
        "framing": _build_delegated_worker_prompt(profile, parent_name=metadata.get("parent_agent_name"))}


async def bind_delegation_context_sources(db, *, task, invocation):
    from app.runtime.context import ensure_runtime_assembly_state

    facts = await _facts(db, task)
    if facts is None:
        return
    digest = _hash(facts)
    source_id = uuid.uuid5(task.id, f"delegation-context:{digest}")
    row = await db.get(InvocationSpan, source_id)
    if row is None:
        db.add(InvocationSpan(id=source_id, tenant_id=task.tenant_id, agent_id=task.child_agent_id,
            user_id=task.root_user_id, session_id=facts["session_id"], runtime_task_id=task.id,
            trace_id=f"session-run:{task.id}", span_id=f"delegation-context:{source_id}",
            span_type="delegation_context", name="runtime_authority_facts", status="captured", duration_ms=0,
            started_at=datetime.now(timezone.utc), metadata_json={"delegation_context": facts}))
    elif row.metadata_json != {"delegation_context": facts}:
        return
    ref = {"source_kind": "delegation_context", "source_id": str(source_id), "source_version": digest}
    invocation.session_context.metadata["_delegation_context_source"] = DelegationContextSource(
        reference=ref, permission_inputs=deepcopy(facts["permission_inputs"]),
        allowed_tool_names=tuple(facts["allowed_tool_names"]))
    if invocation.system_prompt_suffix == facts["framing"]:
        ensure_runtime_assembly_state(invocation.session_context).record_context_sources(
            agent_id=invocation.agent_id, content=invocation.system_prompt_suffix, sources=[ref], unbound=())
    await db.flush()


async def validate_delegation_context_source(db, tenant_id, dependency, *, user, runtime_request):
    from app.services.content_input_receipts import ContentInputReceiptError, _authorize_session_input_source

    try:
        source_id = uuid.UUID(dependency["source_id"])
    except (ValueError, TypeError) as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The delegation context reference is invalid.", 503) from exc
    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    metadata = row.metadata_json if row and isinstance(row.metadata_json, dict) else {}
    facts = metadata.get("delegation_context")
    task = await db.get(RuntimeTask, row.runtime_task_id, populate_existing=True) if row else None
    if (row is None or row.tenant_id != tenant_id or row.span_type != "delegation_context" or row.status != "captured"
        or task is None or task.tenant_id != tenant_id or task.task_type != "delegation"
        or row.agent_id != task.child_agent_id or row.user_id != task.root_user_id
        or row.session_id != str(uuid.UUID(task.child_session_id)) or not isinstance(facts, dict)
        or _hash(facts) != dependency["source_version"]
        or source_id != uuid.uuid5(task.id, f"delegation-context:{dependency['source_version']}")
        or facts != await _facts(db, task)):
        raise ContentInputReceiptError("content_source_changed", "The delegation context no longer matches its source.", 403)
    session = await db.get(ChatSession, uuid.UUID(row.session_id), populate_existing=True)
    await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request)
