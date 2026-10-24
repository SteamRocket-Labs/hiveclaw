"""One explicit first task uses the normal durable peer delegation and return path."""

from __future__ import annotations

import uuid
import logging
import json
from dataclasses import asdict

from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.services.hr_creation_service import HrCreationConflict, direct_creation_authorization

logger = logging.getLogger(__name__)


def needs_first_task_dispatch(draft) -> bool:
    evidence = direct_creation_authorization(draft)
    return bool(evidence and evidence.get("first_task")
                and (draft.provisioning_json or {}).get("first_task", {}).get("status") != "dispatched")


async def load_first_task_work(db, *, draft, user):
    """Read the original requester's existing first task without a parent-model message."""
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.core.permissions import authorize_session_action, check_agent_access
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.session_model_inputs import _origin_dependencies
    from app.services.session_user_question import question_payload

    evidence = direct_creation_authorization(draft)
    if user.id != draft.requested_by_user_id or not evidence or not evidence.get("first_task"):
        return None
    task_id = uuid.uuid5(draft.id, "explicit-first-task.v1")
    session_id = uuid.uuid5(task_id, "session")
    task = await db.get(RuntimeTask, task_id)
    if task is None:
        return None
    work = {"runtime_task_id": str(task_id), "agent_id": str(draft.created_agent_id),
        "session_id": str(session_id), "status": "needs_reconciliation", "question": None, "can_answer": False,
        "permissions": [], "progress": None}
    if (task.tenant_id != draft.tenant_id or task.task_type != "delegation"
        or task.parent_agent_id != draft.hr_agent_id or task.child_agent_id != draft.created_agent_id
        or task.root_user_id != user.id or task.parent_session_id != str(draft.session_id)
        or task.child_session_id != str(session_id) or task.prompt != evidence["first_task"]):
        return work
    work["status"] = task.status
    if task.status not in {"running", "suspended"}:
        return work
    try:
        decision = await authorize_session_action(db, user, agent_id=task.child_agent_id, session_id=session_id,
            action="read_content_input_source")
        from app.services.session_permission_views import read_session_run_permissions

        work["permissions"] = await read_session_run_permissions(db, run=task, session=decision.session, user=user)
        from app.services.original_work_progress import read_original_work_progress

        work['progress'] = await read_original_work_progress(db, run=task, user=user)
        if task.status != "suspended" or not (task.metadata_json or {}).get("delegation_waiting_question_item_id"):
            return work
        child, access = await check_agent_access(db, user, task.child_agent_id)
        question_id = uuid.UUID(task.metadata_json["delegation_waiting_question_item_id"])
        question = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.tenant_id == draft.tenant_id, ChatTranscriptEvent.agent_id == task.child_agent_id,
            ChatTranscriptEvent.session_id == session_id, ChatTranscriptEvent.run_id == task.id,
            ChatTranscriptEvent.item_kind == "user_question", ChatTranscriptEvent.item_id == question_id,
        ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        if question is None or question.lifecycle != "waiting" or question_payload(question).get("requested_by_user_id") != str(user.id):
            return work
        invocation = await db.get(SessionToolInvocation, question.invocation_id) if question.invocation_id else None
        if (invocation is None or invocation.tenant_id != task.tenant_id or invocation.run_id != task.id
            or invocation.session_id != session_id or invocation.tool_name != "ask_user_question"):
            raise ContentInputReceiptError("user_question_unverified", "The question source is unavailable.", 503)
        dependencies, complete = await _origin_dependencies(db, tenant_id=task.tenant_id,
            agent_id=task.child_agent_id, ref=f"session-tool-result:{invocation.id}")
        if not complete:
            raise ContentInputReceiptError("user_question_unverified", "The question source is unavailable.", 503)
        await _validate_input_dependencies(db, task.tenant_id, dependencies, user=user)
        result = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        payload = json.loads(question_payload(result)["content"])
        work["question"] = {"item_id": str(question.item_id), "payload": payload}
        work["can_answer"] = access in {"use", "manage"} and child.deleted_at is None and child.deactivated_at is None
    except ContentInputReceiptError as exc:
        work.update(status="unavailable" if exc.status_code == 403 else "unknown", reason=exc.code, permissions=[], progress=None)
    except HTTPException as exc:
        if exc.status_code not in {403, 404, 410}:
            raise
        work.update(status="unavailable", reason="question_access_unavailable", permissions=[], progress=None)
    except (ValueError, TypeError, KeyError):
        work.update(status="unknown", reason="user_question_unverified", permissions=[], progress=None)
    return work


