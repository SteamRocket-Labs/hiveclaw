"""Trusted content receipts for the two file-backed carriers in the M1 contract."""

import hashlib
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from sqlalchemy import or_, select, text

from app.models.organization_identity import ContentInputReceipt
from app.services.team_memory import _safe_component, _slugify

_ACTIVE_GROUP_TOOL_SOURCES = ContextVar("active_group_tool_sources", default=frozenset())
_DISCLOSURE_SOURCE_VALIDATIONS = ContextVar("disclosure_source_validations", default=None)


@contextmanager
def group_source_read():
    """Reuse successful exact source checks only inside one reader disclosure."""
    token = _DISCLOSURE_SOURCE_VALIDATIONS.set(set())
    try:
        yield
    finally:
        _DISCLOSURE_SOURCE_VALIDATIONS.reset(token)


class ContentInputReceiptError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def team_memory_ref(workspace_key: str, key: str) -> str:
    return f"{workspace_key}\n{_slugify(key)}"


async def lock_team_memory_write(db, tenant_id: uuid.UUID, workspace_key: str, key: str) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"team-memory:{tenant_id}:{_safe_component(workspace_key)}:{_slugify(key)}"})


async def latest_team_memory_receipts(db, tenant_id: uuid.UUID, workspace_key: str, entries) -> dict:
    refs = [team_memory_ref(workspace_key, Path(entry.path).stem) for entry in entries]
    if not refs:
        return {}
    rows = (await db.scalars(select(ContentInputReceipt).where(
        ContentInputReceipt.tenant_id == tenant_id,
        ContentInputReceipt.carrier_kind == "team_memory",
        ContentInputReceipt.carrier_ref.in_(refs),
    ).distinct(ContentInputReceipt.carrier_ref).order_by(
        ContentInputReceipt.carrier_ref, ContentInputReceipt.revision.desc(),
    ))).all()
    return {row.carrier_ref: row for row in rows}


async def validate_browser_input_dependencies(db, tenant_id: uuid.UUID, user, dependencies: list) -> None:
    # Browser authority, including PDEC-013, must not authorize Agent prompt consumption.
    await _validate_input_dependencies(db, tenant_id, dependencies, user=user)


async def validate_runtime_input_dependencies(db, request, dependencies: list) -> None:
    await _validate_input_dependencies(db, uuid.UUID(str(request.context.tenant_id)), dependencies,
        runtime_request=request)


async def _validate_input_dependencies(db, tenant_id: uuid.UUID, dependencies: list, *, user=None, runtime_request=None,
    active_workspace_receipts: frozenset[uuid.UUID] = frozenset()) -> None:
    verified = _DISCLOSURE_SOURCE_VALIDATIONS.get()
    context = {"user": user, "runtime_request": runtime_request, "active_workspace_receipts": active_workspace_receipts}
    if verified is None or not isinstance(dependencies, list) or any(
        not isinstance(row, dict) or set(row) != {"source_kind", "source_id", "source_version"}
        or any(not isinstance(value, str) or not value for value in row.values()) for row in dependencies
    ):
        return await _validate_input_dependency_batch(db, tenant_id, dependencies, **context)
    for dependency in dependencies:
        source_key = (dependency["source_kind"], dependency["source_id"], dependency["source_version"])
        reader_key = (id(db), tenant_id, id(user), id(runtime_request), active_workspace_receipts, source_key)
        if (reader_key not in verified or (source_key[0] == "session_tool_invocation"
            and source_key[1:] in _ACTIVE_GROUP_TOOL_SOURCES.get())):
            await _validate_input_dependency_batch(db, tenant_id, [dependency], **context)
            verified.add(reader_key)


async def _validate_input_dependency_batch(db, tenant_id: uuid.UUID, dependencies: list, *, user=None, runtime_request=None,
    active_workspace_receipts: frozenset[uuid.UUID] = frozenset()) -> None:
    from app.models.knowledge import KnowledgeDocument
    from app.services.personal_knowledge_access import HumanBrowserPrincipal
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    if not isinstance(dependencies, list) or any(
        not isinstance(row, dict) or set(row) != {"source_kind", "source_id", "source_version"}
        or any(not isinstance(value, str) or not value for value in row.values()) for row in dependencies
    ):
        raise ContentInputReceiptError("content_source_unavailable", "The source receipt is invalid.", 503)
    published_user, published = None, []
    if runtime_request is not None:
        from app.services.employee_creation_sources import published_creation_seed_sources

        published_user, published = await published_creation_seed_sources(db, tenant_id=tenant_id, request=runtime_request)
    for dependency in dependencies:
        if dependency in published:
            await _validate_input_dependencies(db, tenant_id, [dependency], user=published_user,
                active_workspace_receipts=active_workspace_receipts)
            continue
        if dependency["source_kind"] == "project_resource":
            from app.services.project_context import validate_project_dependency

            await validate_project_dependency(db, tenant_id, dependency, user=user, runtime_request=runtime_request,
                active_receipts=active_workspace_receipts)
            continue
        if dependency["source_kind"] in {"agent_skill_file", "agent_platform_skill_file"}:
            from app.services.skill_source_receipts import validate_skill_file_dependency

            await validate_skill_file_dependency(db, tenant_id, dependency, user=user, runtime_request=runtime_request)
            continue
        if dependency["source_kind"] in {"agent_context_resource", "agent_resident_memory", "agent_identity", "owner_action_policy"}:
            await _validate_agent_context_dependency(db, tenant_id, dependency, user=user, runtime_request=runtime_request)
            continue
        if dependency["source_kind"] == "workspace_artifact":
            await _validate_workspace_dependency(db, tenant_id, dependency, user=user, runtime_request=runtime_request,
                active_receipts=active_workspace_receipts)
            continue
        if dependency["source_kind"] == "session_actor_identity":
            await _validate_session_actor_identity(db, tenant_id, dependency, user=user, runtime_request=runtime_request)
            continue
        if dependency["source_kind"] == "delegation_context":
            from app.services.delegation_context_sources import validate_delegation_context_source

            await validate_delegation_context_source(db, tenant_id, dependency, user=user, runtime_request=runtime_request)
            continue
        if dependency["source_kind"] in {"session_command", "session_tool_invocation"}:
            await _validate_session_dependency(db, tenant_id, dependency, user=user, runtime_request=runtime_request,
                active_workspace_receipts=active_workspace_receipts)
            continue
        if dependency["source_kind"] in {"runtime_result", "runtime_result_page", "runtime_team_context"}:
            from app.services.runtime_result_sources import validate_runtime_return_dependency

            await validate_runtime_return_dependency(db, tenant_id, dependency, user=user, runtime_request=runtime_request)
            continue
        if dependency["source_kind"] == "company_knowledge_publication":
            await _validate_company_publication_dependency(db, tenant_id, dependency, user=user, runtime_request=runtime_request)
            continue
        if dependency["source_kind"] != "knowledge_document":
            raise ContentInputReceiptError("content_source_unavailable", "A source receipt cannot yet be revalidated. Regenerate from verifiable inputs.", 503)
        try:
            source_id = uuid.UUID(dependency["source_id"])
        except ValueError as exc:
            raise ContentInputReceiptError("content_source_unavailable", "The source receipt is invalid.", 503) from exc
        document = (await db.scalars(select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.id == source_id,
        ).execution_options(populate_existing=True))).one_or_none()
        if document is None or document.canonical_md_sha256 != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "A source is missing or has changed. Regenerate from current authorized inputs.", 403)
        if document.scope_type == "person":
            if runtime_request is not None:
                principal, _requester = await _runtime_input_actor(db, tenant_id, runtime_request)
            else:
                principal = HumanBrowserPrincipal(user_id=user.id, role=user.role, home_tenant_id=tenant_id)
            result = await PersonalKnowledgeService().get_personal_document_with_authority(db,
                tenant_id=tenant_id, owner_user_id=document.scope_id, document_id=document.id,
                principal=principal)
        elif document.scope_type == "company":
            if runtime_request is not None:
                raise ContentInputReceiptError("content_source_unavailable",
                    "This source requires its immutable Company Knowledge publication reader.", 503)
            from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest
            from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal

            result = await CompanyKnowledgeGateway().read(db,
                principal=CompanyKnowledgePrincipal(tenant_id=tenant_id, accountable_user_id=user.id,
                    accountable_role=user.role, actor_type="user", actor_id=user.id,
                    department_id=getattr(user, "department_id", None), purpose="interactive_session"),
                request=CompanyKnowledgeReadRequest(document_id=document.id, publication_id=None, segment_ids=(),
                    max_chars=None, trace_id=f"content-input:{uuid.uuid4().hex}"))
        else:
            raise ContentInputReceiptError("content_source_unavailable", "This source requires its canonical audience reader.", 503)
        if result.status != "ok" or getattr(result, "credential_reference", None):
            raise ContentInputReceiptError("content_source_access_denied", "A required source is no longer authorized for this reader.", 403)
        _validate_source_bytes(document.canonical_md_path, dependency["source_version"])


