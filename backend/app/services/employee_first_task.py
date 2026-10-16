"""One explicit first task uses the normal durable peer delegation and return path."""

from __future__ import annotations

import uuid
import logging

from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.services.hr_creation_service import HrCreationConflict, direct_creation_authorization

logger = logging.getLogger(__name__)


def needs_first_task_dispatch(draft) -> bool:
    evidence = direct_creation_authorization(draft)
    return bool(evidence and evidence.get("first_task")
                and (draft.provisioning_json or {}).get("first_task", {}).get("status") != "dispatched")


async def dispatch_first_task(db, draft) -> bool:
    from app.agents.orchestrator import OrchestrationPolicy, delegate_async
    from app.core.execution_context import ExecutionPrincipal
    from app.kernel.contracts import ExecutionIdentityRef
    from app.core.permissions import check_agent_access
    from app.services.capability_gate import check_capability
    from app.services.approval_ticket import build_live_approval_policy_snapshot
    from app.tools.guard_policy import evaluate_guard_policy
    from app.services.direct_employee_creation import _approval_valid, require_local_write, validate_direct_creation
    from app.services.hr_creation_source import load_creation_requester

    evidence = direct_creation_authorization(draft)
    if not evidence or not evidence.get("first_task"):
        return True
    task_id = uuid.uuid5(draft.id, "explicit-first-task.v1")
    session_id = str(uuid.uuid5(task_id, "session"))
    receipt = {"runtime_task_id": str(task_id), "session_id": session_id}
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
            approved = await _approval_valid(db, evidence.get("approval"), tenant_id=draft.tenant_id,
                                            agent_id=draft.hr_agent_id, user_id=draft.requested_by_user_id,
                                            input_hash=evidence["tool_input_hash"])
            await require_local_write(db, evidence, approved=approved)
            capability = await check_capability(db, draft.tenant_id, draft.hr_agent_id, "delegate_to_agent")
            if capability.denied:
                raise HrCreationConflict("capability_denied", "Company policy denies the first task delegation.")
            if capability.policy_found and capability.escalate_to_l3:
                raise HrCreationConflict("approval_required", "The first task delegation requires approval.")
            profile = evidence.get("permission_profile") or {}
            if not capability.policy_found and not capability.allowed and not (
                approved or profile.get("mode") == "bypass_permissions" or "delegate_to_agent" in profile.get("allowed_tools", [])
            ):
                raise HrCreationConflict("approval_required", "The first task delegation needs scoped authorization.")
            live = await build_live_approval_policy_snapshot(db=db, tenant_id=draft.tenant_id,
                                                           agent_id=draft.hr_agent_id, tool_name="delegate_to_agent")
            guard = evaluate_guard_policy(tool_name="delegate_to_agent",
                                          arguments={"target_agent_id": str(draft.created_agent_id), "message": evidence["first_task"]},
                                          external_visible=False, snapshot=live["guard_policy"])
            if guard.decision != "allow":
                code = "capability_denied" if guard.decision == "deny" else "approval_required"
                raise HrCreationConflict(code, "Company GuardPolicy blocks the first task delegation.")
            user = await load_creation_requester(db, draft.requested_by_user_id)
            target, access = await check_agent_access(db, user, draft.created_agent_id)
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
            handle = await delegate_async(
                target=target, target_model=model, conversation_messages=[{"role": "user", "content": evidence["first_task"]}],
                owner_id=draft.requested_by_user_id, session_id=session_id,
                parent_agent_id=draft.hr_agent_id, parent_agent_name=source.name,
                parent_session_id=str(draft.session_id), tenant_id=draft.tenant_id,
                root_runtime_task_id=str(source_run.id), execution_principal=principal.to_evidence(),
                execution_identity=ExecutionIdentityRef("delegated_user", draft.requested_by_user_id, "Explicit employee first task"),
                policy=OrchestrationPolicy(tool_profile="peer_agent"), runtime_task_id=task_id,
                trace_id=f"employee-first-task:{draft.id}", budget_run_id=source_run.budget_run_id,
                permission_profile=evidence["permission_profile"],
            )
            if handle.task_id == task_id.hex:
                receipt.update(status="dispatched", runtime_status=handle.status)
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


async def enqueue_creation_receipt(db, draft, task):
    from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification

    evidence = direct_creation_authorization(draft)
    if evidence is None:
        return
    first = (draft.provisioning_json or {}).get("first_task") or {}
    summary = (f"Employee creation: {draft.status}; employee_id={draft.created_agent_id or 'none'}; "
               f"first_task={first.get('status', 'not_requested')}; first_task_error={first.get('error', 'none')}; "
               f"creation_error={draft.failure_code or 'none'}; draft_id={draft.id}. "
               "Creation readiness is not evidence of task completion. Continue this original work from these facts.")
    await enqueue_completion_notification(db, CompletionNotification(
        tenant_id=draft.tenant_id, source_kind="employee_creation", source_run_id=str(task.id),
        parent_session_id=str(draft.session_id), parent_agent_id=draft.hr_agent_id,
        parent_user_id=draft.requested_by_user_id, terminal_status=task.status, task_type="hr_provisioning",
        root_runtime_task_id=evidence["source_run_id"], summary=summary,
        metadata={"draft_id": str(draft.id), "created_agent_id": str(draft.created_agent_id or ""),
                  "first_task_status": first.get("status"), "first_task_runtime_id": first.get("runtime_task_id"),
                  "first_task_error": first.get("error"), "creation_error": draft.failure_code},
    ))
