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
    from app.services.content_input_receipts import ContentInputReceiptError
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
    await validate_creation_publication_inputs(db, draft=draft, dependencies=inputs[0], user=user, runtime_request=runtime_request)
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


async def published_creation_seed_sources(db, *, tenant_id, request):
    """Read only original creation seed sources for its live explicit first task."""
    from datetime import datetime, timezone
    from app.agents.delegation_token import DelegationToken, validate_delegation_token
    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.services.content_input_receipts import _runtime_input_actor
    from app.services.hr_creation_service import direct_creation_authorization

    context = getattr(request, "context", None)
    token = getattr(context, "delegation_token", None)
    if not isinstance(token, DelegationToken) or not validate_delegation_token(token, child_agent_id=context.agent_id).valid:
        return None, []
    try:
        task_id, session_id = uuid.UUID(str(context.runtime_task_id)), uuid.UUID(str(context.session_id))
    except (ValueError, TypeError):
        return None, []
    task = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == task_id, RuntimeTask.tenant_id == tenant_id,
        RuntimeTask.task_type == "delegation", RuntimeTask.child_agent_id == context.agent_id,
        RuntimeTask.root_user_id == context.user_id))
    if (task is None or uuid.UUID(task.child_session_id) != session_id or token.parent_agent_id != task.parent_agent_id):
        return None, []
    if not (task.status == 'running' and task.claim_expires_at is not None and task.claim_expires_at > datetime.now(timezone.utc)):
        from app.models.chat_session import ChatSession
        from app.services.shared_group_authority import _permission_execution_matches

        session = await db.get(ChatSession, session_id)
        if session is None or not await _permission_execution_matches(db, task=task, session=session, runtime_request=request):
            return None, []
    principal, requester = await _runtime_input_actor(db, tenant_id, request)
    if principal.purpose != "a2a_delegation":
        return None, []
    draft = await db.scalar(select(HrCreationDraft).where(HrCreationDraft.tenant_id == tenant_id,
        HrCreationDraft.created_agent_id == task.child_agent_id, HrCreationDraft.hr_agent_id == task.parent_agent_id,
        HrCreationDraft.session_id == uuid.UUID(task.parent_session_id), HrCreationDraft.status == "completed",
        HrCreationDraft.requested_by_user_id == requester.id))
    evidence = direct_creation_authorization(draft) if draft else None
    parent = await db.get(Agent, task.parent_agent_id, populate_existing=True)
    child = await db.get(Agent, task.child_agent_id, populate_existing=True)
    if draft is None or task.id != uuid.uuid5(draft.id, 'explicit-first-task.v1'):
        return await _owned_delegation_parent_sources(db, task=task, requester=requester, parent=parent, child=child)
    if (draft is None or task.id != uuid.uuid5(draft.id, "explicit-first-task.v1")
        or session_id != uuid.uuid5(task.id, "session") or not evidence or task.prompt != evidence["first_task"]
        or str(task.root_runtime_task_id) != evidence["source_run_id"]
        or parent is None or parent.tenant_id != tenant_id or parent.deleted_at is not None or parent.deactivated_at is not None
        or (parent.owner_user_id or parent.creator_id) != requester.id or child is None or child.tenant_id != tenant_id
        or child.deleted_at is not None or child.deactivated_at is not None or child.owner_user_id != requester.id):
        return None, []
    original = await load_employee_creation_dependencies(db, draft=draft)
    if original is None or not original[1]:
        return None, []
    return requester, await _creation_seed_dependencies(db, draft=draft, parent=parent, dependencies=original[0])