async def _validate_agent_context_dependency(db, tenant_id, dependency, *, user, runtime_request):
    from fastapi import HTTPException
    from app.core.permissions import check_agent_access
    from app.models.agent import Agent
    from app.services.agent_context import load_agent_context_resource

    try:
        raw_agent_id, ref = dependency["source_id"].split("\n")
        agent_id = uuid.UUID(raw_agent_id)
        resident = dependency["source_kind"] == "agent_resident_memory"
        identity = dependency["source_kind"] == "agent_identity"
        policy_source = dependency["source_kind"] == "owner_action_policy"
        if policy_source:
            policy_id = uuid.UUID(ref)
        elif ref not in ({"identity"} if identity else {"memory/self/self.md"} if resident else {"channels", "soul", "a2a-collaborators"}):
            raise ValueError("unsupported_context_source")
    except ValueError as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The context source receipt is unsupported.", 503) from exc
    try:
        if runtime_request is not None:
            principal, _requester = await _runtime_input_actor(db, tenant_id, runtime_request)
            if principal.agent_id != agent_id:
                from app.services.project_context import project_source_read_allowed

                if not project_source_read_allowed(tenant_id, principal):
                    raise ValueError("context_source_actor_mismatch")
                source = await db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
                    .execution_options(populate_existing=True))
                if source is None:
                    raise ValueError("context_source_agent_unavailable")
                await check_agent_access(db, _requester, agent_id)
        else:
            source = await db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
                .execution_options(populate_existing=True))
            if source is None:
                raise ValueError("context_source_agent_unavailable")
            await check_agent_access(db, user, agent_id)
    except (HTTPException, ValueError) as exc:
        raise ContentInputReceiptError("content_source_access_denied", "The context source is no longer authorized.", 403) from exc
    if policy_source:
        from app.models.config_revision import ConfigRevision
        from app.services.config_versioning import canonical_content_hash
        from app.services.owner_action_policy import OWNER_ACTION_POLICY_ENTITY_TYPE, _policy_from_revision, render_owner_action_policy_prompt_section

        revision = await db.get(ConfigRevision, policy_id, populate_existing=True)
        if (revision is None or revision.tenant_id != tenant_id or revision.entity_id != agent_id
            or revision.entity_type != OWNER_ACTION_POLICY_ENTITY_TYPE or revision.changed_by_agent_id is not None
            or canonical_content_hash(revision.content) != revision.content_hash):
            raise ContentInputReceiptError("content_source_changed", "The exact Owner policy source is unavailable or invalid.", 403)
        policy = _policy_from_revision(agent_id=agent_id, tenant_id=tenant_id, revision=revision)
        rendered = render_owner_action_policy_prompt_section(policy)
        if not policy.valid or hashlib.sha256(rendered.encode("utf-8")).hexdigest() != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "The observed Owner policy no longer matches its receipt.", 403)
        return
    if not resident and not identity and ref == "a2a-collaborators":
        from app.runtime.prompt_sections.a2a_collaborators import build_a2a_collaborators_section

        empty = build_a2a_collaborators_section(
            {"same_owner_agents": [], "public_agents": [], "collaboration_groups": []}, max_chars=None)
        if hashlib.sha256(empty.encode("utf-8")).hexdigest() == dependency["source_version"]:
            return  # Constant empty-directory guidance has no roster data.
        from app.models.config_revision import ConfigRevision
        from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model
        from app.services.agent_context import agent_identity_content
        from app.services.config_versioning import canonical_content_hash

        directory = await build_a2a_collaboration_read_model(db, agent_id)
        if directory.get("public_agents") or directory.get("collaboration_groups"):
            raise ContentInputReceiptError("content_source_unavailable",
                "Shared collaborator metadata requires its original publication and audience receipts.", 503)
        for entry in directory.get("same_owner_agents") or []:
            peer = await db.get(Agent, uuid.UUID(entry["id"]), populate_existing=True)
            if peer is None or peer.tenant_id != tenant_id:
                raise ContentInputReceiptError("content_source_changed", "The collaborator is unavailable.", 403)
            witness = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
                ConfigRevision.entity_type == "agent_bootstrap", ConfigRevision.entity_id == peer.id,
                ConfigRevision.version == 1, ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))
            content = witness.content if witness is not None and isinstance(witness.content, dict) else {}
            if (witness is None or witness.change_source != "user" or witness.changed_by_agent_id is not None
                or witness.changed_by_user_id != peer.creator_id or canonical_content_hash(content) != witness.content_hash
                or content.get("schema") != "hive.agent_bootstrap.v1"
                or content.get("identity") != agent_identity_content(peer.name, peer.role_description)):
                from app.services.employee_creation_sources import load_employee_creation_context

                content = await load_employee_creation_context(db, tenant_id=tenant_id, agent_id=peer.id,
                    user=user, runtime_request=runtime_request)
                if content.get("identity") != agent_identity_content(peer.name, peer.role_description):
                    raise ContentInputReceiptError("content_source_unavailable",
                        "A collaborator label lacks an exact original creation witness.", 503)
        rendered = build_a2a_collaborators_section(directory, max_chars=None)
        if hashlib.sha256(rendered.encode("utf-8")).hexdigest() != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "The collaborator directory has changed.", 403)
        return
    if not resident and not identity:
        resource = await load_agent_context_resource(agent_id=agent_id, tenant_id=tenant_id, resource_ref=ref)
        if resource.status != "ok":
            raise ContentInputReceiptError("content_source_unavailable", "The current context source could not be read. Retry later.", 503)
        if resource.sha256 != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "The context source has changed. Regenerate from current authorized inputs.", 403)
    if resident or identity or ref == "soul":
        from app.models.config_revision import ConfigRevision
        from app.services.config_versioning import canonical_content_hash

        revision = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.entity_type == "agent_bootstrap", ConfigRevision.entity_id == agent_id,
            ConfigRevision.version == 1, ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))
        source = await db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
            .execution_options(populate_existing=True))
        content = revision.content if revision is not None and isinstance(revision.content, dict) else {}
        if (revision is None or source is None or content.get("schema") != "hive.agent_bootstrap.v1"
            or canonical_content_hash(content) != revision.content_hash or revision.change_source != "user"
            or revision.changed_by_agent_id is not None or revision.changed_by_user_id != source.creator_id):
            from app.services.employee_creation_sources import load_employee_creation_context

            content = await load_employee_creation_context(db, tenant_id=tenant_id, agent_id=agent_id,
                user=user, runtime_request=runtime_request)
        from app.config import get_settings

        if identity:
            from app.services.agent_context import agent_identity_content

            current = agent_identity_content(source.name, source.role_description)
            if canonical_content_hash(current) != dependency["source_version"]:
                raise ContentInputReceiptError("content_source_changed", "The employee identity has changed since observation.", 403)
            if content.get("identity") != current:
                raise ContentInputReceiptError("content_source_unavailable", "The identity labels lack an exact original witness.", 503)
            return
        if resident:
            sources = content.get("resident_sources", {})
            version = sources.get(ref) if isinstance(sources, dict) else None
            if version != dependency["source_version"]:
                raise ContentInputReceiptError("content_source_unavailable", "The resident source lacks an exact bootstrap witness.", 503)
            _validate_source_bytes(Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / ref, version)
            return
        soul = content.get("soul", {})
        if (not isinstance(soul, dict) or soul.get("resource_sha256") != resource.sha256
            or not soul.get("content_sha256")):
            raise ContentInputReceiptError("content_source_unavailable", "The identity source lacks an exact bootstrap witness.", 503)
        _validate_source_bytes(Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "soul.md", soul["content_sha256"])


async def record_session_actor_identity(db, *, tenant_id, agent_id, session_id, user_id, display_name, timezone_name=None,
    runtime_task_id=None):
    """Witness SQL actor and work/input channel facts used by the runtime header."""
    from datetime import datetime, timezone
    from sqlalchemy.dialects.postgresql import insert
    from app.models.chat_session import ChatSession
    from app.models.agent import Agent
    from app.models.invocation_span import InvocationSpan
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.session_model_inputs import _hash

    session_id = uuid.UUID(str(session_id))
    row = (await db.execute(select(User, ChatSession).join(ChatSession, or_(ChatSession.user_id == User.id, ChatSession.audience_kind == "group"))
        .join(Agent, Agent.id == ChatSession.agent_id).where(
        User.id == user_id, User.tenant_id == tenant_id, User.is_active.is_(True),
        ChatSession.id == session_id, ChatSession.agent_id == agent_id, ChatSession.tenant_id == tenant_id,
        Agent.tenant_id == tenant_id, Agent.deleted_at.is_(None),
    ).execution_options(populate_existing=True))).first()
    if row is None or (display_name is not None and display_name != (row[0].display_name or row[0].username)):
        raise ContentInputReceiptError("context_actor_identity_unavailable", "The observed actor does not match the current session.", 403)
    actor, session = row
    channel = str(session.source_channel or "")
    if session.audience_kind == "group":
        from app.models.runtime_task import RuntimeTask
        from app.models.session_v2 import SessionCommand
        from app.services.shared_group_authority import load_shared_group_runtime_actor, shared_group_command_channel

        task = await db.get(RuntimeTask, uuid.UUID(str(runtime_task_id))) if runtime_task_id is not None else None
        if task is None or (await load_shared_group_runtime_actor(db, task=task, session=session)).id != actor.id:
            raise ContentInputReceiptError("context_actor_identity_unavailable", "The shared actor lacks its canonical turn.", 403)
        command = await db.get(SessionCommand, uuid.UUID((task.metadata_json or {})["session_v2_command_id"]))
        channel = shared_group_command_channel(command, session=session)
    if timezone_name is not None:
        zones = (await db.execute(select(Agent.timezone, Tenant.timezone).join(Tenant, Tenant.id == Agent.tenant_id)
            .where(Agent.id == agent_id, Agent.tenant_id == tenant_id))).one()
        if timezone_name != (zones[0] or zones[1] or "UTC"):
            raise ContentInputReceiptError("context_timezone_changed", "The observed timezone changed before capture.", 409)
    payload = {"schema": "hive.session_actor_identity.v1", "tenant_id": str(tenant_id), "agent_id": str(agent_id),
        "session_id": str(session.id), "user_id": str(actor.id), "display_name": display_name,
        "channel": channel, "timezone": timezone_name}
    version = _hash(payload)
    source_id = uuid.uuid5(session.id, f"session-actor:{version}")
    await db.execute(insert(InvocationSpan).values(id=source_id, tenant_id=tenant_id, agent_id=agent_id,
        user_id=actor.id, session_id=str(session.id), runtime_task_id=session.runtime_task_id,
        trace_id=f"session-context:{session.id}", span_id=f"actor-source:{source_id}",
        span_type="session_actor_identity", name="session.actor.identity", status="captured", duration_ms=0,
        started_at=datetime.now(timezone.utc), metadata_json={"session_actor_identity": payload}).on_conflict_do_nothing(index_elements=["id"]))
    captured = await db.get(InvocationSpan, source_id, populate_existing=True)
    if (captured is None or captured.tenant_id != tenant_id or captured.agent_id != agent_id or captured.user_id != actor.id
        or captured.session_id != str(session.id) or captured.span_type != "session_actor_identity"
        or captured.name != "session.actor.identity" or captured.status != "captured"
        or captured.metadata_json != {"session_actor_identity": payload}):
        raise ContentInputReceiptError("context_actor_identity_unavailable", "The retained actor observation cannot be rebound.", 503)
    return {"source_kind": "session_actor_identity", "source_id": str(source_id), "source_version": version}, payload


