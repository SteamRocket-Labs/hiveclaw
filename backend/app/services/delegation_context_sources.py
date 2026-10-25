"""Versioned runtime facts for delegated framing and permission configuration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.session_model_inputs import _hash, _model_dependencies, _origin_dependencies, _unique_dependencies


PERMISSION_INPUT_KEYS = (
    "permission_profile",
    "approval_policy",
    "permission_mode",
    "network_access",
    "network_policy",
    "allowed_tools",
    "writable_roots",
    "denied_reads",
    "denied_actions",
    "request_permission_tool_enabled",
    "plan_mode",
    "skill_execution_plans",
    "pending_skill_handoffs",
)


def _permission_inputs(metadata):
    return {key: deepcopy(metadata.get(key)) for key in PERMISSION_INPUT_KEYS}


@dataclass(frozen=True)
class PermissionContextSource:
    reference: dict
    permission_inputs: dict
    allowed_tool_names: tuple[str, ...]

    def matches(self, metadata, allowed_tool_names):
        return self.permission_inputs == _permission_inputs(metadata) and self.allowed_tool_names == tuple(
            allowed_tool_names
        )


DelegationContextSource = PermissionContextSource


async def bind_private_session_permission_source(db, *, task, session, invocation):
    from fastapi import HTTPException
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.services.runtime_task_fence import assert_runtime_task_fence
    from app.services.session_v2_persistence import resolve_session_command_authority

    metadata = invocation.session_context.metadata
    metadata.pop("_session_permission_context_source", None)
    if getattr(session, "audience_kind", None) not in {None, "private"}:
        return
    try:
        task = await db.get(RuntimeTask, task.id, populate_existing=True)
        stored = task.metadata_json or {}
        command_id = uuid.UUID(stored["session_v2_command_id"])
        input_id = uuid.UUID(stored.get("session_v2_input_id") or stored["session_v2_rolled_over_input_id"])
        assert_runtime_task_fence(task)
        if (
            task.tenant_id != session.tenant_id
            or task.parent_agent_id != session.agent_id
            or task.parent_session_id != str(session.id)
            or task.root_user_id != session.user_id
            or invocation.tenant_id != session.tenant_id
            or invocation.agent_id != session.agent_id
            or invocation.user_id != task.root_user_id
            or invocation.session_context.session_id != str(session.id)
        ):
            return
        row = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, command_id)
        if (
            row is None
            or command is None
            or row.tenant_id != session.tenant_id
            or row.session_id != session.id
            or row.command_id != command.id
            or row.target_run_id not in {None, task.id}
            or command.tenant_id != session.tenant_id
            or command.session_id != session.id
            or command.request_hash != _hash({"command_kind": command.command_kind, "request": command.request_json})
            or command.target_hash != _hash(command.target_json)
        ):
            return
        authority = await resolve_session_command_authority(
            db, command=command, session=session, action="mutate_session_input"
        )
        if authority.actor.id != task.root_user_id:
            return
    except (AttributeError, KeyError, ValueError, PermissionError, HTTPException):
        return
    frame = dict((command.target_json or {}).get("runtime_metadata") or {})
    # The authenticated input owns entry; the platform derives the exact
    # restrictive planning protocol, including its reserved file path.
    if metadata.get("plan_mode"):
        from app.services.plan_mode_native_run import load_system_plan_mode

        native_mode = await load_system_plan_mode(db, task=task, session=session)
        if native_mode is not None and metadata["plan_mode"] == native_mode.to_metadata():
            frame["plan_mode"] = native_mode.to_metadata()
        from app.services import plan_mode_core
        from app.services.session_user_checkpoint import render_human_input_content
        from app.services.web_chat_runtime import _interactive_plan_mode_state

        content = render_human_input_content(row.content_parts_json)
        decision = plan_mode_core.classify_plan_mode_entry(
            str(frame.get("display_content") or content), explicit=bool(frame.get("plan_mode_requested"))
        )
        if decision.mode not in {"none", "declined"} and decision.action_kind and decision.tool_name:
            expected = _interactive_plan_mode_state(
                original_request=content, decision=decision, session_id=str(session.id)
            ).to_metadata()
            if metadata["plan_mode"] == expected:
                frame["plan_mode"] = expected
    profile = frame.get("permission_profile") or {}
    exact = (profile.get("capability_policy_snapshot") or {}).get("session_exact_scope") is True
    from app.services.native_trigger_frame import load_native_trigger_frame

    native_frame = await load_native_trigger_frame(db, task=task, session=session, for_effect=False)
    session_tools = tuple(profile.get("allowed_tools") or ()) if exact else ()
    allowed_tools = native_frame.allowed_with(session_tools) if native_frame else session_tools
    source = PermissionContextSource(
        reference={
            "source_kind": "session_command",
            "source_id": str(command.id),
            "source_version": command.request_hash,
        },
        permission_inputs=_permission_inputs(frame),
        allowed_tool_names=allowed_tools,
    )
    if source.matches(metadata, invocation.allowed_tool_names):
        metadata["_session_permission_context_source"] = source


@dataclass(frozen=True)
class NativeRecoveryContextSource:
    checkpoint: dict
    sources: tuple
    unbound: tuple
    file_snapshots: dict = field(default_factory=dict)

    def matches(self, result, session):
        return (
            result is not None
            and result.loaded
            and result.authority is not None
            and self.checkpoint
            == {
                "authority_hash": result.authority.digest,
                "manifest_hash": result.envelope_sha256,
                "session_id": str(session.session_id),
            }
        )

    def matches_file_snapshots(self, session, paths):
        return (
            self.checkpoint.get("session_id") == str(session.session_id)
            and bool(paths)
            and all(
                path in self.file_snapshots and self.file_snapshots[path] == session.file_snapshots.get(path)
                for path in paths
            )
        )


async def _recovery_inputs(db, *, task, provider_request_id):
    agent_id, session_id = _recovery_scope(task)
    model = await db.scalar(
        select(SessionModelResult).where(
            SessionModelResult.tenant_id == task.tenant_id,
            SessionModelResult.run_id == task.id,
            SessionModelResult.session_id == session_id,
            SessionModelResult.provider_request_id == provider_request_id,
            SessionModelResult.state == "round_committed",
        )
    )
    sources, complete = _model_dependencies(model) if model else ([], False)
    tools = list(
        await db.scalars(
            select(SessionToolInvocation)
            .where(
                SessionToolInvocation.tenant_id == task.tenant_id,
                SessionToolInvocation.run_id == task.id,
                SessionToolInvocation.provider_request_id == provider_request_id,
            )
            .order_by(SessionToolInvocation.provider_tool_use_id)
        )
    )
    for tool in tools:
        permission = tool.permission_item_id is not None
        refs, bound = await _origin_dependencies(
            db,
            tenant_id=task.tenant_id,
            agent_id=agent_id,
            ref=f"session-tool-{'permission' if permission else 'result'}:{tool.id}",
        )
        sources.extend(refs)
        settled = tool.effect_state == "effect_committed" or (
            permission
            and (
                (tool.effect_state == "prepared_not_started" and tool.permission_state == "waiting")
                or (tool.result_event_id is not None and tool.permission_state == "denied")
            )
        )
        if permission and tool.permission_state == "expired" and tool.result_event_id is not None:
            from app.models.chat_transcript_event import ChatTranscriptEvent
            from app.services.session_model_inputs import _expired_permission_result_bound
            from app.services.session_semantic_history import _event_payload

            event = await db.get(ChatTranscriptEvent, tool.result_event_id)
            settled = event is not None and await _expired_permission_result_bound(
                db, invocation=tool, event=event, content=_event_payload(event).get("content")
            )
        if (
            permission
            and tool.permission_state == "approved"
            and tool.effect_state == "prepared_not_started"
            and tool.result_event_id is not None
        ):
            from app.models.chat_transcript_event import ChatTranscriptEvent
            from app.services.session_model_inputs import permission_abort_result_bound
            from app.services.session_semantic_history import _event_payload

            event = await db.get(ChatTranscriptEvent, tool.result_event_id)
            settled = event is not None and await permission_abort_result_bound(
                db, invocation=tool, event=event, content=_event_payload(event).get("content")
            )
        if not settled and tool.effect_state == "prepared_not_started" and tool.result_event_id is not None:
            from app.models.chat_transcript_event import ChatTranscriptEvent
            from app.services.session_approval import invocation_enterprise_approval
            from app.services.session_semantic_history import _event_payload

            event = await db.get(ChatTranscriptEvent, tool.result_event_id)
            try:
                result = json.loads(_event_payload(event).get("content", "")) if event else {}
                from app.models.audit import ApprovalRequest

                approval = await db.get(ApprovalRequest, uuid.UUID(str(result.get("approval_id"))))
                settled = (
                    result.get("status") == "approval_required"
                    and approval is not None
                    and await invocation_enterprise_approval(
                        db,
                        invocation=tool,
                        agent_id=agent_id,
                        decision={"approval_id": str(approval.id), "decision_id": approval.decision_id},
                        input_hash=tool.effective_args_hash,
                    )
                    is not None
                )
            except (ValueError, TypeError):
                settled = False
        complete = complete and bound and settled
    return model, _unique_dependencies(sources), complete


def _recovery_scope(task):
    if getattr(task, "task_type", "delegation") == "delegation":
        return task.child_agent_id, uuid.UUID(task.child_session_id)
    if task.task_type == "business_task":
        return task.parent_agent_id, uuid.UUID(task.child_session_id)
    return task.parent_agent_id, uuid.UUID(task.parent_session_id)


async def capture_native_recovery_context(db, *, task, session, provider_request_id):
    agent_id, session_id = _recovery_scope(task)
    checkpoint = (session.metadata or {}).get("_native_recovery_checkpoint")
    if not isinstance(checkpoint, dict) or set(checkpoint) != {"authority_hash", "manifest_hash", "session_id"}:
        return
    try:
        if uuid.UUID(str(checkpoint["session_id"])) != session_id:
            return
    except (TypeError, ValueError):
        return
    model, sources, complete = await _recovery_inputs(db, task=task, provider_request_id=provider_request_id)
    if model is None:
        return
    proof = {
        "schema": "hive.native_recovery_context.v1",
        "checkpoint": checkpoint,
        "model_result_id": str(model.id),
        "model_request_hash": model.model_request_hash,
        "provider_request_id": provider_request_id,
        "sources": sources,
        "complete": complete,
        "file_snapshots": deepcopy(session.file_snapshots),
    }
    digest = _hash(proof)
    source_id = uuid.uuid5(task.id, f"native-recovery-context:{digest}")
    row = await db.get(InvocationSpan, source_id)
    if row is None:
        db.add(
            InvocationSpan(
                id=source_id,
                tenant_id=task.tenant_id,
                agent_id=agent_id,
                user_id=task.root_user_id,
                session_id=str(session_id),
                runtime_task_id=task.id,
                trace_id=f"session-run:{task.id}",
                span_id=f"native-recovery:{source_id}",
                span_type="native_recovery_context",
                name="recovery_input_sources",
                status="captured",
                duration_ms=0,
                started_at=datetime.now(timezone.utc),
                metadata_json={"native_recovery_context": proof},
            )
        )
    elif row.metadata_json != {"native_recovery_context": proof}:
        return
    task.metadata_json = {
        **(task.metadata_json or {}),
        "native_recovery_context_source": {"id": str(source_id), "hash": digest},
    }
    session.metadata["_native_recovery_context_source"] = NativeRecoveryContextSource(
        checkpoint,
        tuple(sources),
        () if complete else ("native_recovery_inputs_incomplete",),
        deepcopy(proof["file_snapshots"]),
    )
    await db.flush()


async def bind_native_recovery_context(db, *, task, session):
    agent_id, session_id = _recovery_scope(task)
    session.metadata.pop("_native_recovery_context_source", None)
    pointer = (task.metadata_json or {}).get("native_recovery_context_source")
    if not isinstance(pointer, dict):
        answer = (task.metadata_json or {}).get("native_trigger_answer")
        if isinstance(answer, dict):
            from app.models.chat_session import ChatSession
            from app.services.native_trigger_answers import revalidate_native_trigger_answer

            original_session = await db.get(ChatSession, session_id)
            await revalidate_native_trigger_answer(db, task=task, session=original_session)
            original = await db.get(RuntimeTask, uuid.UUID(answer["source_run_id"]))
            await bind_native_recovery_context(db, task=original, session=session)
            return
        if getattr(task, "task_type", "delegation") != "delegation" and (task.metadata_json or {}).get(
            "integration_page_id"
        ):
            from app.models.audit import ApprovalRequest
            from app.models.chat_session import ChatSession
            from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
            from app.services.session_approval import group_approval_origin, group_approval_return_actor

            group = await db.get(ChatSession, session_id)
            if group is None:
                return
            rows = list(
                await db.scalars(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.integration_page_id
                        == uuid.UUID(task.metadata_json["integration_page_id"])
                    )
                )
            )
            native_originals = {}
            if group.audience_kind == "group":
                actor = await group_approval_return_actor(db, session=group, rows=rows)
                if actor.id != task.root_user_id:
                    return
            else:
                from app.models.runtime_result import RuntimeResultObject
                from app.services.runtime_result_sources import load_canonical_result_sources

                if group.user_id != task.root_user_id or not rows:
                    return
                for row in rows:
                    result = await db.get(RuntimeResultObject, row.result_object_id)
                    canonical = await load_canonical_result_sources(db, result) if result is not None else False
                    if (
                        not canonical
                        or row.tenant_id != task.tenant_id
                        or row.source_kind not in {"approval", "employee_creation", "a2a_delegation"}
                        or result.source_kind != row.source_kind
                        or row.parent_agent_id != agent_id
                        or row.parent_session_id != session_id
                        or row.parent_user_id != task.root_user_id
                        or canonical[0].root_user_id != task.root_user_id
                        or canonical[0].parent_session_id != str(session_id)
                        or canonical[0].parent_agent_id != agent_id
                    ):
                        return
                    if row.source_kind != "approval":
                        original = await db.get(RuntimeTask, canonical[0].root_runtime_task_id)
                        if (
                            original is None
                            or original.id == task.id
                            or original.tenant_id != task.tenant_id
                            or original.task_type != "web_chat_turn"
                            or original.root_user_id != task.root_user_id
                            or original.parent_agent_id != agent_id
                            or original.parent_session_id != str(session_id)
                        ):
                            return
                        native_originals[row.id] = original
            originals = {}
            for row in rows:
                if row.id in native_originals:
                    # Reuse the original native checkpoint; a result never attests old recovery bytes.
                    original = native_originals[row.id]
                else:
                    approval = await db.get(ApprovalRequest, uuid.UUID(row.metadata_json["approval_id"]))
                    original, _invocation, _event = await group_approval_origin(db, session=group, approval=approval)
                originals[original.id] = original
            if len(originals) == 1:
                original = next(iter(originals.values()))
                if (original.metadata_json or {}).get("native_recovery_context_source"):
                    await bind_native_recovery_context(db, task=original, session=session)
        return
    try:
        source_id = uuid.UUID(pointer["id"])
    except (ValueError, KeyError, TypeError):
        return
    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    proof = (row.metadata_json or {}).get("native_recovery_context") if row else None
    if (
        row is None
        or row.tenant_id != task.tenant_id
        or row.agent_id != agent_id
        or row.user_id != task.root_user_id
        or row.session_id != str(session_id)
        or row.runtime_task_id != task.id
        or row.span_type != "native_recovery_context"
        or row.status != "captured"
        or not isinstance(proof, dict)
        or proof.get("schema") != "hive.native_recovery_context.v1"
        or _hash(proof) != pointer.get("hash")
        or source_id != uuid.uuid5(task.id, f"native-recovery-context:{pointer['hash']}")
    ):
        return
    model, sources, complete = await _recovery_inputs(
        db, task=task, provider_request_id=proof.get("provider_request_id")
    )
    if (
        model is None
        or str(model.id) != proof.get("model_result_id")
        or model.model_request_hash != proof.get("model_request_hash")
        or sources != proof.get("sources")
        or complete is not proof.get("complete")
    ):
        return
    session.metadata["_native_recovery_context_source"] = NativeRecoveryContextSource(
        proof["checkpoint"],
        tuple(sources),
        () if complete else ("native_recovery_inputs_incomplete",),
        deepcopy(proof.get("file_snapshots") or {}),
    )


async def _facts(db, task):
    from app.agents.orchestrator import (
        _build_delegated_worker_prompt,
        _permission_profile_mode,
        _resolve_delegation_tool_profile,
    )
    from app.runtime.ccplus_contracts import permission_profile_snapshot

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
    stored_profile = metadata.get("permission_profile")
    if stored_profile is None:
        stored_profile = permission_profile_snapshot(None)
    if (
        not isinstance(authority, dict)
        or _hash(authority) != metadata.get("authority_snapshot_hash")
        or receipt.get("capability_snapshot_hash") != _hash(authority)
        or session is None
        or session.tenant_id != task.tenant_id
        or session.agent_id != task.child_agent_id
        or session.runtime_task_id != task.id
        or session.user_id != task.root_user_id
        or session.peer_agent_id != task.parent_agent_id
        or str(session.parent_session_id).replace("-", "") != task.parent_session_id.replace("-", "")
        or parent is None
        or parent.tenant_id != task.tenant_id
        or parent.deleted_at is not None
        or (metadata.get("parent_agent_name") and metadata["parent_agent_name"] != parent.name)
        or authority.get("permission_profile") != stored_profile
    ):
        return None
    profile = _resolve_delegation_tool_profile(str(metadata.get("tool_profile") or "worker_safe"))
    permissions = {key: None for key in PERMISSION_INPUT_KEYS}
    permissions["permission_profile"] = metadata.get("permission_profile")
    permissions["permission_mode"] = _permission_profile_mode(metadata.get("permission_profile")) or None
    return {
        "schema": "hive.delegation_context_facts.v1",
        "tenant_id": str(task.tenant_id),
        "agent_id": str(task.child_agent_id),
        "session_id": str(session.id),
        "run_id": str(task.id),
        "owner_id": str(task.root_user_id),
        "authority_snapshot_hash": _hash(authority),
        "permission_inputs": permissions,
        "allowed_tool_names": list(profile.allowed_tools),
        "framing": _build_delegated_worker_prompt(profile, parent_name=metadata.get("parent_agent_name")),
    }


async def bind_delegation_context_sources(db, *, task, invocation):
    from app.runtime.context import ensure_runtime_assembly_state

    facts = await _facts(db, task)
    if facts is None:
        return
    digest = _hash(facts)
    source_id = uuid.uuid5(task.id, f"delegation-context:{digest}")
    row = await db.get(InvocationSpan, source_id)
    if row is None:
        db.add(
            InvocationSpan(
                id=source_id,
                tenant_id=task.tenant_id,
                agent_id=task.child_agent_id,
                user_id=task.root_user_id,
                session_id=facts["session_id"],
                runtime_task_id=task.id,
                trace_id=f"session-run:{task.id}",
                span_id=f"delegation-context:{source_id}",
                span_type="delegation_context",
                name="runtime_authority_facts",
                status="captured",
                duration_ms=0,
                started_at=datetime.now(timezone.utc),
                metadata_json={"delegation_context": facts},
            )
        )
    elif row.metadata_json != {"delegation_context": facts}:
        return
    ref = {"source_kind": "delegation_context", "source_id": str(source_id), "source_version": digest}
    invocation.session_context.metadata["_delegation_context_source"] = DelegationContextSource(
        reference=ref,
        permission_inputs=deepcopy(facts["permission_inputs"]),
        allowed_tool_names=tuple(facts["allowed_tool_names"]),
    )
    if invocation.system_prompt_suffix == facts["framing"]:
        ensure_runtime_assembly_state(invocation.session_context).record_context_sources(
            agent_id=invocation.agent_id, content=invocation.system_prompt_suffix, sources=[ref], unbound=()
        )
    await db.flush()


async def validate_delegation_context_source(db, tenant_id, dependency, *, user, runtime_request):
    from app.services.content_input_receipts import ContentInputReceiptError, _authorize_session_input_source

    try:
        source_id = uuid.UUID(dependency["source_id"])
    except (ValueError, TypeError) as exc:
        raise ContentInputReceiptError(
            "content_source_unavailable", "The delegation context reference is invalid.", 503
        ) from exc
    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    metadata = row.metadata_json if row and isinstance(row.metadata_json, dict) else {}
    facts = metadata.get("delegation_context")
    task = await db.get(RuntimeTask, row.runtime_task_id, populate_existing=True) if row else None
    if (
        row is None
        or row.tenant_id != tenant_id
        or row.span_type != "delegation_context"
        or row.status != "captured"
        or task is None
        or task.tenant_id != tenant_id
        or task.task_type != "delegation"
        or row.agent_id != task.child_agent_id
        or row.user_id != task.root_user_id
        or row.session_id != str(uuid.UUID(task.child_session_id))
        or not isinstance(facts, dict)
        or _hash(facts) != dependency["source_version"]
        or source_id != uuid.uuid5(task.id, f"delegation-context:{dependency['source_version']}")
        or facts != await _facts(db, task)
    ):
        raise ContentInputReceiptError(
            "content_source_changed", "The delegation context no longer matches its source.", 403
        )
    session = await db.get(ChatSession, uuid.UUID(row.session_id), populate_existing=True)
    await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request)