async def _creation_seed_dependencies(db, *, draft, parent, dependencies):
    published = []
    for dependency in dependencies:
        if await _owned_context_source(db, dependency=dependency, agent_id=parent.id, tenant_id=draft.tenant_id,
            session_id=draft.session_id, user_id=draft.requested_by_user_id):
            published.append(dependency)
        elif dependency['source_kind'] == "session_command":
            from app.models.session_v2 import SessionCommand
            from app.services.direct_employee_creation import _human_request
            from app.services.hr_creation_service import HrCreationConflict, direct_creation_authorization
            from app.services.content_input_receipts import ContentInputReceiptError

            try:
                command = await db.get(SessionCommand, uuid.UUID(dependency['source_id']), populate_existing=True)
            except ValueError:
                continue
            if command is not None and command.principal_type == "external_principal":
                evidence = direct_creation_authorization(draft)
                if evidence is None:
                    continue
                try:
                    original, _ = await _human_request(db, tenant_id=draft.tenant_id, user_id=draft.requested_by_user_id,
                        agent_id=draft.hr_agent_id, session_id=draft.session_id,
                        run_id=uuid.UUID(evidence["source_run_id"]), input_id=evidence["input_id"])
                except HrCreationConflict as exc:
                    raise ContentInputReceiptError("content_source_access_denied", "The original channel input is no longer authorized.", 403) from exc
                if command.id == original.command_id and original.content_hash == evidence["input_hash"]:
                    published.append(dependency)
    return published


async def _owned_context_source(db, *, dependency, agent_id, tenant_id, session_id, user_id):
    from app.models.invocation_span import InvocationSpan

    kind, source_id = dependency['source_kind'], dependency['source_id']
    allowed = {'agent_identity': {'identity'}, 'agent_context_resource': {'soul', 'a2a-collaborators', 'channels'},
        'agent_resident_memory': {'memory/self/self.md'}}
    parts = source_id.split('\n')
    if (kind in {*allowed, 'agent_platform_skill_file', 'owner_action_policy'} and parts[0] == str(agent_id)
        and len(parts) > 1 and (kind not in allowed or parts[1] in allowed[kind])):
        return True
    if kind == 'session_actor_identity':
        try:
            row = await db.get(InvocationSpan, uuid.UUID(source_id), populate_existing=True)
        except ValueError:
            return False
        return bool(row is not None and row.tenant_id == tenant_id and row.agent_id == agent_id
            and row.user_id == user_id and row.session_id == str(session_id))
    return False


async def _owned_delegation_parent_sources(db, *, task, requester, parent, child):
    from app.agents.orchestrator import _delegation_projection_request_from_record
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.services.delegation_context_sources import _facts
    from app.services.delegation_input_origin import load_delegation_instruction_origin

    if any(agent is None or agent.tenant_id != task.tenant_id or agent.owner_user_id != requester.id
        or agent.deleted_at is not None or agent.deactivated_at is not None for agent in (parent, child)):
        return None, []
    original_work = await db.get(ChatSession, uuid.UUID(task.parent_session_id), populate_existing=True)
    if (original_work is None or original_work.tenant_id != task.tenant_id or original_work.agent_id != parent.id
        or original_work.user_id != requester.id or original_work.audience_kind not in {None, 'private'}
        or await _facts(db, task) is None):
        return None, []
    original = _delegation_projection_request_from_record({'id': task.id, 'tenant_id': task.tenant_id,
        'parent_agent_id': task.parent_agent_id, 'child_agent_id': task.child_agent_id,
        'parent_session_id': task.parent_session_id, 'child_session_id': task.child_session_id,
        'root_user_id': task.root_user_id, 'metadata_json': task.metadata_json})
    if original is None:
        return None, []
    try:
        pointer = original.conversation_messages[0]['a2a_instruction_origin']
        retained = await db.get(InvocationSpan, uuid.UUID(pointer['source_id']))
    except (KeyError, IndexError, TypeError, ValueError):
        return None, []
    if retained is None or ((retained.metadata_json or {}).get('instruction_origin') or {}).get('schema') != 'hive.delegation_instruction_origin.v1':
        return None, []
    origin = await load_delegation_instruction_origin(db, request=original, tenant_id=task.tenant_id, content=task.prompt)
    if origin is None or origin.unbound:
        return None, []
    published = [source for source in origin.sources if await _owned_context_source(db, dependency=source,
        agent_id=parent.id, tenant_id=task.tenant_id, session_id=original_work.id, user_id=requester.id)]
    return requester, published