async def _validate_session_actor_identity(db, tenant_id, dependency, *, user, runtime_request):
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.services.session_model_inputs import _hash

    try:
        source_id = uuid.UUID(dependency["source_id"])
        row = await db.get(InvocationSpan, source_id, populate_existing=True)
        payload = (row.metadata_json or {}).get("session_actor_identity") if row is not None else None
        if (not isinstance(payload, dict) or set(payload) != {"schema", "tenant_id", "agent_id", "session_id", "user_id", "display_name", "channel", "timezone"}
            or payload["schema"] != "hive.session_actor_identity.v1"
            or (payload["display_name"] is not None and not isinstance(payload["display_name"], str))
            or not isinstance(payload["channel"], str)):
            raise ValueError("actor_source_shape_invalid")
        if payload["timezone"] is not None:
            from zoneinfo import ZoneInfo

            ZoneInfo(payload["timezone"])
        scope = {key: uuid.UUID(payload[key]) for key in ("tenant_id", "agent_id", "session_id", "user_id")}
        if (scope["tenant_id"] != tenant_id or row.tenant_id != tenant_id or row.agent_id != scope["agent_id"]
            or row.user_id != scope["user_id"] or row.session_id != str(scope["session_id"])
            or row.span_type != "session_actor_identity" or row.name != "session.actor.identity" or row.status != "captured"
            or _hash(payload) != dependency["source_version"]
            or source_id != uuid.uuid5(scope["session_id"], f"session-actor:{_hash(payload)}")):
            raise ValueError("actor_source_binding_invalid")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The actor observation is invalid or unavailable.", 503) from exc
    session = await db.scalar(select(ChatSession).where(ChatSession.id == scope["session_id"],
        ChatSession.agent_id == scope["agent_id"], ChatSession.tenant_id == tenant_id).execution_options(populate_existing=True))
    if session is None:
        raise ContentInputReceiptError("content_source_changed", "The actor's source session is unavailable.", 403)
    await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request)


async def _validate_session_dependency(db, tenant_id, dependency, *, user, runtime_request, active_workspace_receipts=None):
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand, SessionToolInvocation
    from app.services.session_model_inputs import _hash
    from app.services.session_semantic_history import _event_payload
    from app.services.session_v2_persistence import SESSION_COMMAND_AUTHORITY_STAMP_KEY

    try:
        source_id = uuid.UUID(dependency["source_id"])
    except ValueError as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The session source receipt is invalid.", 503) from exc
    model = SessionCommand if dependency["source_kind"] == "session_command" else SessionToolInvocation
    row = (await db.execute(select(model, ChatSession).join(ChatSession, ChatSession.id == model.session_id).where(
        model.id == source_id, model.tenant_id == tenant_id, ChatSession.tenant_id == tenant_id,
    ).execution_options(populate_existing=True))).first()
    if row is None:
        raise ContentInputReceiptError("content_source_changed", "The exact session source is unavailable.", 403)
    source, session = row
    if session.audience_kind == "group" and model is SessionCommand and source.namespace != "human_input":
        raise ContentInputReceiptError("content_source_unavailable",
            "Derived group output requires its live source-disclosure reader.", 503)
    if model is SessionCommand:
        version = source.request_hash
        valid = version == _hash({"command_kind": source.command_kind, "request": source.request_json}) and source.target_hash == _hash(source.target_json)
    else:
        event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.id == source.result_event_id, ChatTranscriptEvent.tenant_id == tenant_id,
            ChatTranscriptEvent.agent_id == session.agent_id, ChatTranscriptEvent.session_id == session.id,
            ChatTranscriptEvent.run_id == source.run_id, ChatTranscriptEvent.invocation_id == source.id,
        ).execution_options(populate_existing=True))
        payload = _event_payload(event) if event is not None else {}
        content, version = payload.get("content"), payload.get("content_hash")
        valid = isinstance(content, str) and version == _hash(content)
    if not valid or version != dependency["source_version"]:
        raise ContentInputReceiptError("content_source_changed", "The session source no longer matches its receipt.", 403)
    from app.tools.runtime import ToolExecutionContext

    context = getattr(runtime_request, "context", None)
    await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request,
        delegated_user_command=(model is SessionCommand and source.namespace == "human_input"
            and source.principal_type == "user" and isinstance(context, ToolExecutionContext)
            and source.principal_id == context.user_id
            and SESSION_COMMAND_AUTHORITY_STAMP_KEY not in (source.target_json or {})))
    if model is SessionToolInvocation:
        from app.services.feishu_read_receipts import FEISHU_RESULT_TOOLS, load_feishu_result

        if source.tool_name in FEISHU_RESULT_TOOLS and source.effect_state in {"effect_committed", "failed"}:
            proof = await load_feishu_result(db, tenant_id=tenant_id, agent_id=session.agent_id,
                invocation=source, content_hash=version, current=True)
            reader = (await _runtime_input_actor(db, tenant_id, runtime_request))[1] if runtime_request is not None else user
            if proof is None or reader is None or proof["reader_id"] != str(reader.id):
                raise ContentInputReceiptError("content_source_access_denied",
                    "The original Feishu result or its connection is no longer authorized for this reader.", 403)
    if session.audience_kind == "group" and model is SessionToolInvocation:
        from app.services.session_model_inputs import _origin_dependencies

        key = (dependency["source_id"], dependency["source_version"])
        active = _ACTIVE_GROUP_TOOL_SOURCES.get()
        if key in active:
            raise ContentInputReceiptError("content_source_unavailable", "The group tool source has a cyclic dependency.", 503)
        token = _ACTIVE_GROUP_TOOL_SOURCES.set(active | {key})
        try:
            sources, complete = await _origin_dependencies(db, tenant_id=tenant_id, agent_id=session.agent_id,
                ref=f"session-tool-result:{source.id}")
            if not complete:
                raise ContentInputReceiptError("content_source_unavailable", "The group tool source lacks its complete receipt.", 503)
            await _validate_input_dependencies(db, tenant_id, [item for item in sources if item != dependency], user=user,
                runtime_request=runtime_request, active_workspace_receipts=active_workspace_receipts)
        finally:
            _ACTIVE_GROUP_TOOL_SOURCES.reset(token)


