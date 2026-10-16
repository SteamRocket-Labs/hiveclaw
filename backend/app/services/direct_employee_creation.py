"""Approved private employees from an authenticated direct-human turn."""

from __future__ import annotations

import uuid
from dataclasses import asdict

from sqlalchemy import select, text

from app.models.audit import ApprovalRequest
from app.models.chat_session import ChatSession
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionTurnInput
from app.runtime.ccplus_contracts import permission_profile_snapshot
from app.services.approval_ticket import (
    ApprovalDecisionSet, build_live_approval_policy_snapshot, hash_policy_snapshot, hash_tool_input,
)
from app.services.hr_creation_service import (
    HrCreationConflict, canonical_hr_blueprint_hash, direct_creation_authorization,
    hr_creation_draft_payload, upsert_hr_creation_draft,
)
from app.services.hr_creation_source import load_creation_requester, load_creation_source_agent
from app.services.owner_action_policy import ACTION_LOCAL_WRITE, load_owner_action_policy
from app.services.session_human_input import SESSION_TARGETABLE_RUN_STATUSES

TOOL_NAME = "create_employee_from_request"
AUTHORIZATION_SCHEMA = "hive.employee_creation_authorization.v1"
WEB_INPUT_SOURCES = {
    "legacy_rest_create_session_run", "legacy_rest_start_session_run", "session_input_api", "websocket_message",
}


def _refused(code: str, message: str):
    raise HrCreationConflict(code, message)


async def _human_request(db, *, tenant_id, user_id, agent_id, session_id, run_id, input_id=None):
    user = await load_creation_requester(db, user_id)
    if user is None:
        _refused("source_execute_denied", "The authenticated requester is unavailable.")
    agent = await load_creation_source_agent(db, user, agent_id, require_execute=True)
    session = await db.get(ChatSession, session_id)
    run = await db.get(RuntimeTask, run_id)
    if (
        agent.agent_type != "native" or agent.agent_class != "internal_tenant"
        or session is None or session.tenant_id != tenant_id or session.agent_id != agent_id
        or session.user_id != user_id or session.external_principal_id is not None
        or session.session_kind != "human_chat" or session.actor_type != "user"
        or session.visibility_scope != "direct_user" or session.listed_surface != "chat"
        or run is None or run.tenant_id != tenant_id or run.root_user_id != user_id
        or run.parent_agent_id != agent_id or run.child_agent_id != agent_id
        or run.parent_session_id != str(session_id) or run.child_session_id != str(session_id)
        or run.root_session_id != str(session_id)
    ):
        _refused("source_execute_denied", "Creation requires the requester's original direct work.")
    metadata = run.metadata_json or {}
    raw_input = metadata.get("session_v2_input_id") or metadata.get("session_v2_rolled_over_input_id")
    try:
        row = await db.get(SessionTurnInput, uuid.UUID(str(raw_input)))
    except (TypeError, ValueError):
        row = None
    command = await db.get(SessionCommand, row.command_id) if row is not None else None
    if (
        row is None or row.tenant_id != tenant_id or row.session_id != session_id
        or (input_id is not None and str(row.id) != str(input_id))
        or (row.target_run_id != run_id and not (
            metadata.get("session_v2_rolled_over_input_id") == str(row.id) and row.status == "rolled_over"
            and row.rolled_over_to_turn_id == metadata.get("turn_id")
        ))
        or row.status not in {"bound", "applied", "rolled_over"}
        or command is None or command.tenant_id != tenant_id or command.session_id != session_id
        or command.principal_type != "user" or command.principal_id != user_id
        or command.namespace != "human_input" or command.status not in {"accepted", "applied"}
        or str(command.id) != str(metadata.get("session_v2_command_id"))
    ):
        _refused("direct_human_input_required", "Creation requires a recoverable authenticated human input.")
    source = (command.target_json or {}).get("runtime_metadata", {}).get("source")
    if session.source_channel == "web" and session.runtime_source == "web_chat" and source in WEB_INPUT_SOURCES:
        return row, run
    if session.source_channel == "feishu" and session.runtime_source == "channel_chat" and source == "feishu":
        from app.models.channel_ingress_event import ChannelIngressEvent

        ingress = await db.get(ChannelIngressEvent, row.id)
        if (ingress is not None and ingress.tenant_id == tenant_id and ingress.provider == "feishu"
                and ingress.agent_id == agent_id and ingress.status in {"processing", "processed"}
                and ingress.result_session_id in {None, session_id} and ingress.result_runtime_task_id in {None, run_id}
                and (command.target_json or {}).get("runtime_metadata", {}).get("channel_ingress_event_id") == str(row.id)
                and ingress.external_principal_id is None):
            return row, run
    _refused("direct_human_input_required", "Relayed briefs and background work cannot authorize direct creation.")


