"""Recover an exact retained Skill submission through a current governed call."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.config_revision import ConfigRevision
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.config_versioning import canonical_content_hash, save_revision
from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.explicit_memory_authority import _actor, _writer_input, authorize_memory_audience, memory_effect_actor
from app.services.learned_skill_sources import CANDIDATE_ENTITY, _candidate_entity, _candidate_proof
from app.services.session_model_inputs import _model_dependencies, _stored_dependencies
from app.services.session_tool_runtime import _sha256

TOOLS = {"save_skill", "reconcile_skill_candidate"}


class SkillCandidateOperationError(ContentInputReceiptError):
    def __init__(self, code, message, operation_id, status_code=503):
        super().__init__(code, message, status_code)
        self.operation_id = str(operation_id)


async def _owned_operation(db, request, operation_id):
    tenant_id = uuid.UUID(str(request.context.tenant_id))
    _principal, actor, _session = await _actor(db, tenant_id, request)
    row = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
        ConfigRevision.entity_type == CANDIDATE_ENTITY, ConfigRevision.entity_id == uuid.UUID(str(operation_id)),
        ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))
    c = row.content if row is not None and isinstance(row.content, dict) else {}
    if (row is None or c.get("agent_id") != str(request.context.agent_id) or c.get("origin_kind") == "distillation"
        or c.get("authority", {}).get("owner_user_id") != str(actor.id)):
        raise ContentInputReceiptError("learned_skill_operation_denied", "Choose an operation owned by this requester.", 403)
    await _candidate_proof(db, row, tenant_id=tenant_id, agent_id=request.context.agent_id, allow_prepared=True)
    return row


def _outcome(row):
    c = row.content
    return {"schema": "hive.skill_candidate_operation.v1", "operation_id": str(row.entity_id),
        "candidate_id": c["candidate_id"], "status": c.get("phase", "captured"), "active_skill_created": False}


async def _record_result(request, content, *, source_row=None):
    tenant_id = uuid.UUID(str(request.context.tenant_id))
    async with tenant_scoped_session(tenant_id) as db:
        facts = await _writer_input(db, request, require_sources=False)
        _tid, _principal, actor, _session, invocation, model, _deps, args_hash = facts
        proof = {"schema": "hive.skill_candidate_operation_result.v1", "invocation_id": str(invocation.id),
            "model_result_id": str(model.id), "model_request_hash": model.model_request_hash,
            "args_hash": args_hash, "authority_hash": invocation.authority_snapshot_hash, "content_hash": _sha256(content),
            "sources": ([{"source_kind": "learned_skill_candidate", "source_id": str(source_row.id),
                "source_version": source_row.content_hash}] if source_row is not None else [])}
        identity = uuid.uuid5(invocation.id, "skill-candidate-operation-result")
        row = await db.get(InvocationSpan, identity, populate_existing=True)
        if row is None:
            db.add(InvocationSpan(id=identity, tenant_id=tenant_id, agent_id=request.context.agent_id, user_id=actor.id,
                session_id=str(invocation.session_id), runtime_task_id=invocation.run_id, trace_id=f"session-run:{invocation.run_id}",
                span_id=f"skill-candidate-result:{identity}", span_type="skill_candidate_result", name=request.tool_name,
                status="captured", duration_ms=0, started_at=datetime.now(timezone.utc), metadata_json={"skill_candidate_result": proof}))
        elif row.metadata_json != {"skill_candidate_result": proof}:
            raise ContentInputReceiptError("learned_skill_result_changed", "Reconcile the original Skill operation result.")
        await db.commit()
    return content


async def operation_result_sources(db, *, tenant_id, agent_id, invocation, model, content):
    row = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "skill-candidate-operation-result"), populate_existing=True)
    proof = (row.metadata_json or {}).get("skill_candidate_result") if row else None
    valid = (row is not None and row.tenant_id == tenant_id and row.agent_id == agent_id
        and row.runtime_task_id == invocation.run_id and row.session_id == str(invocation.session_id)
        and row.span_type == "skill_candidate_result" and row.name == invocation.tool_name and row.status == "captured"
        and invocation.tool_name in TOOLS and invocation.effect_state in {"effect_committed", "failed", "needs_reconciliation"}
        and isinstance(proof, dict) and proof.get("schema") == "hive.skill_candidate_operation_result.v1"
        and proof.get("invocation_id") == str(invocation.id) and proof.get("content_hash") == _sha256(content)
        and model is not None and proof.get("model_result_id") == str(model.id)
        and proof.get("model_request_hash") == model.model_request_hash
        and proof.get("args_hash") == (invocation.effective_args_hash or invocation.args_hash)
        and proof.get("authority_hash") == invocation.authority_snapshot_hash)
    sources = _stored_dependencies(proof.get("sources")) if valid else None
    if sources is None:
        return [], False
    for source in sources:
        if source["source_kind"] != "learned_skill_candidate":
            return [], False
        original = await db.get(ConfigRevision, uuid.UUID(source["source_id"]), populate_existing=True)
        try:
            await _candidate_proof(db, original, tenant_id=tenant_id, agent_id=agent_id)
            if original.content_hash != source["source_version"]:
                return [], False
        except ContentInputReceiptError:
            return [], False
    return sources, True


async def validate_recovery(db, row, *, tenant_id, agent_id):
    c, r = row.content, row.content["recovery"]
    try:
        invocation = await db.get(SessionToolInvocation, uuid.UUID(r["invocation_id"]), populate_existing=True)
        model = await db.get(SessionModelResult, uuid.UUID(r["model_result_id"]), populate_existing=True)
        task = await db.get(RuntimeTask, invocation.run_id, populate_existing=True) if invocation else None
        sources, complete = _model_dependencies(model)
        args = invocation.effective_arguments_json or invocation.provider_arguments_json if invocation else {}
        if (invocation is None or invocation.tenant_id != tenant_id or invocation.tool_name != "reconcile_skill_candidate"
            or invocation.effect_state != "effect_committed" or args.get("action") != "retry"
            or uuid.UUID(args["operation_id"]) != row.entity_id or task is None or task.tenant_id != tenant_id
            or str(task.root_user_id) != c["authority"]["owner_user_id"] or agent_id not in {task.parent_agent_id, task.child_agent_id}
            or str(invocation.session_id) not in {task.parent_session_id, task.child_session_id}
            or (invocation.effective_args_hash or invocation.args_hash) != r["args_hash"]
            or invocation.authority_snapshot_hash != r["authority_hash"]
            or model is None or model.tenant_id != tenant_id or model.session_id != invocation.session_id
            or model.run_id != invocation.run_id or model.provider_request_id != invocation.provider_request_id
            or model.model_request_hash != r["model_request_hash"] or not complete or sources != r["dependencies"]):
            raise ValueError("skill_recovery_unverified")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError("learned_skill_recovery_unverified", "Restore the canonical Skill recovery and its original inputs.", 403) from exc


async def operation_error(request, error):
    from app.tools.result_envelope import render_tool_error

    operation_id = getattr(error, "operation_id", None)
    if operation_id is None and request.tool_name == "save_skill":
        try:
            async with tenant_scoped_session(uuid.UUID(str(request.context.tenant_id))) as db:
                facts = await _writer_input(db, request, require_sources=False)
                candidate_id = f"save-skill-{uuid.uuid5(facts[4].id, 'learned-skill-candidate')}"
                operation_id = _candidate_entity(request.context.agent_id, candidate_id)
                row = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == facts[0],
                    ConfigRevision.entity_type == CANDIDATE_ENTITY, ConfigRevision.entity_id == operation_id,
                    ConfigRevision.is_active.is_(True)))
                if (row is None or row.content.get("invocation_id") != str(facts[4].id)
                    or row.content.get("args_hash") != facts[7]
                    or row.content.get("authority", {}).get("owner_user_id") != str(facts[2].id)):
                    operation_id = None
        except Exception:
            operation_id = None
    status = getattr(error, "status_code", 503)
    code = getattr(error, "code", "learned_skill_operation_unknown")
    approval_required = code == "learned_skill_recovery_approval_required"
    content = render_tool_error(tool_name=request.tool_name, provider="skill_candidate", http_status=status,
        error_class="approval_required" if approval_required else "auth_or_permission" if status == 403 else "provider_unavailable",
        message="The Skill candidate operation needs inspection or recovery.", retryable=status >= 500,
        actionable_hint="Use reconcile_skill_candidate to inspect or retry the retained exact plan; do not regenerate the method.",
        extra={"reason_code": code,
            "outcome": "approval_required" if approval_required else "denied" if status == 403 else "unknown",
            "recovery_tool": "reconcile_skill_candidate", "error_type": type(error).__name__,
            **({"operation_id": str(operation_id)} if operation_id is not None else {})})
    try:
        return await _record_result(request, content)
    except Exception:
        return content


async def reconcile_skill_operation(request, *, workspace):
    tenant_id, action = uuid.UUID(str(request.context.tenant_id)), request.arguments["action"]
    operation_id = request.arguments.get("operation_id")
    async with tenant_scoped_session(tenant_id) as db:
        _principal, actor, _session = await _actor(db, tenant_id, request)
        if operation_id is None:
            if action != "status":
                raise ContentInputReceiptError("learned_skill_operation_required", "Choose the retained original operation first.")
            rows = list(await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
                ConfigRevision.entity_type == CANDIDATE_ENTITY, ConfigRevision.is_active.is_(True),
                ConfigRevision.content["agent_id"].astext == str(request.context.agent_id),
                ConfigRevision.content["authority"]["owner_user_id"].astext == str(actor.id),
                ConfigRevision.content["invocation_id"].astext.is_not(None)).order_by(ConfigRevision.created_at, ConfigRevision.id)))
            result = {"schema": "hive.skill_candidate_operations.v1", "status": "ok", "operations": [_outcome(row) for row in rows]}
            source_row = None
        else:
            row = await _owned_operation(db, request, operation_id)
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"learned-skill-candidate:{tenant_id}:{row.content['candidate_id']}"})
            row = await _owned_operation(db, request, operation_id)
            if action == "retry":
                source_row = await _retry(db, request, row, workspace=Path(workspace))
                result = _outcome(source_row)
            elif action == "cancel":
                await memory_effect_actor(db, tenant_id, request)
                if row.content.get("phase", "captured") == "captured":
                    raise ContentInputReceiptError("learned_skill_already_captured", "Review or retire the captured candidate instead of cancelling its submission.")
                if row.content.get("phase") != "cancelled":
                    row = await save_revision(db, tenant_id=tenant_id, entity_type=CANDIDATE_ENTITY, entity_id=row.entity_id,
                        content={**row.content, "phase": "cancelled"}, change_source="tool", changed_by_user_id=actor.id,
                        changed_by_agent_id=request.context.agent_id, change_message="Owned pending Skill submission cancelled; evidence retained")
                    await db.commit()
                result, source_row = _outcome(row), None
            elif action == "status":
                result, source_row = _outcome(row), None
            else:
                raise ValueError("unsupported Skill operation action")
    return await _record_result(request, json.dumps(result, sort_keys=True), source_row=source_row)


async def _retry(db, request, row, *, workspace):
    from app.services.agent_tool_domains.workspace import _submit_skill_activation_candidate
    from app.services.workspace_resource_authority import open_workspace_file

    tenant_id, c = row.tenant_id, deepcopy(row.content)
    facts = await _writer_input(db, request)
    await memory_effect_actor(db, tenant_id, request)
    await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=request.context.agent_id,
        request=request, authority=c["authority"], mutation=True)
    await _validate_input_dependencies(db, tenant_id, c["dependencies"], runtime_request=request)
    if c.get("phase", "captured") == "captured":
        await _candidate_proof(db, row, tenant_id=tenant_id, agent_id=request.context.agent_id)
        return row
    if c.get("phase") != "prepared":
        raise ContentInputReceiptError("learned_skill_operation_cancelled", "The operation was cancelled; issue a new explicit request to learn a revised method.")
    plan = c.get("plan")
    if (not isinstance(plan, dict) or plan.get("schema") != "hive.skill_candidate_plan.v1"
        or canonical_content_hash(plan) != c.get("plan_hash")
        or hashlib.sha256(plan["draft_content"].encode()).hexdigest() != plan["draft_sha256"]
        or _sha256({"tool_name": "save_skill", "arguments": plan["arguments"]}) != c["args_hash"]
        or plan["draft_path"] != f"evolution/skill_candidates/{c['candidate_id']}/SKILL.md.draft"):
        raise SkillCandidateOperationError("learned_skill_plan_unavailable", "Restore the exact original retained plan; retry cannot recreate it.", row.entity_id, 409)
    await _submission_policy(db, request, plan["arguments"])
    def allowed(path):
        try:
            relative = Path(path).resolve().relative_to(workspace.resolve()).as_posix()
            return request.context.workspace_authority_scope.can_read(relative)
        except (ValueError, AttributeError):
            return False
    args, produced = plan["arguments"], []
    receipt = _submit_skill_activation_candidate(workspace, agent_id=request.context.agent_id,
        **{key: args[key] for key in ("name", "description", "instructions")},
        declared_tools=tuple(args.get("tools") or ()), declared_packs=tuple(args.get("packs") or ()),
        folder_name=c["folder_name"], overwrite=bool(args.get("overwrite", False)), candidate_id=c["candidate_id"],
        prepared_markdown=plan["draft_content"], source_authorizer=allowed, outcome_observer=produced.append)
    if not produced:
        raise SkillCandidateOperationError("learned_skill_write_unknown", "Inspect the retained native transaction before retrying.", row.entity_id)
    with open_workspace_file(workspace, plan["draft_path"]) as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    if digest != plan["draft_sha256"]:
        raise SkillCandidateOperationError("learned_skill_draft_changed", "Restore the exact original draft before retrying.", row.entity_id, 409)
    facts = await _writer_input(db, request)
    await memory_effect_actor(db, tenant_id, request)
    await _submission_policy(db, request, plan["arguments"])
    await _validate_input_dependencies(db, tenant_id, c["dependencies"], runtime_request=request)
    _tid, _principal, actor, _session, invocation, model, dependencies, args_hash = facts
    recovered = {**c, "phase": "captured", "draft_path": plan["draft_path"], "draft_sha256": digest, "receipt": receipt,
        "recovery": {"invocation_id": str(invocation.id), "model_result_id": str(model.id),
            "model_request_hash": model.model_request_hash, "authority_hash": invocation.authority_snapshot_hash,
            "args_hash": args_hash, "dependencies": dependencies}}
    row = await save_revision(db, tenant_id=tenant_id, entity_type=CANDIDATE_ENTITY, entity_id=row.entity_id,
        content=recovered, change_source="tool", changed_by_user_id=actor.id, changed_by_agent_id=request.context.agent_id,
        change_message="Original Skill candidate captured by an authorized exact-plan recovery")
    await db.commit()
    return row


async def _submission_policy(db, request, arguments):
    from app.services.approval_ticket import build_live_approval_policy_snapshot
    from app.tools.guard_policy import evaluate_guard_policy

    live = await build_live_approval_policy_snapshot(db=db, tenant_id=request.context.tenant_id,
        agent_id=request.context.agent_id, tool_name="save_skill")
    verdict = evaluate_guard_policy(tool_name="save_skill", arguments=arguments,
        external_visible=False, snapshot=live["guard_policy"])
    if live["capability"]["denied"] or verdict.decision == "deny":
        raise ContentInputReceiptError("learned_skill_recovery_denied", "Current policy denies the original Skill submission.", 403)
    if (live["capability"]["policy_found"] and live["capability"]["escalate_to_l3"]) or verdict.decision == "require_approval":
        raise ContentInputReceiptError("learned_skill_recovery_approval_required",
            "Resolve the current approval policy for the retained exact submission before retrying.", 409)