async def enqueue_first_task_question_notice(db, *, task, question):
    """Queue a canonical first-task question for authorized release to its original work."""
    from sqlalchemy import select
    from app.models.hr_creation import HrCreationDraft
    from app.services.channel_delivery_outbox import enqueue_terminal_delivery_for_task
    from app.services.session_user_question import question_payload

    draft = await db.scalar(select(HrCreationDraft).where(
        HrCreationDraft.tenant_id == task.tenant_id, HrCreationDraft.hr_agent_id == task.parent_agent_id,
        HrCreationDraft.session_id == uuid.UUID(task.parent_session_id),
        HrCreationDraft.created_agent_id == task.child_agent_id,
        HrCreationDraft.requested_by_user_id == task.root_user_id))
    evidence = direct_creation_authorization(draft) if draft is not None else None
    if (not evidence or not evidence.get("first_task")
        or task.id != uuid.uuid5(draft.id, "explicit-first-task.v1")
        or task.child_session_id != str(uuid.uuid5(task.id, "session")) or task.prompt != evidence["first_task"]
        or question_payload(question).get("requested_by_user_id") != str(task.root_user_id)):
        return None
    from app.models.chat_session import ChatSession

    session = await db.get(ChatSession, draft.session_id)
    channel = str(((session.delivery_target_json or {}) if session is not None else {}).get('channel') or '').lower()
    text = first_task_question_text(draft, question_payload(question), channel=channel)
    return await enqueue_terminal_delivery_for_task(db, task=task, content=text,
        terminal_status="clarification_required", delivery_kind="interactive_prompt", prompt_item_id=question.item_id,
        metadata={"source": "employee_first_task_question", "projection_kind": "question_content", "draft_id": str(draft.id),
            "question_item_id": str(question.item_id)})


def first_task_question_text(draft, payload=None, *, channel=None):
    from app.config import get_settings
    from app.services.session_user_question import render_user_questions

    if payload is None:
        text = "首任务有一条待答问题。请在 HiveClaw 原工作中查看最新状态并答复；此提示不代表任务完成。"
    else:
        action = "引用此消息回复即可继续原工作" if channel == 'feishu' else "请在原工作中查看并答复"
        text = f"首任务正在等待答复。{action}；此提示不代表任务完成。\n" + render_user_questions(payload)
    settings = get_settings()
    base_url = (settings.PUBLIC_BASE_URL or settings.BASE_URL).rstrip("/")
    if base_url:
        text += f"\n{base_url}/agents/{draft.hr_agent_id}?session_id={draft.session_id}#chat"
    return text