async def _authorize_session_input_source(db, tenant_id, session, *, user, runtime_request, delegated_user_command=False):
    from fastapi import HTTPException
    from app.core.permissions import authorize_session_action

    if session.audience_kind == "group":
        from app.core.permissions import check_agent_access, is_scoped_business_admin
        from app.services.shared_group_authority import require_current_group_member

        try:
            requester = (await _runtime_input_actor(db, tenant_id, runtime_request))[1] if runtime_request is not None else user
            if is_scoped_business_admin(requester, resource_tenant_id=session.tenant_id):
                await authorize_session_action(db, requester, agent_id=session.agent_id, session_id=session.id,
                    action="read_content_input_source")
                return
            await check_agent_access(db, requester, session.agent_id)
            await require_current_group_member(db, session=session, user=requester)
            return
        except HTTPException as exc:
            raise ContentInputReceiptError("group_membership_unavailable" if exc.status_code == 503 else "content_source_access_denied",
                "The group source requires current membership and Agent access.", exc.status_code) from exc
        except ContentInputReceiptError:
            raise
        except ValueError as exc:
            raise ContentInputReceiptError("content_source_access_denied", "The group source reader is not authorized.", 403) from exc
    if session.audience_kind not in {None, "private"}:
        raise ContentInputReceiptError("content_source_unavailable", "This session source requires its live shared-audience reader.", 503)
    try:
        if runtime_request is None:
            await authorize_session_action(db, user, agent_id=session.agent_id, session_id=session.id,
                action="read_content_input_source")
            return
        principal, requester = await _runtime_input_actor(db, tenant_id, runtime_request)
        if session.user_id != requester.id:
            raise ValueError("session_source_actor_mismatch")
        if session.agent_id != principal.agent_id:
            from app.services.project_context import project_source_read_allowed

            if project_source_read_allowed(tenant_id, principal):
                from app.core.permissions import check_agent_access

                await check_agent_access(db, requester, session.agent_id)
                return
            if not delegated_user_command:
                raise ValueError("session_source_actor_mismatch")
            from datetime import datetime, timezone
            from sqlalchemy import func
            from app.agents.delegation_token import DelegationToken, validate_delegation_token
            from app.models.agent import Agent
            from app.models.runtime_task import RuntimeTask

            context = runtime_request.context
            token = context.delegation_token
            if (not delegated_user_command or principal.purpose != "a2a_delegation"
                or not isinstance(token, DelegationToken)
                or not validate_delegation_token(token, child_agent_id=context.agent_id).valid
                or token.parent_agent_id != session.agent_id):
                raise ValueError("session_source_actor_mismatch")
            parent = await db.scalar(select(Agent).join(RuntimeTask, RuntimeTask.parent_agent_id == Agent.id).where(
                RuntimeTask.id == uuid.UUID(str(context.runtime_task_id)), RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.task_type == "delegation", RuntimeTask.status == "running",
                RuntimeTask.root_user_id == requester.id, RuntimeTask.child_agent_id == context.agent_id,
                func.replace(func.lower(RuntimeTask.child_session_id), "-", "") == uuid.UUID(str(context.session_id)).hex,
                RuntimeTask.claim_expires_at > datetime.now(timezone.utc), RuntimeTask.parent_agent_id == session.agent_id,
                func.replace(func.lower(RuntimeTask.parent_session_id), "-", "") == session.id.hex,
                Agent.tenant_id == tenant_id, Agent.deleted_at.is_(None), Agent.deactivated_at.is_(None)))
            if parent is None or (parent.owner_user_id or parent.creator_id) != requester.id:
                raise ValueError("session_source_actor_mismatch")
    except (HTTPException, ValueError) as exc:
        raise ContentInputReceiptError("content_source_access_denied", "The session source is no longer authorized for this reader.", 403) from exc


async def _runtime_input_actor(db, tenant_id, request):
    from fastapi import HTTPException
    from app.core.permissions import check_agent_access
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.tools.handlers.knowledge import _personal_kb_runtime_principal

    try:
        principal = request if isinstance(request, AgentRuntimePrincipal) else _personal_kb_runtime_principal(request)
        requester = await db.scalar(select(User).where(User.id == principal.requester_user_id,
            User.tenant_id == tenant_id, User.is_active.is_(True)).execution_options(populate_existing=True))
        if requester is None:
            raise ValueError("runtime_requester_unavailable")
        current_session = await db.scalar(select(ChatSession).where(
            ChatSession.id == uuid.UUID(str(principal.session_id)), ChatSession.tenant_id == tenant_id,
            ChatSession.agent_id == principal.agent_id))
        if current_session is None:
            raise ValueError("runtime_session_actor_mismatch")
        if current_session.audience_kind == "group":
            from app.models.runtime_task import RuntimeTask
            from app.services.shared_group_authority import load_shared_group_runtime_actor

            task = await db.get(RuntimeTask, uuid.UUID(str(principal.runtime_task_id)))
            actor = await load_shared_group_runtime_actor(db, task=task, session=current_session) if task is not None else None
            if actor is None or actor.id != requester.id:
                raise ValueError("runtime_session_actor_mismatch")
        elif current_session.user_id != requester.id:
            raise ValueError("runtime_session_actor_mismatch")
        # Refresh the current employee owner before the shared Agent access check.
        agent = await db.scalar(select(Agent).where(Agent.id == principal.agent_id, Agent.tenant_id == tenant_id)
            .execution_options(populate_existing=True))
        if agent is None:
            raise ValueError("runtime_agent_tenant_mismatch")
        await check_agent_access(db, requester, principal.agent_id)
        return principal, requester
    except (HTTPException, ValueError, PermissionError) as exc:
        raise ContentInputReceiptError("content_source_access_denied", "The runtime source reader is no longer authorized.", 403) from exc


def _validate_source_bytes(path, version: str) -> None:
    from app.config import get_settings
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file

    try:
        root = Path(get_settings().AGENT_DATA_DIR).resolve()
        path = Path(path)
        if path.is_absolute():
            path = path.relative_to(root)
        with open_workspace_file(root, path.as_posix()) as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    except (OSError, ValueError, TypeError, WorkspaceAuthorityError) as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The canonical source bytes are unavailable.", 503) from exc
    if digest != version:
        raise ContentInputReceiptError("content_source_changed", "The canonical source bytes no longer match the receipt.", 403)


async def _validate_company_publication_dependency(db, tenant_id, dependency, *, user, runtime_request):
    from app.models.company_knowledge import CompanyKnowledgePublication
    from app.models.knowledge import KnowledgeDocument
    from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest
    from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal

    try:
        publication_id = uuid.UUID(dependency["source_id"])
    except ValueError as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The publication receipt is invalid.", 503) from exc
    publication = await db.scalar(select(CompanyKnowledgePublication).where(
        CompanyKnowledgePublication.id == publication_id, CompanyKnowledgePublication.tenant_id == tenant_id,
    ).execution_options(populate_existing=True))
    if publication is None or publication.content_hash != dependency["source_version"]:
        raise ContentInputReceiptError("content_source_changed", "The exact publication is unavailable or has changed.", 403)
    if runtime_request is not None:
        from app.tools.handlers.knowledge import company_kb_principal_for_runtime

        try:
            runtime, _requester = await _runtime_input_actor(db, tenant_id, runtime_request)
            principal = await company_kb_principal_for_runtime(db, runtime, tenant_id)
        except ValueError as exc:
            raise ContentInputReceiptError("content_source_access_denied", "The runtime source authority is invalid.", 403) from exc
    else:
        principal = CompanyKnowledgePrincipal(tenant_id=tenant_id, accountable_user_id=user.id,
            accountable_role=user.role, actor_type="user", actor_id=user.id,
            department_id=getattr(user, "department_id", None), purpose="interactive_session")
    result = await CompanyKnowledgeGateway().read(db, principal=principal,
        request=CompanyKnowledgeReadRequest(document_id=None, publication_id=publication_id, segment_ids=(),
            max_chars=None, trace_id=f"content-input:{uuid.uuid4().hex}"))
    if result.status != "ok" or result.publication_id != publication_id or result.source_version != dependency["source_version"]:
        raise ContentInputReceiptError("content_source_access_denied", "The required publication is no longer authorized for this reader.", 403)
    document = await db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == result.document_id,
        KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.scope_type == "company", KnowledgeDocument.scope_id == tenant_id,
    ).execution_options(populate_existing=True))
    if document is None or document.canonical_md_sha256 != dependency["source_version"]:
        raise ContentInputReceiptError("content_source_changed", "The publication's canonical source version has changed.", 403)
    _validate_source_bytes(document.canonical_md_path, dependency["source_version"])


async def _validate_workspace_dependency(db, tenant_id, dependency, *, user, runtime_request, active_receipts):
    from fastapi import HTTPException
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, authorize_workspace_path

    try:
        receipt_id = uuid.UUID(dependency["source_id"])
    except ValueError as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The workspace source receipt is invalid.", 503) from exc
    receipt = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.id == receipt_id,
        ContentInputReceipt.tenant_id == tenant_id, ContentInputReceipt.carrier_kind == "workspace_artifact",
    ).execution_options(populate_existing=True))
    if receipt is None or receipt.content_sha256 != dependency["source_version"]:
        raise ContentInputReceiptError("content_source_changed", "The exact workspace source version is unavailable.", 403)
    if receipt_id in active_receipts:
        raise ContentInputReceiptError("content_source_unavailable", "The workspace source dependency chain is cyclic.", 503)
    try:
        raw_agent, path = receipt.carrier_ref.split("\n", 1)
        agent_id = uuid.UUID(raw_agent)
        if workspace_artifact_ref(agent_id, path) != receipt.carrier_ref or not path.startswith("workspace/"):
            raise ValueError("workspace_source_binding_invalid")
    except (ValueError, HTTPException) as exc:
        raise ContentInputReceiptError("content_source_unavailable", "The workspace source binding is invalid.", 503) from exc
    manifest = await db.scalar(select(WorkspaceResourceManifest).where(
        WorkspaceResourceManifest.tenant_id == tenant_id, WorkspaceResourceManifest.agent_id == agent_id,
        WorkspaceResourceManifest.path == path).execution_options(populate_existing=True))
    if manifest is None or manifest.authority_state != "owned" or manifest.deleted_at is not None:
        raise ContentInputReceiptError("content_source_access_denied", "The workspace source is unavailable or quarantined.", 403)
    if manifest.audience_kind not in {None, "private"}:
        raise ContentInputReceiptError("content_source_unavailable", "This workspace source requires its live shared-audience reader.", 503)
    principal = None
    if runtime_request is not None:
        principal, user = await _runtime_input_actor(db, tenant_id, runtime_request)
        from app.services.project_context import project_source_read_allowed

        if principal.agent_id != agent_id and not project_source_read_allowed(tenant_id, principal):
            raise ContentInputReceiptError("content_source_access_denied", "The workspace source belongs to another employee.", 403)
    try:
        await authorize_workspace_path(db, user, agent_id=agent_id, path=path, action="read", path_exists=True,
            session_id=uuid.UUID(principal.session_id) if principal is not None and principal.agent_id == agent_id else None,
            runtime_principal=principal)
    except (HTTPException, WorkspaceAuthorityError) as exc:
        raise ContentInputReceiptError("content_source_access_denied", "The workspace source is no longer authorized for this reader.", 403) from exc
    latest = await latest_workspace_receipt(db, tenant_id, agent_id, path)
    if latest is None or latest.revision < receipt.revision or manifest.content_hash != latest.content_sha256:
        raise ContentInputReceiptError("content_source_changed", "The current workspace bytes have no matching receipt.", 403)
    _validate_source_bytes(Path(str(agent_id)) / path, latest.content_sha256)
    # A later write is not a source of the previously observed version.
    await _validate_workspace_receipt_inputs(db, receipt, user=user, runtime_request=runtime_request,
        active_receipts=active_receipts | {receipt_id})