async def _approval_valid(db, evidence, *, tenant_id, agent_id, user_id, input_hash, tool_name=TOOL_NAME):
    if not isinstance(evidence, dict):
        return False
    try:
        approval = await db.get(ApprovalRequest, uuid.UUID(str(evidence.get("approval_id"))))
    except (TypeError, ValueError):
        return False
    if (approval is None or approval.status != "approved" or approval.consumed_at is None
            or approval.tenant_id != tenant_id or approval.agent_id != agent_id or approval.requested_by != user_id
            or approval.tool_name != tool_name or approval.input_hash != input_hash
            or approval.decision_id != evidence.get("decision_id")
            or approval.execution_envelope_hash != evidence.get("envelope_hash")):
        return False
    live = await build_live_approval_policy_snapshot(db=db, tenant_id=tenant_id, agent_id=agent_id, tool_name=tool_name)
    return hash_policy_snapshot(live) == approval.policy_snapshot_hash == evidence.get("policy_snapshot_hash")


async def _require_policy(db, evidence):
    from app.services.capability_gate import check_capability
    from app.tools.guard_policy import evaluate_guard_policy
    from app.tools.governance import ToolGovernanceContext, _has_exact_session_grant
    from app.runtime.ccplus_contracts import build_permission_profile

    tenant_id, agent_id, user_id = (uuid.UUID(evidence[key]) for key in (
        "tenant_id", "source_agent_id", "requester_user_id",
    ))
    capability = await check_capability(db, tenant_id, agent_id, TOOL_NAME)
    approved = await _approval_valid(db, evidence.get("approval"), tenant_id=tenant_id, agent_id=agent_id,
                                    user_id=user_id, input_hash=evidence["tool_input_hash"])
    live = await build_live_approval_policy_snapshot(db=db, tenant_id=tenant_id, agent_id=agent_id, tool_name=TOOL_NAME)
    verdict = evaluate_guard_policy(tool_name=TOOL_NAME, arguments=evidence["tool_arguments"],
                                    external_visible=False, snapshot=live["guard_policy"])
    if verdict.decision == "deny":
        _refused("capability_denied", "Company GuardPolicy denies this creation request.")
    if verdict.decision == "require_approval" and not approved:
        _refused("approval_required", "Company GuardPolicy requires scoped approval.")
    if capability.denied:
        _refused("capability_denied", "Company policy denies employee creation.")
    profile = evidence.get("permission_profile") or {}
    exact_grant = _has_exact_session_grant(ToolGovernanceContext(
        agent_id=agent_id, user_id=user_id, tenant_id=str(tenant_id), tool_name=TOOL_NAME,
        arguments=evidence["tool_arguments"], permission_profile=build_permission_profile(profile),
    ))
    session_grant = exact_grant or TOOL_NAME in profile.get("allowed_tools", []) or profile.get("mode") == "bypass_permissions"
    if not approved and (capability.escalate_to_l3 or not capability.allowed) and not (
        not capability.policy_found and session_grant
    ):
        _refused("approval_required", "Scoped authorization is required before employee creation.")
    await require_local_write(db, evidence, approved=approved or exact_grant)


async def require_local_write(db, evidence, *, approved: bool):
    policy = await load_owner_action_policy(db, tenant_id=uuid.UUID(evidence["tenant_id"]),
                                          agent_id=uuid.UUID(evidence["source_agent_id"]), create_default=False)
    zone = policy.zone_for(ACTION_LOCAL_WRITE).value
    if not policy.valid or zone == "never_do":
        _refused("capability_denied", "The current Owner policy denies this local write.")
    if zone != "full_authority" and not (approved and evidence.get("owner_policy_hash") == policy.content_hash):
        _refused("approval_required", "The Owner requires confirmation of this local write.")


async def validate_direct_creation(db, draft, *, check_creation_policy: bool = True):
    from sqlalchemy.exc import SQLAlchemyError

    evidence = direct_creation_authorization(draft)
    if evidence is None:
        _refused("missing_confirmation_evidence", "Direct creation authorization is missing or changed.")
    row, _ = await _human_request(
        db, tenant_id=draft.tenant_id, user_id=draft.requested_by_user_id, agent_id=draft.hr_agent_id,
        session_id=draft.session_id, run_id=uuid.UUID(evidence["source_run_id"]), input_id=evidence["input_id"],
    )
    if row.content_hash != evidence["input_hash"]:
        _refused("runtime_authority_mismatch", "The original human input changed.")
    if check_creation_policy:
        try:
            await _require_policy(db, evidence)
        except SQLAlchemyError as exc:
            raise HrCreationConflict("capability_unavailable", "Creation policy is temporarily unavailable.") from exc
    return evidence


