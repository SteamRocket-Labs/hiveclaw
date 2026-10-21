"""Continue an exact reviewed Skill through the existing governed save tool."""

import hashlib
import json
from pathlib import Path
import uuid

from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.config_revision import ConfigRevision
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.explicit_memory_authority import _actor, _writer_input, memory_effect_actor
from app.services.learned_skill_sources import CANDIDATE_ENTITY, authorize_candidate


def activation_arguments(row, *, workspace):
    from app.evals.artifact_gate import artifact_gate_passed
    from app.services.workspace_resource_authority import open_workspace_file

    c = row.content
    plan = c.get("activation") or {}
    gates = plan.get("gates") or {}
    if (plan.get("schema") != "hive.reviewed_skill_activation.v1" or gates.get("verification_passed") is not True
        or not artifact_gate_passed(gates.get("artifact_gate_report") or {})
        or (gates.get("referee_review") or {}).get("decision") != "approve"):
        raise ContentInputReceiptError("learned_skill_activation_evidence_unavailable",
            "Restore the exact retained activation metadata and successful gate evidence.", 503)
    with open_workspace_file(Path(workspace), c["draft_path"]) as source:
        body = source.read()
    if hashlib.sha256(body).hexdigest() != c["draft_sha256"]:
        raise ContentInputReceiptError("learned_skill_draft_changed", "Restore the exact reviewed Skill bytes.", 403)
    return {"reviewed_revision_id": str(row.id), "name": plan["name"], "description": plan["description"],
        "instructions": body.decode("utf-8"), "tools": plan["tools"], "packs": plan["packs"],
        "folder_name": c["folder_name"], "overwrite": False}


def approved_activation(request, *, arguments, policy):
    """Accept only the current runtime's consumed same-tool, same-input decision."""
    from app.services.approval_ticket import ApprovalDecisionSet, hash_policy_snapshot, hash_tool_input

    if request is None or request.tool_name != "save_skill" or request.arguments != arguments:
        return False
    decision = request.context.approval_decision
    return (isinstance(decision, ApprovalDecisionSet) and decision.tool_name == "save_skill"
        and str(decision.tenant_id) == str(request.context.tenant_id)
        and decision.requested_by_user_id == request.context.user_id
        and decision.action_type in {"agent.skill.write", "guard_policy:save_skill"}
        and decision.input_hash == hash_tool_input("save_skill", arguments)
        and decision.policy_snapshot_hash == hash_policy_snapshot(policy))


async def _owned_review(db, request, revision_id):
    tenant_id = uuid.UUID(str(request.context.tenant_id))
    _principal, actor, _session = await _actor(db, tenant_id, request)
    try:
        identity = uuid.UUID(str(revision_id))
    except ValueError as exc:
        raise ContentInputReceiptError("learned_skill_review_denied", "Choose an exact reviewed revision reference.", 403) from exc
    row = await db.get(ConfigRevision, identity, populate_existing=True)
    c = row.content if row is not None and isinstance(row.content, dict) else {}
    if (row is None or row.tenant_id != tenant_id or row.entity_type != CANDIDATE_ENTITY
        or c.get("origin_kind") != "distillation" or c.get("agent_id") != str(request.context.agent_id)
        or c.get("authority", {}).get("owner_user_id") != str(actor.id)):
        raise ContentInputReceiptError("learned_skill_review_denied", "Choose a reviewed version owned by this requester.", 403)
    await authorize_candidate(db, row, tenant_id=tenant_id, agent_id=request.context.agent_id,
        request=request, check_draft=True)
    return row


async def _installation(db, request, row):
    from app.services.learned_skill_sources import validate_learned_installation
    from app.services.skill_source_receipts import _installation_id

    installed = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == row.tenant_id,
        ConfigRevision.entity_type == "agent_skill_install",
        ConfigRevision.entity_id == _installation_id(request.context.agent_id, row.content["folder_name"]),
        ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))
    if installed is not None:
        await validate_learned_installation(db, installed, tenant_id=row.tenant_id,
            agent_id=request.context.agent_id, request=request)
        if (installed.content.get("learning") or {}).get("candidate_revision_id") != str(row.id):
            raise ContentInputReceiptError("learned_skill_installation_changed", "Reconcile the original reviewed installation.", 409)
    return installed


async def confirm_reviewed_installation(*, frame, workspace, candidate_id, target_path):
    """Read back a committed projection without reissuing the native effect."""
    from app.models.ai_asset import AIAssetRecord
    from app.services.config_versioning import canonical_content_hash
    from app.services.learned_skill_lifecycle import native_lifecycle_projection
    from app.services.learned_skill_sources import _revision_valid, validate_learned_installation
    from app.services.skill_source_receipts import _installation_id

    try:
        async with tenant_scoped_session(frame.tenant_id) as db:
            folder = Path(target_path).parts[1]
            installed = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == frame.tenant_id,
                ConfigRevision.entity_type == "agent_skill_install",
                ConfigRevision.entity_id == _installation_id(frame.agent_id, folder), ConfigRevision.is_active.is_(True)))
            if installed is None or installed.content["learning"]["candidate_revision_id"] != str(frame.candidates[candidate_id]):
                return False
            await validate_learned_installation(db, installed, tenant_id=frame.tenant_id,
                agent_id=frame.agent_id, request=frame.reader)
            record = await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == frame.tenant_id,
                AIAssetRecord.native_key == f"skill:agent:{frame.agent_id}:{folder}"))
            if record is None or record.projection_status != "applied" or record.projection_error is not None:
                return False
            snapshot = await native_lifecycle_projection(db, record, reader=frame.reader)
            active = await db.get(ConfigRevision, record.active_revision_id)
            return (snapshot is not None and canonical_content_hash(snapshot[0].content) == record.content_hash
                and _revision_valid(active, tenant_id=frame.tenant_id, entity_type="ai_asset")
                and active.entity_id == record.id and active.content_hash == record.content_hash and active.is_active)
    except Exception:
        return False