async def _validate_workspace_receipt_inputs(db, receipt, *, user=None, runtime_request=None, active_receipts=frozenset()):
    if runtime_request is not None:
        from app.services.runtime_result_sources import validate_published_workspace_inputs

        if await validate_published_workspace_inputs(db, runtime_request, receipt, active_receipts=active_receipts):
            return
    await _validate_input_dependencies(db, receipt.tenant_id, receipt.dependencies_json, user=user,
        runtime_request=runtime_request, active_workspace_receipts=active_receipts)


async def authorize_workspace_execution_inputs(request) -> list[dict]:
    """Exclude unavailable receipted inputs, preserving other process capabilities."""
    from dataclasses import replace
    from sqlalchemy.exc import SQLAlchemyError
    from app.database import tenant_scoped_session
    from app.services.session_tool_runtime import _sha256
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope, WorkspaceInputRead

    context = request.context
    scope = context.workspace_authority_scope
    if request.tool_name not in {"execute_code", "run_command", "run_skill_tool", "office_document_create", "office_document_apply"} or not isinstance(scope, WorkspaceAuthorityScope):
        return []
    paths = {path for path in scope.allowed_paths if path.startswith("workspace/")}
    reads, denied = [], []
    try:
        if request.tool_name in {"office_document_create", "office_document_apply"}:
            from app.core.resource_authority import normalize_workspace_resource_path

            raw = request.arguments.get("template_path") if request.tool_name == "office_document_create" else request.arguments.get("path")
            resources = (request.arguments.get('resource_paths') or []) if request.tool_name == 'office_document_apply' else []
            if not isinstance(resources, list) or any(not isinstance(path, str) for path in resources):
                raise ValueError('invalid Office resource paths')
            paths &= {normalize_workspace_resource_path(path) for path in [*([str(raw)] if raw else []), *resources]}
        tenant_id = uuid.UUID(str(context.tenant_id))
        if scope.agent_id != context.agent_id or scope.user_id != context.user_id or scope.operator_view:
            raise ContentInputReceiptError("content_source_access_denied", "The execution input scope does not match.", 403)
        async with tenant_scoped_session(tenant_id) as db:
            await _runtime_input_actor(db, tenant_id, request)
            for path in sorted(paths):
                receipt = await latest_workspace_receipt(db, tenant_id, context.agent_id, path)
                if receipt is None:
                    continue  # Do not invent receipts for unmigrated legacy carriers.
                try:
                    await _validate_workspace_receipt_inputs(db, receipt, runtime_request=request,
                        active_receipts=frozenset({receipt.id}))
                except ContentInputReceiptError as exc:
                    denied.append({"path": path, "reason_code": exc.code,
                        "status": "denied" if exc.status_code == 403 else "unavailable", "retryable": exc.status_code == 503})
                    continue
                reads.append(WorkspaceInputRead(path=path, receipt_id=receipt.id, revision=receipt.revision,
                    content_sha256=receipt.content_sha256, dependencies_hash=_sha256(receipt.dependencies_json)))
    except (ContentInputReceiptError, OSError, ValueError, SQLAlchemyError) as exc:
        code = exc.code if isinstance(exc, ContentInputReceiptError) else "content_source_unavailable"
        status = exc.status_code if isinstance(exc, ContentInputReceiptError) else 503
        reads = []
        denied = [{"path": path, "reason_code": code, "status": "denied" if status == 403 else "unavailable",
            "retryable": status == 503} for path in sorted(paths)]
    context.workspace_authority_scope = replace(scope,
        allowed_paths=scope.allowed_paths - {row["path"] for row in denied}, input_reads=tuple(reads))
    return denied


async def authorize_workspace_receipt_read(request):
    """Recheck known receipt dependencies under the actual native Agent principal."""
    from dataclasses import replace
    from sqlalchemy.exc import SQLAlchemyError
    from app.core.resource_authority import normalize_workspace_resource_path
    from app.database import tenant_scoped_session
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope, WorkspaceInputRead
    from app.services.session_tool_runtime import _sha256
    from app.tools.result_envelope import render_tool_error

    if request.tool_name not in {"read_file", "read_document"} and not (
        request.tool_name == "fs_read" and str(request.arguments.get("mode") or "text").strip().lower() in {"text", "document"}
    ):
        return None
    context = request.context
    try:
        path = normalize_workspace_resource_path(str(request.arguments.get("path") or ""))
        if not path.startswith("workspace/"):
            return None
        scope = context.workspace_authority_scope
        if scope is not None and not scope.can_read(path):
            return None  # The existing path boundary denies before body access.
        tenant_id = uuid.UUID(str(context.tenant_id))
        async with tenant_scoped_session(tenant_id) as db:
            receipt = await latest_workspace_receipt(db, tenant_id, context.agent_id, path)
            if receipt is None:
                return None  # Legacy carrier migration remains separate; no receipt is invented.
            if not isinstance(scope, WorkspaceAuthorityScope) or scope.agent_id != context.agent_id or scope.user_id != context.user_id:
                raise ContentInputReceiptError("content_source_access_denied", "The runtime file scope does not match.", 403)
            await _validate_workspace_receipt_inputs(db, receipt, runtime_request=request,
                active_receipts=frozenset({receipt.id}))
            # This hash is checked against the very bytes returned by the file reader.
            scope = context.workspace_authority_scope
            observed = WorkspaceInputRead(path=path, receipt_id=receipt.id, revision=receipt.revision,
                content_sha256=receipt.content_sha256, dependencies_hash=_sha256(receipt.dependencies_json))
            context.workspace_authority_scope = replace(scope,
                input_reads=tuple(read for read in scope.input_reads if read.path != path) + (observed,))
        return observed
    except (ContentInputReceiptError, OSError, ValueError, SQLAlchemyError) as exc:
        status = exc.status_code if isinstance(exc, ContentInputReceiptError) else 503
        code = exc.code if isinstance(exc, ContentInputReceiptError) else "content_source_unavailable"
        return render_tool_error(tool_name=request.tool_name,
            error_class="auth_or_permission" if status == 403 else "provider_unavailable",
            message="This file's source receipt cannot authorize model consumption.", provider="content_input_receipt",
            http_status=status, retryable=status == 503,
            actionable_hint="Restore the scoped source grant or regenerate from current authorized inputs.",
            extra={"reason_code": code, "outcome": "denied" if status == 403 else "unavailable"})


async def validate_team_memory_receipt(db, tenant_id: uuid.UUID, user, workspace_key: str, entry, receipts: dict):
    receipt = receipts.get(team_memory_ref(workspace_key, Path(entry.path).stem))
    if receipt is None or receipt.revision != entry.revision or receipt.content_sha256 != entry.checksum:
        raise ContentInputReceiptError("team_memory_unverified", "This entry has no matching current content receipt. Preserve it and regenerate a new entry from authorized inputs.")
    await validate_browser_input_dependencies(db, tenant_id, user, receipt.dependencies_json)
    return receipt


def record_team_memory_receipt(db, tenant_id: uuid.UUID, workspace_key: str, entry, dependencies: list) -> None:
    unique = {(row["source_kind"], row["source_id"], row["source_version"]) for row in dependencies}
    db.add(ContentInputReceipt(tenant_id=tenant_id, carrier_kind="team_memory",
        carrier_ref=team_memory_ref(workspace_key, Path(entry.path).stem), revision=entry.revision,
        content_sha256=entry.checksum, dependencies_json=[dict(zip(("source_kind", "source_id", "source_version"), row)) for row in sorted(unique)]))


def workspace_artifact_ref(agent_id: uuid.UUID, path: str) -> str:
    from app.core.resource_authority import normalize_workspace_resource_path

    normalized = normalize_workspace_resource_path(path)
    if not normalized.startswith("workspace/"):
        raise ContentInputReceiptError("workspace_content_unverified", "Only workspace files carry content receipts.")
    return f"{agent_id}\n{normalized}"


async def lock_workspace_receipt_write(db, tenant_id: uuid.UUID, agent_id: uuid.UUID, path: str) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"workspace-content:{tenant_id}:{workspace_artifact_ref(agent_id, path)}"})


async def latest_workspace_receipt(db, tenant_id: uuid.UUID, agent_id: uuid.UUID, path: str):
    return await db.scalar(select(ContentInputReceipt).where(
        ContentInputReceipt.tenant_id == tenant_id, ContentInputReceipt.carrier_kind == "workspace_artifact",
        ContentInputReceipt.carrier_ref == workspace_artifact_ref(agent_id, path),
    ).order_by(ContentInputReceipt.revision.desc()).limit(1).execution_options(populate_existing=True))


