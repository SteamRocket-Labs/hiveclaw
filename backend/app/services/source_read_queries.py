"""One-statement live reader facts for an existing source check point."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import case, cast, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import joinedload, load_only

from app.models.agent import Agent, AgentPermission
from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
from app.models.security_audit import ResourcePermission
from app.models.tenant import Tenant
from app.models.user import User


# Wider projections extend the fields shared by the same source checks.
SESSION_ID_FIELDS = "id tenant_id agent_id"
SESSION_AUDIENCE_FIELDS = SESSION_ID_FIELDS + " user_id audience_kind"
SESSION_SOURCE_FIELDS = SESSION_AUDIENCE_FIELDS + (
    " external_principal_id source_channel group_installation_ref group_subject_id session_kind runtime_source runtime_task_id")
SESSION_ACTOR_FIELDS = SESSION_SOURCE_FIELDS.replace("user_id ", "user_id peer_agent_id ", 1)
SESSION_ROOT_FIELDS = SESSION_ACTOR_FIELDS + " root_session_id"
USER_READER_FIELDS = "id tenant_id role department_id is_active username display_name"
RUNTIME_ACTOR_FIELDS = ("id tenant_id machine_credential_id root_external_principal_id root_user_id "
    "parent_agent_id parent_session_id")
RUNTIME_DIRECTORY_FIELDS = ("id tenant_id root_user_id parent_agent_id child_agent_id parent_session_id child_session_id")
RUNTIME_RESULT_FIELDS = ("id tenant_id task_type status parent_agent_id child_agent_id child_agent_name "
    "parent_session_id child_session_id root_user_id root_session_id root_runtime_task_id root_external_principal_id "
    "machine_credential_id prompt result_summary metadata_json trace_id depth writer_generation "
    "config_snapshot_hash policy_snapshot_hash")


def _json_rows(model, fields, *where):
    return select(func.jsonb_agg(func.jsonb_build_object(
        *[value for field in fields for value in (field, getattr(model, field))]
    ))).where(*where).correlate_except(model).scalar_subquery()


def _source_reader_query(user, agent_id, source, agent_columns):
    permissions = _json_rows(AgentPermission, ("scope_type", "scope_id", "access_level"),
        AgentPermission.agent_id == Agent.id)
    teams = select(func.jsonb_agg(BusinessTeamMembership.business_team_id)).join(
        BusinessTeam, BusinessTeam.id == BusinessTeamMembership.business_team_id).join(
        User, User.id == BusinessTeamMembership.user_id).where(
            BusinessTeamMembership.user_id == user.id, BusinessTeamMembership.status == "active",
            BusinessTeamMembership.tenant_id == Agent.tenant_id, BusinessTeam.status == "active",
            BusinessTeam.tenant_id == Agent.tenant_id, User.is_active.is_(True),
            User.tenant_id == Agent.tenant_id).correlate(Agent, *([source] if source is not None else [])).scalar_subquery()
    resources = resource_permission_rows("agent", Agent.id, Agent.tenant_id)
    statement = select(agent_columns, Tenant.is_active, permissions, teams, resources)
    statement = (statement.select_from(Agent).where(Agent.id == agent_id) if source is None
        else statement.select_from(source).outerjoin(Agent, Agent.id == agent_id))
    return statement.outerjoin(Tenant, Tenant.id == Agent.tenant_id)


def source_reader_statement(user, agent_id, *, source=None):
    """Select live ACL inputs together; the canonical ACL still decides access."""
    scope = _preflight_inputs_for_statement() if isinstance(agent_id, uuid.UUID) and source is None else None
    key = ("source_reader_statement", user.id, agent_id)
    if scope is not None and key in scope:
        return scope[key]
    statement = _source_reader_query(user, agent_id, source, Agent).options(
        load_only(Agent.id, Agent.tenant_id, Agent.owner_user_id, Agent.creator_id,
            Agent.deleted_at, Agent.deactivated_at, Agent.deactivation_reason, Agent.name,
            Agent.role_description, Agent.status, Agent.agent_class, Agent.agent_type, Agent.created_at),
            joinedload(Agent.owner).load_only(User.id, User.is_active),
            joinedload(Agent.creator).load_only(User.id, User.is_active)).execution_options(populate_existing=True)
    if scope is not None:
        scope[key] = statement
    return statement


def fact_columns(model, fields):
    return case((model.id.is_not(None), func.jsonb_build_object(
        *[value for name in fields.split() for value in (name, getattr(model, name))])))


def transcript_event_columns(scope):
    """Keep the existing ORM read projection at a formerly ORM-read entry."""
    from app.models.chat_transcript_event import ChatTranscriptEvent

    column = ChatTranscriptEvent.__table__.alias("e").c.metadata_json
    scope.statements["transcript_event_columns"] = column.type.column_expression(column)


def transcript_event_row(db, row):
    from app.services.content_input_receipts import _DISCLOSURE_SOURCE_VALIDATIONS, _preflight_inputs

    scope = _DISCLOSURE_SOURCE_VALIDATIONS.get()
    if row is not None and scope is not None and "transcript_event_columns" in scope.statements:
        return (_preflight_inputs(db) or {}).get(("event_orm_projection", row.id), row)
    return row


def fact_object(model, value, *, identities=None):
    if value is None:
        return None
    from sqlalchemy import DateTime, LargeBinary

    key = (model, value.get("id"), frozenset(value))
    if identities is not None and key in identities:
        return identities[key]
    data = dict(value)
    for name, item in value.items():
        column = model.__table__.columns.get(name)
        if column is None:
            continue
        if item is not None and isinstance(column.type, UUID):
            data[name] = uuid.UUID(item)
        elif item is not None and isinstance(column.type, DateTime):
            timestamp = datetime.fromisoformat(item)
            # asyncpg's timestamptz decoder returns UTC. Preserve that exact
            # representation for the existing isoformat-based proof hashes.
            data[name] = timestamp.astimezone(timezone.utc) if timestamp.tzinfo is not None else timestamp
        elif item is not None and isinstance(column.type, LargeBinary):
            data[name] = bytes.fromhex(item.removeprefix("\\x"))
        elif item is not None:
            from app.services.channel_secret_storage import EncryptedDeliveryTargetJSON

            if isinstance(column.type, EncryptedDeliveryTargetJSON):
                data[name] = column.type.process_result_value(item, None)
    if model is Agent:
        for relation in ("owner", "creator"):
            data[relation] = fact_object(User, data.get(relation), identities=identities)
    from app.models.chat_session import ChatSession

    # Session consumers distinguish joined session facts from other source
    # rows by type. Construct a detached projection with no session or lazy IO.
    row = ChatSession(**data) if model is ChatSession else SimpleNamespace(**data)
    if identities is not None:
        identities[key] = row
    return row


def source_reader_facts_statement(user, agent_id, *, source=None, agent_fields=""):
    """The same ACL facts as plain projections, without expiring caller ORM rows."""
    def owner(identity):
        return select(fact_columns(User, "id is_active")).where(User.id == identity).correlate(Agent).scalar_subquery()

    agent = fact_columns(Agent, "id tenant_id owner_user_id creator_id deleted_at deactivated_at " + agent_fields)
    agent = agent.op("||")(func.jsonb_build_object("owner", owner(Agent.owner_user_id), "creator", owner(Agent.creator_id)))
    # An outer join's missing Agent must remain None, not an all-null object.
    agent = case((Agent.id.is_not(None), agent))
    return _source_reader_query(user, agent_id, source, agent)


def _preflight_inputs_for_statement():
    from app.services.content_input_receipts import _DISCLOSURE_SOURCE_VALIDATIONS

    scope = _DISCLOSURE_SOURCE_VALIDATIONS.get()
    return scope.statements if scope is not None and scope.active and scope.source_catalog else None


def source_uuid(value):
    # Match UUID's string normalization without casting malformed stored text.
    normalized = func.lower(func.replace(func.btrim(func.replace(func.replace(value, "urn:", ""), "uuid:", ""), "{}"), "-", ""))
    return case((normalized.op("~")("^[a-f0-9]{32}$"), cast(normalized, UUID(as_uuid=True))))


def resource_permission_rows(kind, resource_id, tenant_id):
    return _json_rows(ResourcePermission, ("principal_type", "principal_id", "actions", "effect",
        "conditions", "revoked_at", "expires_at"), ResourcePermission.resource_type == kind,
        ResourcePermission.resource_id == resource_id, ResourcePermission.tenant_id == tenant_id)


def permission_rows(values):
    rows = []
    for value in values or []:
        fields = dict(value)
        for key in ("scope_id", "principal_id"):
            if fields.get(key) is not None:
                fields[key] = uuid.UUID(fields[key])
        for key in ("revoked_at", "expires_at"):
            if fields.get(key) is not None:
                fields[key] = datetime.fromisoformat(fields[key])
        rows.append(SimpleNamespace(**fields))
    return rows


def model_source_state_columns():
    from app.models.session_v2 import SessionModelResult

    # Reconciliation bookkeeping is live; the immutable request payload is
    # reused separately. Only this one key of the response seal is consulted.
    seal = case((func.jsonb_typeof(SessionModelResult.seal_json) == "object",
        case((SessionModelResult.seal_json == {}, SessionModelResult.seal_json),
            else_=func.jsonb_build_object("provider_response_ref", SessionModelResult.seal_json["provider_response_ref"]))
    ))
    return (SessionModelResult.state, SessionModelResult.reconciliation_owner,
        SessionModelResult.round_committed_event_id, seal)


def reader_columns(*models):
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask

    columns = {
        User: (User.id, User.tenant_id, User.role, User.department_id, User.is_active, User.username, User.display_name),
        ChatSession: (ChatSession.id, ChatSession.tenant_id, ChatSession.agent_id, ChatSession.user_id,
            ChatSession.audience_kind, ChatSession.external_principal_id, ChatSession.source_channel,
            ChatSession.group_installation_ref, ChatSession.group_subject_id, ChatSession.session_kind,
            ChatSession.runtime_source, ChatSession.runtime_task_id),
        RuntimeTask: (RuntimeTask.id, RuntimeTask.tenant_id, RuntimeTask.root_user_id,
            RuntimeTask.parent_agent_id, RuntimeTask.child_agent_id, RuntimeTask.parent_session_id, RuntimeTask.child_session_id),
    }
    return tuple(load_only(*columns[model]) for model in models)


async def load_source_catalogue(db, tenant_id, dependencies, closure_sql):
    """Read the closure and retained evidence in one statement; decide no ACLs."""
    import json
    from sqlalchemy import text
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.invocation_span import InvocationSpan
    from app.models.organization_identity import ContentInputReceipt
    from app.models.runtime_result import RuntimeResultObject, RuntimeResultIntegrationPage
    from app.models.session_v2 import SessionCommand, SessionModelResult, SessionToolInvocation
    from app.services.content_input_receipts import _DISCLOSURE_SOURCE_VALIDATIONS, _preflight_inputs, _catalogue_key
    from app.tools.output_limits import TOOL_OUTPUT_BYTES

    scope, rows = _DISCLOSURE_SOURCE_VALIDATIONS.get(), _preflight_inputs(db)
    if all((source.get("source_kind"), source.get("source_id")) in rows.get("catalogue_sources", ())
        for source in dependencies):
        return
    fields = {
        ContentInputReceipt: "id tenant_id carrier_kind carrier_ref revision content_sha256 dependencies_json",
        SessionToolInvocation: "id tenant_id session_id run_id round_id provider_request_id provider_tool_use_id "
            "tool_name provider_arguments_json invocation_item_id args_hash effective_arguments_json "
            "effective_args_hash authority_snapshot_hash effect_idempotency_key effect_state "
            "execution_fence_ref receipt_ref result_event_id permission_item_id permission_state "
            "permission_request_version permission_authority_snapshot_hash permission_response_schema "
            "permission_expires_at permission_receipt_ref",
        SessionCommand: "id tenant_id session_id namespace principal_type principal_id command_kind "
            "request_json request_hash target_json target_hash",
        SessionModelResult: "id tenant_id session_id run_id round_id turn_id provider_request_id "
            "model_request_hash model_request_snapshot_json state",
        ChatTranscriptEvent: "id tenant_id agent_id session_id run_id invocation_id result_id content_hash item_kind lifecycle",
        InvocationSpan: "id tenant_id agent_id user_id runtime_task_id session_id trace_id span_id span_type "
            "name status input_hash metadata_json",
        RuntimeResultObject: "id tenant_id source_kind source_run_id payload_schema payload_bytes sha256 size_bytes media_type encoding",
        RuntimeResultIntegrationPage: "id tenant_id parent_agent_id parent_session_id parent_user_id root_runtime_task_id "
            "manifest_json manifest_sha256",
    }

    def packed(model, alias):
        return "jsonb_build_object(" + ",".join("'" + field + "'," + alias + "." + field
            for field in fields[model].split()) + ")"

    def columns(model, alias, extra=""):
        return ",".join(f"{alias}.{field}" for field in (fields[model] + " " + extra).split())

    column = ChatTranscriptEvent.__table__.alias("e").c.metadata_json
    metadata = scope.statements.get("transcript_event_columns")
    if metadata is None:
        metadata = column.type.column_expression(column)
    # Both are fixed presentations of the same settled event. Retaining each
    # preserves nested callers' existing raw-path versus ORM-path byte checks.
    metadata_sql = str(metadata.compile(dialect=db.get_bind().dialect, compile_kwargs={"literal_binds": True}))
    # Full request bytes remain necessary for the existing integrity comparison.
    # They are fetched once/model, never by the live-state query below. Keeping
    # that comparison is distinct from trusting the stored hash as its own proof.
    # UUID output is canonical lowercase text. A permissive UUID cast alone
    # would add rows for uppercase/compact IDs that the old text joins missed.
    uuid_text = r"^[0123456789abcdef]{8}(-[0123456789abcdef]{4}){3}-[0123456789abcdef]{12}$"
    statement = f"""
        WITH source_closure AS ({closure_sql}), selected_sources AS MATERIALIZED (
            SELECT kind,id,CASE WHEN id ~ '{uuid_text}' THEN id::uuid END AS uuid_id,
                CASE WHEN split_part(id,chr(10),3) ~ '{uuid_text}'
                    THEN split_part(id,chr(10),3)::uuid END AS third_uuid FROM source_closure
        ), results AS (
            SELECT {columns(RuntimeResultObject,'o')},CASE WHEN o.source_run_id ~ '{uuid_text}'
                THEN o.source_run_id::uuid END AS run_uuid FROM selected_sources s CROSS JOIN LATERAL (
                SELECT {columns(RuntimeResultObject,'o')} FROM runtime_result_objects o
                    WHERE s.kind='runtime_result' AND o.id=s.third_uuid AND o.tenant_id=:tenant_id LIMIT 1
            ) o
        ), tools AS (
            SELECT {columns(SessionToolInvocation,'t')} FROM selected_sources s CROSS JOIN LATERAL (
                SELECT {columns(SessionToolInvocation,'t')} FROM session_tool_invocations t
                    WHERE s.kind='session_tool_invocation' AND t.id=s.uuid_id AND t.tenant_id=:tenant_id
                        AND t.result_event_id IS NOT NULL AND t.permission_state IS DISTINCT FROM 'waiting' LIMIT 1
            ) t
        ), model_ids AS (
            SELECT uuid_id AS id FROM selected_sources WHERE kind='session_model_result'
            UNION SELECT m.id FROM (SELECT DISTINCT run_uuid FROM results) o CROSS JOIN LATERAL (
                SELECT m.id FROM session_model_results m WHERE m.tenant_id=:tenant_id AND m.run_id=o.run_uuid OFFSET 0
            ) m
            UNION SELECT m.id FROM (SELECT DISTINCT session_id,run_id,provider_request_id FROM tools) t
                CROSS JOIN LATERAL (SELECT m.id FROM session_model_results m WHERE m.tenant_id=:tenant_id
                    AND (m.session_id,m.run_id,m.provider_request_id)=(t.session_id,t.run_id,t.provider_request_id) LIMIT 1) m
        ), models AS (
            SELECT {columns(SessionModelResult,'m')} FROM model_ids i CROSS JOIN LATERAL (
                SELECT {columns(SessionModelResult,'m')} FROM session_model_results m WHERE m.id=i.id
                    AND m.tenant_id=:tenant_id AND m.state IN ('sealed','round_committed') LIMIT 1
            ) m
        ), events AS (
            SELECT {columns(ChatTranscriptEvent,'e','metadata_json event_type content')} FROM tools t CROSS JOIN LATERAL (
                SELECT {columns(ChatTranscriptEvent,'e','metadata_json event_type content')}
                    FROM chat_transcript_events e WHERE e.id=t.result_event_id AND e.tenant_id=:tenant_id LIMIT 1
            ) e
        ), tool_runs AS (
            SELECT run_id,array_agg(id::text) AS invocation_ids FROM tools GROUP BY run_id
        ), event_span_ids AS (
            SELECT split_part(pointer.value->>'evidence_ref',':',2) AS id FROM events e CROSS JOIN LATERAL
                jsonb_each(CASE WHEN jsonb_typeof(e.metadata_json #> '{{v2_payload,metadata}}')='object'
                    THEN e.metadata_json #> '{{v2_payload,metadata}}' ELSE '{{}}'::jsonb END) pointer
        ), span_ids AS (
            SELECT uuid_id AS id FROM selected_sources
            UNION SELECT third_uuid FROM selected_sources WHERE kind='runtime_team_context'
            UNION SELECT p.id FROM tool_runs t CROSS JOIN LATERAL (
                SELECT p.id FROM invocation_spans p WHERE p.tenant_id=:tenant_id AND p.runtime_task_id=t.run_id
                    AND p.span_type NOT IN ('workspace_output','remote_action') AND COALESCE(
                        p.metadata_json #>> '{{workspace_input_evidence,invocation_id}}',
                        p.metadata_json #>> '{{memory_result,invocation_id}}')=ANY(t.invocation_ids) OFFSET 0
            ) p
            UNION SELECT CASE WHEN id ~ '{uuid_text}' THEN id::uuid END FROM event_span_ids
        ), spans AS (
            SELECT {columns(InvocationSpan,'p')} FROM span_ids i CROSS JOIN LATERAL (
                SELECT {columns(InvocationSpan,'p')} FROM invocation_spans p WHERE p.id=i.id
                    AND p.tenant_id=:tenant_id AND p.span_type NOT IN ('workspace_output','remote_action') LIMIT 1
            ) p
        )
        SELECT s.kind,s.id,NULL::jsonb AS fields FROM selected_sources s
        UNION ALL SELECT 'workspace_artifact',r.id::text,{packed(ContentInputReceipt,'r')}
            FROM selected_sources s CROSS JOIN LATERAL (SELECT {columns(ContentInputReceipt,'r')}
                FROM content_input_receipts r WHERE s.kind='workspace_artifact' AND r.id=s.uuid_id AND r.tenant_id=:tenant_id LIMIT 1) r
        UNION ALL SELECT 'memory_receipt_binding',r.id::text,jsonb_build_object('id',r.id,'carrier_ref',r.carrier_ref)
            FROM selected_sources s CROSS JOIN LATERAL (SELECT r.id,r.carrier_ref FROM content_input_receipts r
                WHERE s.kind='explicit_memory' AND r.id=s.uuid_id AND r.tenant_id=:tenant_id LIMIT 1) r
        UNION ALL SELECT 'session_tool_invocation',t.id::text,{packed(SessionToolInvocation,'t')} FROM tools t
        UNION ALL SELECT 'session_command',c.id::text,{packed(SessionCommand,'c')}
            FROM selected_sources s CROSS JOIN LATERAL (SELECT {columns(SessionCommand,'c')} FROM session_commands c
                WHERE s.kind='session_command' AND c.id=s.uuid_id AND c.tenant_id=:tenant_id LIMIT 1) c
        UNION ALL SELECT 'session_model_result',m.id::text,{packed(SessionModelResult,'m')} FROM models m
        UNION ALL SELECT 'event',e.id::text,{packed(ChatTranscriptEvent,'e')} ||
            jsonb_build_object('metadata_json',jsonb_build_object('v2_payload',e.metadata_json->'v2_payload')) FROM events e
        UNION ALL SELECT 'event_orm_projection',e.id::text,{packed(ChatTranscriptEvent,'e')} ||
            jsonb_build_object('metadata_json',jsonb_build_object('v2_payload',({metadata_sql})->'v2_payload')) FROM events e
            WHERE e.event_type IN ('tool_result','tool_result.completed') AND octet_length(e.content)>{TOOL_OUTPUT_BYTES}
        UNION ALL SELECT 'span',p.id::text,{packed(InvocationSpan,'p')} FROM spans p
        UNION ALL SELECT 'runtime_result',o.id::text,{packed(RuntimeResultObject,'o')} FROM results o
        UNION ALL SELECT 'runtime_result_page',g.id::text,{packed(RuntimeResultIntegrationPage,'g')}
            FROM selected_sources s CROSS JOIN LATERAL (SELECT {columns(RuntimeResultIntegrationPage,'g')}
                FROM runtime_result_integration_pages g WHERE s.kind='runtime_result_page' AND g.id=s.uuid_id AND g.tenant_id=:tenant_id LIMIT 1) g
    """
    models = {"workspace_artifact": ContentInputReceipt, "session_tool_invocation": SessionToolInvocation,
        "session_command": SessionCommand, "session_model_result": SessionModelResult,
        "event": ChatTranscriptEvent, "event_orm_projection": ChatTranscriptEvent, "span": InvocationSpan,
        "runtime_result": RuntimeResultObject, "runtime_result_page": RuntimeResultIntegrationPage,
        "memory_receipt_binding": ContentInputReceipt}
    selected = await db.execute(text(statement), {"tenant_id": tenant_id, "dependencies": json.dumps(dependencies)})
    if not scope.active:
        return
    rows = scope.inputs.setdefault(db.get_transaction(), {})
    for kind, source_id, payload in selected:
        if payload is None:
            # The ordered validator owns malformed nested-source errors.
            if isinstance(source_id, str):
                scope.source_ids.setdefault(kind, set()).add(source_id)
            rows.setdefault("catalogue_sources", set()).add((kind, source_id))
            continue
        model = models[kind]
        if kind in {"event", "event_orm_projection"} and not isinstance(
            (payload.get("metadata_json") or {}).get("v2_payload"), dict
        ):
            # Legacy top-level metadata retains its existing live ORM fallback.
            # Only the canonical payload has an immutable writer boundary.
            continue
        row = fact_object(model, payload)
        if kind == "event_orm_projection":
            rows[kind, row.id] = row
            scope.retained[kind, row.id] = row
            continue
        if kind == "memory_receipt_binding":
            rows[kind, row.id] = row
            continue
        if model is SessionModelResult and row.state not in {"sealed", "round_committed"}:
            # Prepared/streaming/failed retry rows still have snapshot writers.
            # Their existing live recovery reader must own those observations.
            continue
        rows[model, row.id] = row
        if model is SessionModelResult and row.state in {"sealed", "round_committed"}:
            scope.model_digests["immutable_model_digest", id(row.model_request_snapshot_json)] = (
                row.model_request_snapshot_json, None)
    index_source_catalogue(rows)
    scope.retained.update({key: set(row) if key == "catalogue_sources" else row
        for key, row in rows.items() if _catalogue_key(key, row)})


def index_source_catalogue(rows):
    """Rebuild lookup indexes from retained rows, without any live facts."""
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand, SessionModelResult, SessionToolInvocation

    rows["tool_models"] = {(row.session_id, row.run_id, row.provider_request_id): row
        for key, row in rows.items() if isinstance(key, tuple) and key[0] is SessionModelResult}
    rows["workspace_output_span_ids"] = {uuid.uuid5(row.id, "workspace-output-evidence")
        for key, row in rows.items() if isinstance(key, tuple) and key[0] is SessionToolInvocation and row is not None}
    for key, source in list(rows.items()):
        if not isinstance(key, tuple) or key[0] not in {SessionCommand, SessionToolInvocation}:
            continue
        linked = rows.get((ChatTranscriptEvent, source.result_event_id)) if key[0] is SessionToolInvocation else None
        rows["session_source", key[0], source.id] = (source, None, linked)