async def inspect_reviewed_skill(request, *, workspace):
    from app.services.skill_candidate_operations import _record_result

    tenant_id = uuid.UUID(str(request.context.tenant_id))
    selected = request.arguments.get("reviewed_revision_id")
    async with tenant_scoped_session(tenant_id) as db:
        _principal, actor, _session = await _actor(db, tenant_id, request)
        if selected:
            row = await _owned_review(db, request, selected)
            arguments = activation_arguments(row, workspace=workspace)
            installed = await _installation(db, request, row)
            result = {"schema": "hive.reviewed_skill_inspection.v1", "status": "installed" if installed else "pending",
                "reviewed_revision_id": str(row.id), "content_hash": row.content_hash,
                "draft_sha256": row.content["draft_sha256"], "gate_evidence": row.content["activation"]["gates"],
                "activation_arguments": arguments}
        else:
            rows = await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
                ConfigRevision.entity_type == CANDIDATE_ENTITY, ConfigRevision.is_active.is_(True),
                ConfigRevision.content["agent_id"].astext == str(request.context.agent_id),
                ConfigRevision.content["origin_kind"].astext == "distillation",
                ConfigRevision.content["authority"]["owner_user_id"].astext == str(actor.id))
                .order_by(ConfigRevision.created_at, ConfigRevision.id))
            candidates = []
            for candidate in rows:
                try:
                    await _owned_review(db, request, candidate.id)
                    if await _installation(db, request, candidate) is None:
                        candidates.append({"reviewed_revision_id": str(candidate.id),
                            "content_hash": candidate.content_hash, "draft_sha256": candidate.content["draft_sha256"]})
                except ContentInputReceiptError:
                    continue
            result, row = {"schema": "hive.reviewed_skill_inspection.v1", "status": "ok", "candidates": candidates}, None
    return await _record_result(request, json.dumps(result, sort_keys=True), source_row=row)


async def activate_reviewed_skill(request, *, workspace):
    from app.services.learned_skill_sources import SkillReviewInputs, _REVIEW
    from app.services.skill_candidate_operations import _record_result
    from app.services.skill_distiller import _commit_skill_with_asset_revision
    from app.services.skill_source_receipts import SkillInputCapture, load_skill_input_snapshot

    tenant_id = uuid.UUID(str(request.context.tenant_id))
    async with tenant_scoped_session(tenant_id) as db:
        facts = await _writer_input(db, request)
        await memory_effect_actor(db, tenant_id, request)
        row = await _owned_review(db, request, request.arguments["reviewed_revision_id"])
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"reviewed-skill-activation:{tenant_id}:{row.id}"})
        row = await _owned_review(db, request, row.id)
        arguments = activation_arguments(row, workspace=workspace)
        if request.arguments != arguments:
            raise ContentInputReceiptError("learned_skill_activation_arguments_changed",
                "Copy the exact activation_arguments from inspect_reviewed_skill; revised content requires another review.", 403)
        path = f"skills/{row.content['folder_name']}/SKILL.md"
        installed = await _installation(db, request, row)
        if installed is None:
            frame = SkillReviewInputs(agent_id=request.context.agent_id, tenant_id=tenant_id, reader=request,
                candidates={row.content["candidate_id"]: row.id}, dependencies=[*row.content["dependencies"], *facts[6]],
                scope=request.context.workspace_authority_scope, skill_capture=SkillInputCapture(request.context.agent_id, allow_empty=True),
                activation_request=request)
            await load_skill_input_snapshot(frame.skill_capture, tenant_id, runtime_request=request)
            token = _REVIEW.set(frame)
            try:
                result = await _commit_skill_with_asset_revision(workspace=workspace, target_relative_path=path,
                    rendered_markdown=arguments["instructions"], skill_name=arguments["name"], overwrite=False,
                    status="provisional", candidate_id=row.content["candidate_id"],
                    skill_origin=row.content["activation"]["skill_origin"], agent_id=request.context.agent_id,
                    tenant_id=tenant_id, source_candidate_id=row.content["candidate_id"])
                if "✅" not in result:
                    raise ContentInputReceiptError("learned_skill_activation_pending", "Reconcile the retained exact activation: " + result, 503)
            finally:
                _REVIEW.reset(token)
        result = {"schema": "hive.reviewed_skill_activation_result.v1", "reviewed_revision_id": str(row.id),
            "status": "already_installed" if installed else "provisional", "skill_path": path,
            "active_skill_created": installed is None}
    return await _record_result(request, json.dumps(result, sort_keys=True), source_row=row)