async def validate_browser_workspace_receipt(db, tenant_id: uuid.UUID, user, receipt, content_sha256: str) -> None:
    if receipt is None or receipt.content_sha256 != content_sha256:
        raise ContentInputReceiptError("workspace_content_unverified",
            "The file has no matching verified content receipt. Preserve it and regenerate a new file from authorized inputs.")
    dependencies = receipt.dependencies_json
    if not isinstance(dependencies, list) or any(
        not isinstance(row, dict) or set(row) != {"source_kind", "source_id", "source_version"}
        or any(not isinstance(value, str) or not value for value in row.values()) for row in dependencies
    ):
        raise ContentInputReceiptError("workspace_content_unverified", "The file's input receipt is invalid.")
    await validate_browser_input_dependencies(db, tenant_id, user, dependencies)


async def validate_runtime_workspace_receipt(db, tenant_id, principal, receipt, content_sha256):
    if receipt is None or receipt.content_sha256 != content_sha256:
        raise ContentInputReceiptError("workspace_content_unverified", "The file has no matching verified content receipt.")
    await _runtime_input_actor(db, tenant_id, principal)
    await _validate_workspace_receipt_inputs(db, receipt, runtime_request=principal,
        active_receipts=frozenset({receipt.id}))


async def validate_artifact_content_receipt(db, tenant_id, user, artifact, *, runtime_principal=None):
    """Recheck the captured version's sources without borrowing a later version's audience."""
    snapshot = artifact.snapshot_json or {}
    try:
        receipt_id = uuid.UUID(snapshot["content_input_receipt_id"])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError("workspace_content_unverified",
            "The artifact has no pinned source receipt. Preserve it and reconcile its original inputs.") from exc
    receipt = await db.get(ContentInputReceipt, receipt_id, populate_existing=True)
    if (receipt is None or receipt.tenant_id != tenant_id or receipt.carrier_kind != "workspace_artifact"
        or receipt.carrier_ref != workspace_artifact_ref(artifact.agent_id, artifact.path)
        or receipt.content_sha256 != snapshot.get("content_hash")):
        raise ContentInputReceiptError("workspace_content_unverified", "The artifact's source receipt does not match its captured bytes.")
    if runtime_principal is not None:
        await _runtime_input_actor(db, tenant_id, runtime_principal)
    if snapshot.get("selection_model_result_id") is not None:
        from app.models.session_v2 import SessionModelResult
        from app.services.session_model_inputs import _model_dependencies

        try:
            model_id = uuid.UUID(snapshot["selection_model_result_id"])
        except (ValueError, TypeError) as exc:
            raise ContentInputReceiptError("artifact_selection_unavailable", "The artifact selection source is invalid.", 503) from exc
        model = await db.get(SessionModelResult, model_id, populate_existing=True)
        dependencies, complete = _model_dependencies(model)
        if not complete or (model.tenant_id, model.session_id, model.run_id) != (
            artifact.tenant_id, artifact.session_id, artifact.runtime_task_id):
            raise ContentInputReceiptError("artifact_selection_unavailable", "The exact artifact selection source is unavailable.", 503)
        await _validate_input_dependencies(db, tenant_id, dependencies, user=user, runtime_request=runtime_principal)
    await _validate_workspace_receipt_inputs(db, receipt, user=user,
        runtime_request=runtime_principal, active_receipts=frozenset({receipt.id}))


def record_workspace_receipt(db, tenant_id: uuid.UUID, agent_id: uuid.UUID, path: str, content_sha256: str, previous,
    *, additional_dependencies=()):
    # Authenticated human input adds no authority to the previous source audience.
    dependencies = [*(previous.dependencies_json if previous is not None else []), *additional_dependencies]
    unique = sorted({(row["source_kind"], row["source_id"], row["source_version"]) for row in dependencies})
    receipt = ContentInputReceipt(id=uuid.uuid4(), tenant_id=tenant_id, carrier_kind="workspace_artifact",
        carrier_ref=workspace_artifact_ref(agent_id, path), revision=previous.revision + 1 if previous else 1,
        content_sha256=content_sha256,
        dependencies_json=[dict(zip(("source_kind", "source_id", "source_version"), row)) for row in unique])
    db.add(receipt)
    return receipt


async def persist_workspace_execution_evidence(*, context, tool_name, tool_call_id, arguments, result, states, lineage, errors,
    input_read=None, descriptor_writes=None, skill_read=None):
    """Persist mechanical executor facts; this is not an input-authority receipt."""
    from datetime import datetime, timezone
    from app.database import tenant_scoped_session
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.session_tool_runtime import _sha256

    if not all((context.tenant_id, context.session_id, context.runtime_task_id)):
        return {"status": "unavailable", "reason_code": "workspace_execution_context_missing"}
    try:
        tenant_id, session_id, run_id = (uuid.UUID(str(value)) for value in (
            context.tenant_id, context.session_id, context.runtime_task_id))
        args_hash = _sha256({"tool_name": tool_name, "arguments": arguments})
        async with tenant_scoped_session(tenant_id) as db:
            session = await db.get(ChatSession, session_id)
            if session is None or session.agent_id != context.agent_id or session.tenant_id != tenant_id:
                raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The executor session does not match.")
            invocation = (await db.scalars(select(SessionToolInvocation).where(
                SessionToolInvocation.tenant_id == tenant_id, SessionToolInvocation.session_id == session_id,
                SessionToolInvocation.run_id == run_id, SessionToolInvocation.provider_tool_use_id == tool_call_id,
                SessionToolInvocation.tool_name == tool_name,
            ))).one_or_none()
            if invocation is None or (invocation.effective_args_hash or invocation.args_hash) != args_hash:
                raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The canonical invocation does not match.")
            if invocation.result_event_id is not None:
                from app.models.runtime_task import RuntimeTask
                from app.services.shared_group_authority import approval_execution_matches

                task = await db.get(RuntimeTask, run_id)
                if session.audience_kind != "group" or not await approval_execution_matches(
                    db, task=task, session=session, invocation_id=invocation.id,
                ):
                    raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The settled call has no claimed company execution.")
            model = await db.scalar(select(SessionModelResult).where(
                SessionModelResult.tenant_id == tenant_id, SessionModelResult.session_id == session_id,
                SessionModelResult.run_id == run_id, SessionModelResult.provider_request_id == invocation.provider_request_id,
            ))
            if model is None:
                raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The canonical model request is missing.")
            proof = {"schema": "hive.workspace_execution_evidence.v1", "invocation_id": str(invocation.id),
                "model_result_id": str(model.id), "model_request_hash": model.model_request_hash,
                "effective_args_hash": args_hash, "authority_snapshot_hash": invocation.authority_snapshot_hash,
                "tool_result_hash": _sha256(str(result)), "states": states, "lineage": lineage, "errors": errors}
            if descriptor_writes is not None:
                proof["descriptor_writes"] = descriptor_writes
            from app.services.workspace_resource_authority import WorkspaceExecutionInputs

            execution_inputs = getattr(context.workspace_authority_scope, "execution_inputs", None)
            if isinstance(execution_inputs, WorkspaceExecutionInputs):
                proof["execution_inputs"] = execution_inputs.snapshot()
            channel, metadata_key = "workspace-output", "workspace_execution_evidence"
            if input_read is not None:
                proof.pop("states")
                proof.pop("lineage")
                proof.pop("errors")
                proof.update(schema="hive.workspace_input_evidence.v1",
                    content_input_receipt_id=str(input_read.receipt_id),
                    carrier_ref=workspace_artifact_ref(context.agent_id, input_read.path),
                    revision=input_read.revision, content_sha256=input_read.content_sha256,
                    dependencies_hash=input_read.dependencies_hash, observed_sha256=input_read.observed_sha256)
                channel, metadata_key = "workspace-input", "workspace_input_evidence"
            evidence_id = uuid.uuid5(invocation.id, f"{channel}-evidence")
            if skill_read is not None:
                from app.services.skill_source_receipts import SkillInputCapture

                if not isinstance(skill_read, SkillInputCapture) or skill_read.agent_id != context.agent_id:
                    raise ContentInputReceiptError("skill_input_scope_mismatch", "The selected Skill read scope does not match.")
                proof = {key: value for key, value in proof.items() if key not in {"states", "lineage", "errors", "descriptor_writes"}}
                proof.update(schema="hive.skill_input_evidence.v1", **skill_read.snapshot())
                proof["source_hash"] = _sha256(proof)
                channel, metadata_key = "skill-input", "skill_input_evidence"
                evidence_id = uuid.uuid5(invocation.id, f"skill-input:{proof['source_hash']}")
            existing = await db.get(InvocationSpan, evidence_id)
            if existing is not None:
                if existing.metadata_json.get(metadata_key) != proof:
                    raise ContentInputReceiptError("workspace_execution_evidence_changed", "Executor evidence is immutable.")
            else:
                existing = InvocationSpan(id=evidence_id, tenant_id=tenant_id, agent_id=context.agent_id,
                    user_id=context.user_id, runtime_task_id=run_id, session_id=str(session_id),
                    trace_id=f"session-tool:{invocation.id}", span_id=f"{channel}:{invocation.id}",
                    span_type=channel.replace("-", "_"), name=tool_name, status="captured", duration_ms=0,
                    started_at=datetime.now(timezone.utc), input_hash=args_hash,
                    metadata_json={metadata_key: proof})
                db.add(existing)
            await db.flush()
            if channel == "workspace-output":
                status = await record_native_workspace_receipts(db, context=context, invocation=invocation,
                    model=model, arguments=arguments, content_hash=proof["tool_result_hash"])
                existing.metadata_json = {**existing.metadata_json, "native_content_receipts": status}
            await db.commit()
            return {"status": "captured", "receipt_ref": f"{channel}:{evidence_id}",
                "tracking_source": {"source_kind": "session_tool_invocation", "source_id": str(invocation.id),
                    "source_version": proof["tool_result_hash"]}}
    except ContentInputReceiptError as exc:
        return {"status": "unavailable", "reason_code": exc.code}
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("Workspace execution evidence unavailable (%s)", type(exc).__name__)
        # A failed evidence write cannot undo a file effect or authorize its replay.
        return {"status": "unavailable", "reason_code": "workspace_execution_evidence_unavailable"}