async def authorize_first_task_question_notice(db, *, item, session, user):
    from app.core.source_read_purpose import source_recipient_read
    from app.models.hr_creation import HrCreationDraft
    from app.services.content_input_receipts import ContentInputReceiptError

    try:
        draft = await db.get(HrCreationDraft, uuid.UUID(item.metadata['draft_id']), populate_existing=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise ContentInputReceiptError('first_task_question_reference_unavailable', 'Restore the original creation reference.', 503) from exc
    if (draft is None or user is None or draft.tenant_id != item.tenant_id
        or draft.hr_agent_id != item.agent_id or draft.session_id != item.session_id
        or draft.requested_by_user_id != user.id or item.user_id != user.id
        or item.runtime_task_id != uuid.uuid5(draft.id, 'explicit-first-task.v1')
        or session is None or session.id != draft.session_id or session.tenant_id != draft.tenant_id
        or item.delivery_kind != 'interactive_prompt' or item.artifact_ids):
        raise ContentInputReceiptError('first_task_question_binding_changed', 'Restore the original question binding.', 403)
    projection = item.metadata.get('projection_kind')
    if projection is None and item.text == first_task_question_text(draft):
        return  # Legacy intents contain only the fixed status and original-work link.
    if projection != 'question_content':
        raise ContentInputReceiptError('first_task_question_content_changed', 'Restore the canonical question.', 403)
    if session.audience_kind != 'group' and item.channel == 'feishu':
        from app.models.external_principal import ExternalPrincipal

        principal = await db.get(ExternalPrincipal, item.external_principal_id) if item.external_principal_id else None
        if (principal is None or principal.tenant_id != item.tenant_id or principal.id != session.external_principal_id
            or item.delivery_target.get('receive_id_type') != 'open_id'
            or item.delivery_target.get('receive_id') != principal.subject_id):
            raise ContentInputReceiptError('first_task_question_recipient_changed', 'Restore the original channel recipient.', 403)
    with source_recipient_read(session_id=session.id, user_id=user.id):
        work = await load_first_task_work(db, draft=draft, user=user)
    if work is None or work['status'] in {'unavailable', 'unknown', 'needs_reconciliation'}:
        raise ContentInputReceiptError((work or {}).get('reason', 'first_task_question_source_unavailable'),
            'Restore access to the exact question sources.', 403 if work and work['status'] == 'unavailable' else 503)
    question = work.get('question')
    if (work['status'] != 'suspended' or not question or question['item_id'] != item.prompt_key
        or question['item_id'] != item.metadata.get('question_item_id')
        or item.text != first_task_question_text(draft, question['payload'], channel=item.channel)):
        raise ContentInputReceiptError('first_task_question_no_longer_current', 'Check the current original work.', 403)


async def resume_first_task_from_tool(context, arguments) -> str:
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.database import tenant_scoped_session
    from app.models.hr_creation import HrCreationDraft
    from app.services.a2a_outcome import A2AOutcome
    from app.services.hr_creation_service import load_hr_creation_draft
    from app.services.hr_creation_source import load_creation_requester, load_creation_source_agent
    from app.services.hr_provisioning_runtime import _mark_task_completed, _runtime_authority_issues
    from app.tools.result_envelope import render_tool_error

    dispatch_started = False
    preflight_proof = None
    try:
        if context is None:
            raise HrCreationConflict("first_task_authority_required", "Resume the first task from its original governed work.")
        tenant_id, session_id, draft_id = (uuid.UUID(str(value)) for value in (
            context.tenant_id, context.session_id, arguments.get("first_task_draft_id"),
        ))
        async with tenant_scoped_session(tenant_id) as db:
            from app.services.delegation_input_origin import _parent_call

            parent = await _parent_call(db, tenant_id=tenant_id, agent_id=context.agent_id,
                session_id=session_id, run_id=uuid.UUID(str(context.runtime_task_id)), call_id=context.tool_call_id)
            if parent is not None:
                call, _model, _arguments = parent
                if call.effect_state == "effect_started" and call.result_event_id is None:
                    # The handler receives runtime-injected authority fields; bind
                    # the canonical request captured before that injection.
                    preflight_proof = _first_task_preflight_binding(context,
                        call.effective_arguments_json or call.provider_arguments_json)
            requester = await load_creation_requester(db, context.user_id)
            if requester is None:
                raise HrCreationConflict("source_execute_denied", "The first-task requester is unavailable.")
            await load_creation_source_agent(db, requester, context.agent_id, require_execute=True)
            candidate = await db.scalar(select(HrCreationDraft).where(
                HrCreationDraft.id == draft_id, HrCreationDraft.tenant_id == tenant_id,
                HrCreationDraft.hr_agent_id == context.agent_id, HrCreationDraft.session_id == session_id,
                HrCreationDraft.requested_by_user_id == context.user_id,
            ))
            if candidate is None or candidate.provisioning_task_id is None:
                raise HrCreationConflict("not_found", "No first-task creation exists in this original work.")
            job = await db.get(RuntimeTask, candidate.provisioning_task_id, with_for_update=True)
            draft = await load_hr_creation_draft(db, draft_id=draft_id, tenant_id=tenant_id,
                                                 hr_agent_id=context.agent_id, requested_by_user_id=context.user_id,
                                                 session_id=session_id, for_update=True)
            evidence = direct_creation_authorization(draft)
            if draft.status != "completed" or not evidence or not evidence.get("first_task"):
                raise HrCreationConflict("employee_not_ready", "The recorded employee must be ready before first-task dispatch.")
            if job is None or _runtime_authority_issues(job, draft, allow_completed=True) or job.status == "needs_reconciliation":
                raise HrCreationConflict("runtime_authority_mismatch", "Reconcile the immutable first-task binding before resuming.")
            if job.status not in {"failed", "killed", "resumable", "completed"}:
                raise HrCreationConflict("creation_in_progress", "The existing provisioning worker is still handling this first task.")
            target = await db.get(Agent, draft.created_agent_id)
            allowed = {"first_task_draft_id", "agent_name", "target_agent_id", "message", "parent_session_id"}
            if (target is None or arguments.get("message") != evidence["first_task"]
                    or arguments.get("agent_name") != target.name
                    or str(arguments.get("target_agent_id") or target.id) != str(target.id)
                    or any(key not in allowed and not key.startswith("_") for key in arguments)):
                raise HrCreationConflict("first_task_arguments_mismatch", "Use the exact recorded employee and first task, without worker overrides.")
            dispatch_started = True
            sent = await dispatch_first_task(db, draft, execution_context=context, arguments=arguments)
            receipt = (draft.provisioning_json or {}).get("first_task") or {}
            if sent:
                _mark_task_completed(job, draft)
                await enqueue_creation_receipt(db, draft, job)
            await db.commit()
            if not sent:
                return render_tool_error(tool_name="delegate_to_agent", error_class=receipt.get("error") or "first_task_not_dispatched",
                                         retryable=receipt.get("status") == "unknown",
                                         message="The first task was not confirmed dispatched; recover its existing intent.",
                                         extra={"first_task_draft_id": str(draft.id), **receipt})
            return A2AOutcome.success(operation="delegate", payload={
                "task_id": uuid.UUID(receipt["runtime_task_id"]).hex, "runtime_task_id": receipt["runtime_task_id"],
                "session_id": receipt["session_id"], "child_session_id": receipt["session_id"],
                "status": receipt.get("runtime_status") or "queued", "target_agent": target.name,
                "target_agent_id": str(target.id), "first_task_draft_id": str(draft.id),
                "continuation_tool": "send_agent_session_message",
            }).to_tool_result()
    except (HrCreationConflict, HTTPException, ValueError, TypeError) as exc:
        code = getattr(exc, "code", "first_task_authority_required")
        proof = None if dispatch_started else preflight_proof
        return render_tool_error(tool_name="delegate_to_agent", error_class=code, retryable=False,
                                 message=getattr(exc, "message", "The first task cannot be resumed from this work."),
                                 extra={"native_first_task_preflight": proof} if proof else None)


def _first_task_preflight_binding(context, arguments):
    from app.services.approval_ticket import hash_tool_input

    try:
        ids = {key: str(uuid.UUID(str(getattr(context, key))))
            for key in ("tenant_id", "agent_id", "session_id", "runtime_task_id")}
        uuid.UUID(str(arguments["first_task_draft_id"]))
        if not isinstance(context.tool_call_id, str) or not context.tool_call_id:
            return None
        return {"schema": "hive.first_task_preflight.v1", "effect": "not_started", **ids,
            "tool_call_id": context.tool_call_id, "arguments_hash": hash_tool_input("delegate_to_agent", arguments)}
    except (AttributeError, KeyError, ValueError, TypeError):
        return None


def first_task_preflight_result_bound(*, agent_id, invocation, payload, content):
    """Bind a native pre-dispatch refusal; no target content or execution is attested."""
    from types import SimpleNamespace
    from app.services.session_model_inputs import _hash
    from app.tools.result_envelope import render_tool_error

    try:
        arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
        proof = _first_task_preflight_binding(SimpleNamespace(tenant_id=invocation.tenant_id, agent_id=agent_id,
            session_id=invocation.session_id, runtime_task_id=invocation.run_id,
            tool_call_id=invocation.provider_tool_use_id), arguments)
        _prefix, marker, encoded = content.rpartition("<tool_error>")
        if not marker or not encoded.endswith("</tool_error>"):
            return False
        body = json.loads(encoded[:-len("</tool_error>")])
        return (proof is not None and invocation.tool_name == "delegate_to_agent" and invocation.effect_state == "failed"
            and payload.get("outcome") == "failed" and invocation.effective_args_hash == proof["arguments_hash"]
            and invocation.args_hash == _hash({"tool_name": invocation.tool_name, "arguments": invocation.provider_arguments_json})
            and payload.get("content_or_error_ref") == invocation.receipt_ref == f"tool-frame:{_hash(content)}"
            and body.get("native_first_task_preflight") == proof
            and isinstance(body.get("error_class"), str) and isinstance(body.get("message"), str)
            and content == render_tool_error(tool_name="delegate_to_agent", error_class=body["error_class"],
                message=body["message"], retryable=False, extra={"native_first_task_preflight": proof}))
    except (AttributeError, KeyError, ValueError, TypeError):
        return False


async def dispatch_first_task(db, draft, *, execution_context=None, arguments=None) -> bool:
    from app.agents.orchestrator import ASYNC_DELEGATION_TIMEOUT_SECONDS, OrchestrationPolicy, delegate_async
    from app.core.execution_context import ExecutionPrincipal
    from app.kernel.contracts import ExecutionIdentityRef
    from app.core.permissions import check_agent_access
    from app.services.capability_gate import check_capability
    from app.services.approval_ticket import build_live_approval_policy_snapshot
    from app.services.approval_ticket import ApprovalDecisionSet, hash_tool_input
    from app.runtime.ccplus_contracts import permission_profile_snapshot
    from app.tools.guard_policy import evaluate_guard_policy
    from app.services.direct_employee_creation import _approval_valid, require_local_write, validate_direct_creation
    from app.services.hr_creation_source import load_creation_requester
    from app.services.owner_action_policy import load_owner_action_policy
    from app.tools.governance import ToolGovernanceContext, _has_exact_session_grant

    evidence = direct_creation_authorization(draft)
    if not evidence or not evidence.get("first_task"):
        return True
    task_id = uuid.uuid5(draft.id, "explicit-first-task.v1")
    session_id = str(uuid.uuid5(task_id, "session"))
    receipt = {"runtime_task_id": str(task_id), "session_id": session_id}
    prior_authorization = ((draft.provisioning_json or {}).get("first_task") or {}).get("authorization")
    if isinstance(prior_authorization, dict):
        receipt["authorization"] = dict(prior_authorization)
    existing = await db.get(RuntimeTask, task_id)
    if existing is not None:
        if (existing.task_type != "delegation" or existing.tenant_id != draft.tenant_id
                or existing.parent_agent_id != draft.hr_agent_id or existing.child_agent_id != draft.created_agent_id
                or existing.root_user_id != draft.requested_by_user_id
                or existing.parent_session_id != str(draft.session_id)
                or existing.child_session_id != session_id or existing.prompt != evidence["first_task"]):
            receipt.update(status="needs_reconciliation", error="first_task_binding_mismatch")
        else:
            receipt.update(status="dispatched", runtime_status=existing.status)
    else:
        try:
            await validate_direct_creation(db, draft, check_creation_policy=False)
            user = await load_creation_requester(db, draft.requested_by_user_id)
            target, access = await check_agent_access(db, user, draft.created_agent_id)
            policy_evidence = evidence
            tool_name = "create_employee_from_request"
            session_granted = False
            delegate_arguments = {"agent_name": target.name,
                                  "target_agent_id": str(draft.created_agent_id), "message": evidence["first_task"],
                                  "first_task_draft_id": str(draft.id)}
            if execution_context is not None:
                approval = execution_context.approval_decision
                owner_policy = await load_owner_action_policy(db, tenant_id=draft.tenant_id,
                                                              agent_id=draft.hr_agent_id, create_default=False)
                policy_evidence = {
                    **evidence, "tool_input_hash": hash_tool_input("delegate_to_agent", arguments),
                    "permission_profile": permission_profile_snapshot(execution_context.permission_profile),
                    "owner_policy_hash": owner_policy.content_hash,
                    "approval": asdict(approval) if isinstance(approval, ApprovalDecisionSet) else None,
                }
                delegate_arguments = arguments
                tool_name = "delegate_to_agent"
                session_granted = _has_exact_session_grant(ToolGovernanceContext(
                    agent_id=execution_context.agent_id, user_id=execution_context.user_id,
                    tenant_id=execution_context.tenant_id, tool_name=tool_name, arguments=arguments,
                    permission_profile=execution_context.permission_profile,
                ))
            approved = await _approval_valid(db, policy_evidence.get("approval"), tenant_id=draft.tenant_id,
                                            agent_id=draft.hr_agent_id, user_id=draft.requested_by_user_id,
                                            input_hash=policy_evidence["tool_input_hash"], tool_name=tool_name)
            await require_local_write(db, policy_evidence, approved=approved or session_granted)
            capability = await check_capability(db, draft.tenant_id, draft.hr_agent_id, "delegate_to_agent")
            if capability.denied:
                raise HrCreationConflict("capability_denied", "Company policy denies the first task delegation.")
            if capability.policy_found and capability.escalate_to_l3 and not (approved and tool_name == "delegate_to_agent"):
                raise HrCreationConflict("approval_required", "The first task delegation requires approval.")
            profile = policy_evidence.get("permission_profile") or {}
            if not capability.policy_found and not capability.allowed and not (
                approved or session_granted or profile.get("mode") == "bypass_permissions" or "delegate_to_agent" in profile.get("allowed_tools", [])
            ):
                raise HrCreationConflict("approval_required", "The first task delegation needs scoped authorization.")
            live = await build_live_approval_policy_snapshot(db=db, tenant_id=draft.tenant_id,
                                                           agent_id=draft.hr_agent_id, tool_name="delegate_to_agent")
            guard = evaluate_guard_policy(tool_name="delegate_to_agent",
                                          arguments=delegate_arguments,
                                          external_visible=False, snapshot=live["guard_policy"])
            if guard.decision == "deny" or (guard.decision == "require_approval" and not (approved and tool_name == "delegate_to_agent")):
                code = "capability_denied" if guard.decision == "deny" else "approval_required"
                raise HrCreationConflict(code, "Company GuardPolicy blocks the first task delegation.")
            source = await db.get(Agent, draft.hr_agent_id)
            model = await db.get(LLMModel, target.primary_model_id) if target.primary_model_id else None
            if (access not in {"use", "manage"} or target.deactivated_at is not None
                    or target.deleted_at is not None or model is None or not model.enabled
                    or model.tenant_id != draft.tenant_id):
                raise HrCreationConflict("employee_not_ready", "The employee is not ready for its first task.")
            source_run = await db.get(RuntimeTask, uuid.UUID(evidence["source_run_id"]))
            principal = ExecutionPrincipal(
                tenant_id=draft.tenant_id, source_agent_id=draft.hr_agent_id,
                requester_user_id=draft.requested_by_user_id, root_session_id=str(draft.session_id),
                root_runtime_task_id=str(source_run.id), origin="explicit_employee_first_task",
                delegation_chain=(f"agent:{draft.hr_agent_id}",),
            )
            instruction = {"role": "user", "content": evidence["first_task"]}
            if "creation_origin" in evidence:
                instruction["a2a_instruction_origin"] = evidence["creation_origin"]
            handle = await delegate_async(
                target=target, target_model=model, conversation_messages=[instruction],
                owner_id=draft.requested_by_user_id, session_id=session_id,
                parent_agent_id=draft.hr_agent_id, parent_agent_name=source.name,
                parent_session_id=str(draft.session_id), tenant_id=draft.tenant_id,
                root_runtime_task_id=str(source_run.id), execution_principal=principal.to_evidence(),
                execution_identity=ExecutionIdentityRef("delegated_user", draft.requested_by_user_id, "Explicit employee first task"),
                policy=OrchestrationPolicy(timeout_seconds=ASYNC_DELEGATION_TIMEOUT_SECONDS,
                                           tool_profile="peer_agent"), runtime_task_id=task_id,
                trace_id=f"employee-first-task:{draft.id}", budget_run_id=source_run.budget_run_id,
                permission_profile=policy_evidence["permission_profile"],
            )
            if handle.task_id == task_id.hex:
                receipt.update(status="dispatched", runtime_status=handle.status)
                if execution_context is not None:
                    receipt["authorization"] = {
                        "tool_name": tool_name, "input_hash": policy_evidence["tool_input_hash"],
                        "approval_id": str(execution_context.approval_decision.approval_id) if approved else None,
                        "runtime_task_id": str(execution_context.runtime_task_id),
                        "session_grant_input_hash": policy_evidence["tool_input_hash"] if session_granted else None,
                    }
            else:
                receipt.update(status="blocked", error=handle.status)
        except HrCreationConflict as exc:
            receipt.update(status="blocked", error=exc.code)
        except Exception as exc:
            # An interrupted dispatch reuses the same durable id; it never issues another intent.
            logger.exception("Explicit first-task dispatch did not finish for draft %s", draft.id)
            receipt.update(status="unknown", error=getattr(exc, "code", type(exc).__name__))
    draft.provisioning_json = {**dict(draft.provisioning_json or {}), "first_task": receipt}
    return receipt["status"] == "dispatched"


async def creation_receipt_notification(db, draft, task):
    """Render the native creation observation without publishing or changing it."""
    from app.services.runtime_notification_outbox import CompletionNotification

    evidence = direct_creation_authorization(draft)
    if evidence is None:
        return
    first = (draft.provisioning_json or {}).get("first_task") or {}
    summary = (f"Employee creation: {draft.status}; employee_id={draft.created_agent_id or 'none'}; "
               f"first_task={first.get('status', 'not_requested')}; first_task_error={first.get('error', 'none')}; "
               f"creation_error={draft.failure_code or 'none'}; draft_id={draft.id}. "
               "Creation readiness is not evidence of task completion. Continue this original work from these facts.")
    if draft.created_agent_id and first.get("status") in {"blocked", "unknown"}:
        target = await db.get(Agent, draft.created_agent_id)
        summary += "\nRecorded first-task recovery request: " + json.dumps({
            "tool_name": "delegate_to_agent", "arguments": {
                "first_task_draft_id": str(draft.id), "agent_name": target.name if target else None,
                "target_agent_id": str(draft.created_agent_id), "message": evidence["first_task"],
            },
        }, ensure_ascii=False)
    return CompletionNotification(
        tenant_id=draft.tenant_id, source_kind="employee_creation", source_run_id=str(task.id),
        parent_session_id=str(draft.session_id), parent_agent_id=draft.hr_agent_id,
        parent_user_id=draft.requested_by_user_id, terminal_status=task.status, task_type="hr_provisioning",
        root_runtime_task_id=evidence["source_run_id"], summary=summary,
        metadata={"draft_id": str(draft.id), "created_agent_id": str(draft.created_agent_id or ""),
                  "first_task_status": first.get("status"), "first_task_runtime_id": first.get("runtime_task_id"),
                  "first_task_error": first.get("error"), "creation_error": draft.failure_code},
    )


async def enqueue_creation_receipt(db, draft, task):
    from app.services.runtime_notification_outbox import enqueue_completion_notification

    notification = await creation_receipt_notification(db, draft, task)
    if notification is not None:
        await enqueue_completion_notification(db, notification)