async def create_from_request(db, request):
    from app.services.assistant_binding import _approved_model
    from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task
    from app.services.owner_action_policy import load_owner_action_policy
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    context = request.context
    tenant_id, user_id, agent_id, session_id, run_id = (uuid.UUID(str(value)) for value in (
        context.tenant_id, context.user_id, context.agent_id, context.session_id, context.runtime_task_id,
    ))
    row, run = await _human_request(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                                  session_id=session_id, run_id=run_id)
    # One human input owns one immutable creation intent, including retries with a new tool-call id.
    key = f"direct-employee:{row.id}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
    existing = await db.scalar(select(HrCreationDraft).where(
        HrCreationDraft.tenant_id == tenant_id, HrCreationDraft.creation_idempotency_key == key,
    ))
    if existing is not None:
        return hr_creation_draft_payload(existing), False
    if run.status not in SESSION_TARGETABLE_RUN_STATUSES:
        _refused("source_execute_denied", "The originating user turn has ended.")
    policy = await load_owner_action_policy(db, tenant_id=tenant_id, agent_id=agent_id, create_default=False)
    evidence = {
        "schema": AUTHORIZATION_SCHEMA, "mode": "policy_direct", "package": "private_native_employee.v1",
        "tenant_id": str(tenant_id), "requester_user_id": str(user_id), "source_agent_id": str(agent_id),
        "source_session_id": str(session_id), "source_run_id": str(run_id), "input_id": str(row.id),
        "input_hash": row.content_hash, "tool_input_hash": hash_tool_input(TOOL_NAME, request.arguments),
        "tool_arguments": dict(request.arguments),
        "permission_profile": permission_profile_snapshot(context.permission_profile),
        "owner_policy_hash": policy.content_hash,
        "approval": asdict(context.approval_decision) if isinstance(context.approval_decision, ApprovalDecisionSet) else None,
        "first_task": str(request.arguments.get("first_task") or "").strip(),
    }
    # UUIDs in consumed approval evidence are identifiers, never credentials.
    if evidence["approval"]:
        evidence["approval"] = {key: str(value) if isinstance(value, uuid.UUID) else value
                                for key, value in evidence["approval"].items()}
    await _require_policy(db, evidence)
    state, model_id = await _approved_model(db, tenant_id)
    if state != "ok" or model_id is None:
        _refused("approved_model_unavailable", "Configure an approved company default model before creation.")
    arguments = request.arguments
    requester = await load_creation_requester(db, user_id)
    preview = _build_blueprint_preview_payload({
        "name": arguments["name"], "role_description": arguments["role_description"],
        "primary_users": [requester.display_name or requester.username], "core_outputs": arguments["core_outputs"],
        "boundaries": arguments["boundaries"], "focus_content": arguments["role_description"],
        "permission_scope": "self", "first_task_autostart": False,
    })
    # Only approved builtins/defaults belong to this package; recommendations never install authority.
    for field in ("skill_names", "effective_skill_names", "requested_skill_names", "deferred_skill_names"):
        preview["blueprint"][field] = []
    preview["will_install"] = []
    preview["creation_flow"] = {
        **preview["creation_flow"], "mode": "policy_direct",
        "instruction": (
            "The approved private package is authorized by this human request and current policy; no preview click is needed. "
            "Read the durable provisioning receipt. Creation is separate from first-task authorization and task completion."
        ),
    }
    preview["confirmation_requirements"] = {"required": False, "authorization_mode": "policy_direct"}
    preview["blueprint"]["approved_model_id"] = str(model_id)
    preview["blueprint_hash"] = canonical_hr_blueprint_hash(preview["blueprint"])
    evidence["blueprint_hash"] = preview["blueprint_hash"]
    evidence["approved_model_id"] = str(model_id)
    preview["creation_authorization"] = evidence
    draft = await upsert_hr_creation_draft(db, tenant_id=tenant_id, hr_agent_id=agent_id, session_id=session_id,
                                         requested_by_user_id=user_id, preview_payload=preview)
    draft.status = "confirmed"
    draft.creation_idempotency_key = key
    task = build_hr_provisioning_runtime_task(draft)
    from app.services.session_writer_epoch import assign_runtime_task_writer_generation

    task.budget_run_id = run.budget_run_id
    task.root_runtime_task_id = run.root_runtime_task_id or run.id
    await assign_runtime_task_writer_generation(db, task)
    db.add(task)
    await db.flush()
    draft.provisioning_task_id = task.id
    draft.provisioning_json = {"runtime_task_id": str(task.id), "runtime_status": "pending"}
    from app.core.policy import write_audit_event

    await write_audit_event(db, event_type="hr.creation_policy_authorized", severity="info", actor_type="user",
                           actor_id=user_id, tenant_id=tenant_id, action="employee.create.policy_direct",
                           resource_type="hr_creation_draft", resource_id=draft.id,
                           details={"input_id": str(row.id), "package": evidence["package"],
                                    "model_id": str(model_id), "first_task_requested": bool(evidence["first_task"])})
    await db.flush()
    return hr_creation_draft_payload(draft), True