async def record_native_workspace_receipts(db, *, context, invocation, model, arguments, content_hash):
    """Bind executor-observed output to complete inputs under the actual runtime principal."""
    from app.config import get_settings
    from app.models.invocation_span import InvocationSpan
    from app.services.session_model_inputs import _model_dependencies, _stored_dependencies, _unique_dependencies
    from app.services.skill_source_receipts import SkillInputCapture
    from app.tools.runtime import ToolExecutionRequest

    dependencies, complete = _model_dependencies(model)
    if not complete:
        return {"status": "unavailable", "reason_code": "model_input_inventory_incomplete", "receipts": {}}
    if invocation.tool_name not in {"write_file", "fs_write", "run_skill_tool", "run_command", "execute_code", "office_document_create", "office_document_apply"}:
        return {"status": "unavailable", "reason_code": "runtime_output_producer_unavailable", "receipts": {}}
    request = ToolExecutionRequest(tool_name=invocation.tool_name, arguments=arguments, context=context)
    try:
        try:
            context_scope = tuple(uuid.UUID(str(value)) for value in (
                context.tenant_id, context.session_id, context.runtime_task_id))
        except (TypeError, ValueError) as exc:
            raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The output execution scope is invalid.") from exc
        if (context_scope != (invocation.tenant_id, invocation.session_id, invocation.run_id) or model.tenant_id != invocation.tenant_id
            or model.session_id != invocation.session_id or model.run_id != invocation.run_id
            or model.provider_request_id != invocation.provider_request_id):
            raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The output execution scope does not match.")
        await _runtime_input_actor(db, invocation.tenant_id, request)
        if Path(context.workspace).resolve() != (Path(get_settings().AGENT_DATA_DIR) / str(context.agent_id)).resolve():
            raise ContentInputReceiptError("workspace_execution_scope_mismatch", "The output workspace cannot be rebound.")
        if invocation.tool_name == "run_skill_tool":
            capture = getattr(context.workspace_authority_scope, "skill_inputs", None)
            if not isinstance(capture, SkillInputCapture) or capture.agent_id != context.agent_id:
                raise ContentInputReceiptError("skill_input_unverified", "The actual Skill input inventory is missing.")
            selected = capture.snapshot()
            if selected["unbound"] or not selected["sources"]:
                raise ContentInputReceiptError("skill_input_unverified", "The actual Skill input inventory is incomplete.")
            dependencies = _unique_dependencies([*dependencies, *selected["sources"]])
        retained = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "workspace-output-evidence"), populate_existing=True)
        _ref, outputs = await load_workspace_execution_evidence(db, agent_id=context.agent_id, invocation=invocation,
            model_result=model, arguments=arguments, content_hash=content_hash)
        proof = retained.metadata_json["workspace_execution_evidence"]
        if invocation.tool_name in {"run_command", "execute_code", "office_document_create", "office_document_apply"}:
            dependencies = _unique_dependencies([*dependencies, *_workspace_execution_input_sources(proof)])
    except ContentInputReceiptError as exc:
        return {"status": "denied" if exc.status_code == 403 else "unavailable", "reason_code": exc.code, "receipts": {}}
    if not outputs:
        return {"status": "unavailable", "reason_code": "workspace_output_not_observed", "receipts": {}}
    receipts, failures = {}, {}
    predecessors = {item["path"]: item["before_state"] for item in proof["lineage"]
        if isinstance(item, dict) and isinstance(item.get("before_state"), dict) and isinstance(item.get("path"), str)}
    for path, digest in outputs.items():
        try:
            async with db.begin_nested():
                await lock_workspace_receipt_write(db, invocation.tenant_id, context.agent_id, path)
                previous = await latest_workspace_receipt(db, invocation.tenant_id, context.agent_id, path)
                receipt_id = uuid.uuid5(invocation.id, f"workspace-content:{path}")
                existing = await db.get(ContentInputReceipt, receipt_id, populate_existing=True)
                before = predecessors.get(path, {})
                if existing is None and ((previous is None and before.get("exists") is not False)
                    or (previous is not None and before.get("exists") is True and before.get("sha256") != previous.content_sha256)):
                    raise ContentInputReceiptError("workspace_predecessor_unverified", "The previous bytes lack an exact source receipt.")
                inherited = previous.dependencies_json if previous is not None else []
                if _stored_dependencies(inherited) is None:
                    raise ContentInputReceiptError("workspace_content_unverified", "The inherited source inventory is invalid.")
                combined = _unique_dependencies([*dependencies, *inherited])
                if existing is None:
                    existing = ContentInputReceipt(id=receipt_id, tenant_id=invocation.tenant_id,
                        carrier_kind="workspace_artifact", carrier_ref=workspace_artifact_ref(context.agent_id, path),
                        revision=previous.revision + 1 if previous else 1, content_sha256=digest, dependencies_json=combined)
                    db.add(existing)
                elif (existing.tenant_id != invocation.tenant_id or existing.carrier_kind != "workspace_artifact"
                    or existing.carrier_ref != workspace_artifact_ref(context.agent_id, path) or existing.content_sha256 != digest
                    or _stored_dependencies(existing.dependencies_json) != combined):
                    raise ContentInputReceiptError("workspace_content_unverified", "The captured receipt cannot be rewritten.")
                await db.flush()
                # The tentative successor permits historical inputs from this same path;
                # failed authority checks roll it back without losing execution evidence.
                await validate_runtime_input_dependencies(db, request, [{"source_kind": "workspace_artifact",
                    "source_id": str(receipt_id), "source_version": digest}])
                import asyncio
                from app.services.workspace_receipt_versions import capture_receipt_version

                await asyncio.to_thread(capture_receipt_version, context.agent_id, path, existing)
            receipts[path] = str(receipt_id)
        except ContentInputReceiptError as exc:
            failures[path] = {"status": "denied" if exc.status_code == 403 else "unavailable", "reason_code": exc.code}
    return {"status": "captured" if receipts and not failures else "unavailable", "receipts": receipts, "failures": failures}


async def load_skill_input_evidence(db, *, agent_id, invocation, model_result, arguments, content_hash, capture):
    from app.models.invocation_span import InvocationSpan
    from app.services.session_tool_runtime import _sha256

    if capture is None:
        return None
    if not isinstance(capture, dict) or capture.get("status") != "captured":
        return {"status": "unavailable", "reason_code": "skill_input_capture_unavailable"}
    try:
        channel, raw_id = capture["receipt_ref"].split(":", 1)
        evidence_id = uuid.UUID(raw_id)
        if channel != "skill-input":
            raise ValueError("invalid_skill_input_channel")
    except (ValueError, KeyError, TypeError) as exc:
        raise ContentInputReceiptError("skill_input_unverified", "The Skill read capture is invalid.") from exc
    row = await db.get(InvocationSpan, evidence_id, populate_existing=True)
    proof = (row.metadata_json or {}).get("skill_input_evidence") if row is not None else None
    if (not isinstance(proof, dict) or row.tenant_id != invocation.tenant_id or row.agent_id != agent_id
        or row.session_id != str(invocation.session_id) or row.runtime_task_id != invocation.run_id
        or row.span_type != "skill_input" or row.status != "captured" or row.name != invocation.tool_name
        or invocation.tool_name not in {"load_skill", "run_skill_tool", "tool_search"}
        or proof.get("schema") != "hive.skill_input_evidence.v1"
        or proof.get("invocation_id") != str(invocation.id) or proof.get("model_result_id") != str(model_result.id)
        or proof.get("model_request_hash") != model_result.model_request_hash
        or _sha256(model_result.model_request_snapshot_json) != model_result.model_request_hash
        or proof.get("authority_snapshot_hash") != invocation.authority_snapshot_hash
        or proof.get("effective_args_hash") != _sha256({"tool_name": invocation.tool_name, "arguments": arguments})
        or proof.get("tool_result_hash") != content_hash
        or proof.get("source_hash") != _sha256({key: value for key, value in proof.items() if key != "source_hash"})
        or evidence_id != uuid.uuid5(invocation.id, f"skill-input:{proof.get('source_hash')}")):
        raise ContentInputReceiptError("skill_input_unverified", "The selected Skill bytes cannot be bound to this invocation.")
    sources, unbound = proof.get("sources"), proof.get("unbound")
    if (not isinstance(sources, list) or not isinstance(unbound, list) or any(not isinstance(value, str) for value in unbound)
        or any(not isinstance(source, dict) or set(source) != {"source_kind", "source_id", "source_version"}
            or any(not isinstance(value, str) or not value for value in source.values())
            or source["source_kind"] not in {"agent_skill_file", "agent_platform_skill_file", "workspace_artifact"} for source in sources)):
        raise ContentInputReceiptError("skill_input_unverified", "The captured Skill source inventory is invalid.")
    return {"status": "verified", "evidence_ref": f"skill-input:{evidence_id}", "sources": sources, "unbound": unbound}


