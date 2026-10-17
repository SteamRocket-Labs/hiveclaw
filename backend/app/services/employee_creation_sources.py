"""Original HR-generated context, with creating-model inputs kept distinct from authorship."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy import select

from app.models.config_revision import ConfigRevision
from app.models.hr_creation import HrCreationDraft
from app.services.config_versioning import canonical_content_hash, save_revision
from app.services.delegation_input_origin import load_employee_creation_dependencies


async def record_employee_creation_context(db, *, draft, agent, workspace, template_dir, model_used, refinement=None):
    from app.services.agent_context import _strip_primary_heading, agent_identity_content
    from app.services.hr_creation_service import direct_creation_authorization
    from app.services.workspace_resource_authority import open_workspace_file

    inputs = await load_employee_creation_dependencies(db, draft=draft)
    if inputs is None:
        return
    if Path(template_dir).resolve() == (Path(__file__).resolve().parents[2] / "agent_template").resolve():
        from app.services.skill_source_receipts import record_original_platform_skill_sources

        await record_original_platform_skill_sources(db, agent=agent, workspace=workspace)
    prior = await db.scalar(select(ConfigRevision.id).where(ConfigRevision.tenant_id == draft.tenant_id,
        ConfigRevision.entity_type == "agent_hr_bootstrap", ConfigRevision.entity_id == agent.id).limit(1))
    if prior is not None:
        return
    with open_workspace_file(workspace, "soul.md") as source:
        soul = source.read()
    with open_workspace_file(workspace, "memory/self/self.md") as source:
        resident_hash = hashlib.file_digest(source, "sha256").hexdigest()
    dependencies, complete = inputs
    refined_complete = model_used is False or await _refinement_is_bound(db, draft=draft, refinement=refinement)
    await save_revision(db, entity_type="agent_hr_bootstrap", entity_id=agent.id, tenant_id=draft.tenant_id,
        content={"schema": "hive.employee_creation_context.v1", "draft_id": str(draft.id),
            "creation_origin": direct_creation_authorization(draft)["creation_origin"],
            "identity": agent_identity_content(agent.name, agent.role_description),
            "soul": {"content_sha256": hashlib.sha256(soul).hexdigest(),
                "resource_sha256": hashlib.sha256(_strip_primary_heading(soul.decode().strip()).strip().encode()).hexdigest()},
            "resident_sources": {"memory/self/self.md": resident_hash}, "dependencies": dependencies, "refinement": refinement,
            "unbound": ([] if complete else ["creating_model_inputs_unbound"])
                + ([] if refined_complete else ["hr_refinement_sources_unbound"])},
        change_source="hr_provisioning", changed_by_agent_id=draft.hr_agent_id,
        change_message="Original HR context captured with creating-model sources")


async def load_employee_creation_context(db, *, tenant_id, agent_id, user, runtime_request):
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.hr_creation_service import direct_creation_authorization

    revision = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
        ConfigRevision.entity_type == "agent_hr_bootstrap", ConfigRevision.entity_id == agent_id,
        ConfigRevision.version == 1, ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))
    content = revision.content if revision is not None and isinstance(revision.content, dict) else {}
    try:
        draft_id = uuid.UUID(content["draft_id"])
    except (ValueError, KeyError, TypeError):
        draft_id = None
    draft = await db.get(HrCreationDraft, draft_id, populate_existing=True) if draft_id else None
    evidence = direct_creation_authorization(draft) if draft else None
    inputs = await load_employee_creation_dependencies(db, draft=draft) if draft else None
    if (revision is None or content.get("schema") != "hive.employee_creation_context.v1"
        or canonical_content_hash(content) != revision.content_hash or revision.change_source != "hr_provisioning"
        or revision.changed_by_user_id is not None or draft is None or draft.tenant_id != tenant_id
        or draft.created_agent_id != agent_id or draft.status != "completed" or not evidence
        or revision.changed_by_agent_id != draft.hr_agent_id or content.get("creation_origin") != evidence.get("creation_origin")
        or inputs is None or not inputs[1] or content.get("dependencies") != inputs[0] or content.get("unbound") != []):
        raise ContentInputReceiptError("content_source_unavailable", "The HR context lacks complete original creation sources.", 503)
    if content.get("refinement") is not None and not await _refinement_is_bound(db, draft=draft, refinement=content["refinement"]):
        raise ContentInputReceiptError("content_source_unavailable", "The original refinement call no longer matches its receipt.", 503)
    await _validate_input_dependencies(db, tenant_id, inputs[0], user=user, runtime_request=runtime_request)
    return content


async def _refinement_is_bound(db, *, draft, refinement):
    from app.models.hr_creation import HrProvisioningStep
    from app.services.hr_creation_service import _step_input_hash
    from app.tools.handlers.hr import _SOUL_REFINE_PROMPT, _parse_list, _trim_role_description_for_prompt_guard

    if not isinstance(refinement, dict) or refinement.get("draft_id") != str(draft.id) or refinement.get("blueprint_hash") != draft.blueprint_hash:
        return False
    call = refinement.get("call")
    args = draft.blueprint_json or {}
    inputs = {"name": (args.get("name") or "").strip(),
        "role_description": _trim_role_description_for_prompt_guard(args.get("role_description", "")),
        "personality": args.get("personality", ""), "boundaries": args.get("boundaries", ""),
        "primary_users": _parse_list(args.get("primary_users")), "core_outputs": _parse_list(args.get("core_outputs"))}
    if (not isinstance(call, dict) or call.get("schema") != "hive.hr_refinement_call.v1" or call.get("status") != "completed"
        or call.get("inputs") != inputs or call.get("sha256") != canonical_content_hash({k: v for k, v in call.items() if k != "sha256"})
        or call.get("output") != (draft.provisioning_json or {}).get("refined_inputs")):
        return False
    prompt = _SOUL_REFINE_PROMPT.format(**{k: (", ".join(v) if isinstance(v, list) else v) or "(not specified)"
        for k, v in inputs.items()})
    request = call.get("request")
    if (not isinstance(request, dict) or set(request) != {"provider", "model", "messages", "temperature", "max_tokens"}
        or not all(isinstance(request.get(k), str) and request[k] for k in ("provider", "model"))
        or request["messages"] != [{"role": "user", "content": prompt}]
        or not isinstance(call.get("response_content"), str) or not call["response_content"].strip()):
        return False
    step = await db.scalar(select(HrProvisioningStep).where(HrProvisioningStep.tenant_id == draft.tenant_id,
        HrProvisioningStep.draft_id == draft.id, HrProvisioningStep.step_key == "model",
        HrProvisioningStep.step_kind == "model", HrProvisioningStep.status == "completed").execution_options(populate_existing=True))
    return (step is not None and step.input_hash == _step_input_hash(draft.blueprint_hash, "model")
        and (step.receipt_json or {}).get("model_used") is True and (step.receipt_json or {}).get("refinement") == refinement)