async def creation_tool_result_bound(db, *, tenant_id, agent_id, invocation, content):
    """Bind the native creation transaction result, retaining its original Session audience."""
    import json
    from app.services.hr_creation_service import direct_creation_authorization

    try:
        output = json.loads(content)
        draft = await db.get(HrCreationDraft, uuid.UUID(output["blueprint_id"]), populate_existing=True)
    except (ValueError, KeyError, TypeError):
        return False
    if (draft is None or draft.tenant_id != tenant_id or draft.hr_agent_id != agent_id
        or draft.session_id != invocation.session_id or output.get("blueprint_hash") != draft.blueprint_hash
        or output.get("blueprint_version") != draft.blueprint_version or output.get("blueprint") != draft.blueprint_json
        or output.get("hr_agent_id") != str(agent_id) or output.get("session_id") != str(invocation.session_id)
        or output.get("requested_by_user_id") != str(draft.requested_by_user_id)
        or output.get("provisioning_task_id") != str(draft.provisioning_task_id)):
        return False
    evidence = direct_creation_authorization(draft)
    if (not evidence or evidence.get("source_run_id") != str(invocation.run_id)
        or evidence.get("tool_arguments") != invocation.provider_arguments_json
        or output.get("creation_authorization") != evidence):
        return False
    inputs = await load_employee_creation_dependencies(db, draft=draft)
    return inputs is not None and inputs[1]


async def validate_creation_publication_inputs(db, *, draft, dependencies, user, runtime_request,
    creation_dependencies=None, active_workspace_receipts=frozenset()):
    from app.models.agent import Agent
    from app.models.session_v2 import SessionCommand
    from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor, _validate_input_dependencies

    if runtime_request is None:
        await _validate_input_dependencies(db, draft.tenant_id, dependencies, user=user,
            active_workspace_receipts=active_workspace_receipts)
        return
    principal, requester = await _runtime_input_actor(db, draft.tenant_id, runtime_request)
    if principal.agent_id != draft.created_agent_id or requester.id != draft.requested_by_user_id:
        await _validate_input_dependencies(db, draft.tenant_id, dependencies, runtime_request=runtime_request,
            active_workspace_receipts=active_workspace_receipts)
        return
    parent = await db.get(Agent, draft.hr_agent_id, populate_existing=True)
    child = await db.get(Agent, draft.created_agent_id, populate_existing=True)
    if (parent is None or child is None or parent.tenant_id != draft.tenant_id or child.tenant_id != draft.tenant_id
        or parent.deleted_at is not None or parent.deactivated_at is not None
        or child.deleted_at is not None or child.deactivated_at is not None
        or (parent.owner_user_id or parent.creator_id) != requester.id or child.owner_user_id != requester.id):
        raise ContentInputReceiptError("content_source_access_denied", "The original employee profile is no longer authorized.", 403)
    creation_dependencies = dependencies if creation_dependencies is None else creation_dependencies
    published = await _creation_seed_dependencies(db, draft=draft, parent=parent, dependencies=creation_dependencies)
    for dependency in creation_dependencies:
        if dependency["source_kind"] == "session_command":
            try:
                command_id = uuid.UUID(dependency["source_id"])
            except ValueError:
                continue
            command = await db.scalar(select(SessionCommand.id).where(SessionCommand.id == command_id,
                SessionCommand.tenant_id == draft.tenant_id, SessionCommand.session_id == draft.session_id,
                SessionCommand.principal_type == "user", SessionCommand.principal_id == requester.id,
                SessionCommand.namespace == "human_input"))
            if command is not None:
                published.append(dependency)
    # The authenticated creation publishes its profile to this employee; it grants no upstream tool or KB access.
    for dependency in dependencies:
        await _validate_input_dependencies(db, draft.tenant_id, [dependency],
            user=requester if dependency in published else None,
            runtime_request=None if dependency in published else runtime_request,
            active_workspace_receipts=active_workspace_receipts)