async def settle_workspace_receipt_read(*, request, read, tool_call_id, result):
    from app.database import tenant_scoped_session
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.tools.result_envelope import render_tool_error

    context = request.context
    if read.observed_sha256 == read.content_sha256:
        evidence = await persist_workspace_execution_evidence(context=context, tool_name=request.tool_name,
            tool_call_id=tool_call_id, arguments=request.arguments, result=result,
            states={}, lineage=[], errors={}, input_read=read)
        return None, evidence
    status, code, quarantine = 503, "workspace_input_read_unverified", "not_attempted"
    if read.observed_sha256 is not None:
        try:
            tenant_id = uuid.UUID(str(context.tenant_id))
            async with tenant_scoped_session(tenant_id) as db:
                await lock_workspace_receipt_write(db, tenant_id, context.agent_id, read.path)
                current = await latest_workspace_receipt(db, tenant_id, context.agent_id, read.path)
                if current is not None and current.id == read.receipt_id:
                    manifest = await db.scalar(select(WorkspaceResourceManifest).where(
                        WorkspaceResourceManifest.tenant_id == tenant_id,
                        WorkspaceResourceManifest.agent_id == context.agent_id,
                        WorkspaceResourceManifest.path == read.path).with_for_update())
                    if manifest is not None:
                        manifest.authority_state = "quarantined"
                        await db.commit()
                        quarantine = "committed"
                else:
                    quarantine = "superseded_receipt"
            status, code = ((409, "workspace_input_revision_changed") if quarantine == "superseded_receipt"
                else (403, "workspace_content_unverified"))
        except Exception:
            code, quarantine = "workspace_input_quarantine_unavailable", "unknown"
    return render_tool_error(tool_name=request.tool_name,
        error_class="auth_or_permission" if status == 403 else "operation_failed" if status == 409 else "provider_unavailable",
        message="The executor could not bind returned bytes to the selected content receipt.",
        provider="content_input_receipt", http_status=status, retryable=status in {409, 503},
        actionable_hint=("Read the latest revision after revalidating its sources." if status == 409
            else "Reconcile the file or regenerate from current authorized inputs."),
        extra={"reason_code": code, "outcome": "denied" if status == 403 else "failed" if status == 409 else "unavailable"}), {
            "status": "denied" if status == 403 else "unavailable", "reason_code": code, "quarantine": quarantine}


async def load_workspace_input_evidence(db, *, agent_id, invocation, model_result, arguments, content_hash, capture):
    from app.models.invocation_span import InvocationSpan
    from app.services.session_tool_runtime import _sha256

    evidence_id = uuid.uuid5(invocation.id, "workspace-input-evidence")
    if capture is None:
        return None  # No current capture is never an empty dependency set.
    if not isinstance(capture, dict) or capture.get("status") != "captured":
        return {"status": "unavailable", "reason_code": "workspace_input_capture_unavailable"}
    if capture.get("receipt_ref") != f"workspace-input:{evidence_id}":
        raise ContentInputReceiptError("workspace_input_evidence_invalid", "The current read capture does not match.")
    row = await db.get(InvocationSpan, evidence_id)
    if row is None:
        raise ContentInputReceiptError("workspace_input_evidence_invalid", "The current read evidence is missing.")
    proof = (row.metadata_json or {}).get("workspace_input_evidence")
    if not isinstance(proof, dict) or row.tenant_id != invocation.tenant_id or row.agent_id != agent_id or (
        row.session_id != str(invocation.session_id) or row.runtime_task_id != invocation.run_id
        or row.span_type != "workspace_input" or proof.get("schema") != "hive.workspace_input_evidence.v1"
        or proof.get("invocation_id") != str(invocation.id) or proof.get("model_result_id") != str(model_result.id)
        or proof.get("model_request_hash") != model_result.model_request_hash
        or _sha256(model_result.model_request_snapshot_json) != model_result.model_request_hash
        or proof.get("authority_snapshot_hash") != invocation.authority_snapshot_hash
        or proof.get("effective_args_hash") != _sha256({"tool_name": invocation.tool_name, "arguments": arguments})
        or proof.get("tool_result_hash") != content_hash
        or proof.get("observed_sha256") != proof.get("content_sha256")
    ):
        raise ContentInputReceiptError("workspace_input_evidence_invalid", "The read evidence is not bound to this invocation.")
    try:
        receipt_id = uuid.UUID(proof["content_input_receipt_id"])
        carrier_ref = workspace_artifact_ref(agent_id, arguments.get("path", ""))
    except (KeyError, TypeError, ValueError) as exc:
        raise ContentInputReceiptError("workspace_input_evidence_invalid", "The input receipt identity is invalid.") from exc
    receipt = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.id == receipt_id,
        ContentInputReceipt.tenant_id == invocation.tenant_id, ContentInputReceipt.carrier_kind == "workspace_artifact",
        ContentInputReceipt.carrier_ref == carrier_ref))
    if receipt is None or proof.get("carrier_ref") != carrier_ref or (
        receipt.revision != proof.get("revision") or receipt.content_sha256 != proof.get("content_sha256")
        or _sha256(receipt.dependencies_json) != proof.get("dependencies_hash")
    ):
        raise ContentInputReceiptError("workspace_input_evidence_invalid", "The exact input receipt has changed or is unavailable.")
    return {"status": "verified", "evidence_ref": f"workspace-input:{evidence_id}",
        "content_input_receipt_id": str(receipt.id), "revision": receipt.revision}


def _workspace_execution_input_sources(proof):
    from app.services.session_model_inputs import _stored_dependencies

    inputs = proof.get("execution_inputs")
    sources = _stored_dependencies(inputs.get("sources")) if isinstance(inputs, dict) else None
    if (not isinstance(inputs, dict) or inputs.get("schema") != "hive.workspace_execution_inputs.v1"
        or inputs.get("observed") is not True or inputs.get("unbound") != [] or sources is None
        or any(row["source_kind"] != "workspace_artifact" for row in sources)):
        raise ContentInputReceiptError("workspace_execution_inputs_unverified", "The copied execution inputs lack complete source receipts.")
    return sources


async def load_workspace_execution_input_evidence(db, *, agent_id, invocation, model_result, arguments, content_hash):
    from app.models.invocation_span import InvocationSpan

    ref, _hashes = await load_workspace_execution_evidence(db, agent_id=agent_id, invocation=invocation,
        model_result=model_result, arguments=arguments, content_hash=content_hash)
    retained = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "workspace-output-evidence"))
    return ref, _workspace_execution_input_sources(retained.metadata_json["workspace_execution_evidence"])


async def load_workspace_execution_evidence(db, *, agent_id, invocation, model_result, arguments, content_hash):
    from app.models.invocation_span import InvocationSpan
    from app.services.session_tool_runtime import _sha256

    evidence_id = uuid.uuid5(invocation.id, "workspace-output-evidence")
    row = await db.get(InvocationSpan, evidence_id)
    proof = (row.metadata_json or {}).get("workspace_execution_evidence") if row is not None else None
    if not isinstance(proof, dict) or row.tenant_id != invocation.tenant_id or row.agent_id != agent_id or (
        row.session_id != str(invocation.session_id) or row.runtime_task_id != invocation.run_id
        or row.span_type != "workspace_output"
        or proof.get("schema") != "hive.workspace_execution_evidence.v1"
        or proof.get("invocation_id") != str(invocation.id)
        or proof.get("model_result_id") != str(model_result.id)
        or proof.get("model_request_hash") != model_result.model_request_hash
        or _sha256(model_result.model_request_snapshot_json) != model_result.model_request_hash
        or proof.get("authority_snapshot_hash") != invocation.authority_snapshot_hash
        or proof.get("effective_args_hash") != _sha256({"tool_name": invocation.tool_name, "arguments": arguments})
        or proof.get("tool_result_hash") != content_hash
    ):
        raise ContentInputReceiptError("workspace_execution_unverified", "The captured output cannot be bound to this invocation.")
    states = proof.get("states")
    lineage = proof.get("lineage")
    errors = proof.get("errors")
    if not isinstance(states, dict) or not isinstance(lineage, list) or not isinstance(errors, dict):
        raise ContentInputReceiptError("workspace_execution_unverified", "The captured output evidence is invalid.")
    hashes = {}
    for item in lineage:
        if not isinstance(item, dict):
            continue
        path, after = item.get("path"), item.get("after_state")
        if not isinstance(path, str) or path in errors or not isinstance(after, dict) or after != states.get(path) or after.get("exists") is not True:
            continue
        digest = after.get("sha256")
        if isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            if invocation.tool_name in {"office_document_create", "office_document_apply"}:
                writers = proof.get("descriptor_writes")
                if not isinstance(writers, list) or not any(isinstance(writer, dict) and writer.get("path") == path
                    and writer.get("verified") is True and writer.get("observed_output_sha256") == digest
                    and writer.get("expected_output_sha256") == digest for writer in writers):
                    continue
            if invocation.tool_name in {"write_file", "fs_write"} and isinstance(arguments.get("content"), str):
                if digest != hashlib.sha256(arguments["content"].encode()).hexdigest():
                    continue
            hashes[path] = digest
    return f"workspace-output:{evidence_id}", hashes
