"""Original HR-generated context, with creating-model inputs kept distinct from authorship."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy import select

from app.models.config_revision import ConfigRevision
from app.models.hr_creation import HrCreationDraft
from app.services.config_versioning import canonical_content_hash, save_revision
from app.services.delegation_input_origin import load_employee_creation_dependencies


async def record_employee_creation_context(db, *, draft, agent, workspace, template_dir, model_used):
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
    await save_revision(db, entity_type="agent_hr_bootstrap", entity_id=agent.id, tenant_id=draft.tenant_id,
        content={"schema": "hive.employee_creation_context.v1", "draft_id": str(draft.id),
            "creation_origin": direct_creation_authorization(draft)["creation_origin"],
            "identity": agent_identity_content(agent.name, agent.role_description),
            "soul": {"content_sha256": hashlib.sha256(soul).hexdigest(),
                "resource_sha256": hashlib.sha256(_strip_primary_heading(soul.decode().strip()).strip().encode()).hexdigest()},
            "resident_sources": {"memory/self/self.md": resident_hash}, "dependencies": dependencies,
            "unbound": ([] if complete else ["creating_model_inputs_unbound"])
                + ([] if model_used is False else ["hr_refinement_sources_unbound"])},
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
    await _validate_input_dependencies(db, tenant_id, inputs[0], user=user, runtime_request=runtime_request)
    return content
