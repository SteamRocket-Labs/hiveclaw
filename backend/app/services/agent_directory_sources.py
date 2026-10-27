"""Native directory observations, distinct from original employee identity claims."""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.invocation_span import InvocationSpan
from app.services.config_versioning import canonical_content_hash as digest

KIND = "agent_directory_observation"
SCHEMA = "hive.agent_directory_observation.v1"
EXTERNAL_SCHEMA = "hive.agent_directory_observation.external.v1"
SECTIONS = {"same_owner_agents": "id", "public_agents": "id", "collaboration_groups": "group_id"}


def _error(code="content_source_access_denied"):
    from app.services.content_input_receipts import ContentInputReceiptError

    return ContentInputReceiptError(
        code, "The original directory observation is no longer verifiable for this reader.", 403
    )


async def _scope(db, *, tenant_id, agent_id, session_id, user_id, run_id, external_principal_id=None):
    from app.core.permissions import check_agent_access
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_user_identity import load_scoped_runtime_user

    if external_principal_id is not None:
        from app.services.external_principal_service import load_external_context_actor

        session = await db.get(ChatSession, session_id, populate_existing=True)
        task = await db.get(RuntimeTask, run_id, populate_existing=True)
        actor = await load_external_context_actor(
            db, tenant_id=tenant_id, agent_id=agent_id, session=session, task=task
        )
        agent = await db.get(Agent, agent_id, populate_existing=True)
        if (
            user_id is not None
            or actor is None
            or actor.external_principal_id != external_principal_id
            or agent is None
        ):
            raise _error()
        return actor, agent
    user = await load_scoped_runtime_user(db, user_id, tenant_id=tenant_id)
    agent = await db.get(Agent, agent_id, populate_existing=True)
    session = await db.get(ChatSession, session_id, populate_existing=True)
    task = (
        await db.execute(
            select(
                RuntimeTask.root_user_id,
                RuntimeTask.parent_agent_id,
                RuntimeTask.child_agent_id,
                RuntimeTask.parent_session_id,
                RuntimeTask.child_session_id,
            ).where(RuntimeTask.id == run_id, RuntimeTask.tenant_id == tenant_id)
        )
    ).one_or_none()
    if (
        user is None
        or agent is None
        or agent.tenant_id != tenant_id
        or agent.deleted_at is not None
        or agent.deactivated_at is not None
        or session is None
        or session.tenant_id != tenant_id
        or session.agent_id != agent_id
        or session.user_id != user_id
        or session.audience_kind not in {None, "private"}
        or task is None
        or task.root_user_id != user_id
        or agent_id not in {task.parent_agent_id, task.child_agent_id}
        or str(session_id) not in {task.parent_session_id, task.child_session_id}
    ):
        raise _error()
    from fastapi import HTTPException

    try:
        await check_agent_access(db, user, agent_id)
    except HTTPException as exc:
        raise _error() from exc
    return user, agent


def _inventory(directory):
    return [
        {"section": section, "id": row[key], "sha256": digest(row)}
        for section, key in SECTIONS.items()
        for row in directory[section]
    ]


async def observe_directory(db, *, tenant_id, agent_id, runtime_request):
    from app.runtime.prompt_sections.a2a_collaborators import build_a2a_collaborators_section
    from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model
    from app.services.content_input_receipts import _runtime_input_actor

    principal, user = await _runtime_input_actor(db, tenant_id, runtime_request)
    session_id, run_id = uuid.UUID(str(principal.session_id)), uuid.UUID(str(principal.runtime_task_id))
    if principal.agent_id != agent_id:
        raise _error()
    external_id = getattr(user, "external_principal_id", None) if user.id is None else None
    _, agent = await _scope(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user.id,
        run_id=run_id,
        external_principal_id=external_id,
    )
    directory = await build_a2a_collaboration_read_model(db, agent_id)
    content = build_a2a_collaborators_section(directory, max_chars=None)
    proof = {
        "schema": EXTERNAL_SCHEMA if external_id is not None else SCHEMA,
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "run_id": str(run_id),
        "reader_user_id": str(user.id) if user.id is not None else None,
        "agent_owner_id": str(agent.owner_user_id or agent.creator_id),
        "inventory": _inventory(directory),
        "directory": directory,
        "directory_sha256": digest(directory),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    if external_id is not None:
        proof["reader_external_principal_id"] = str(external_id)
    version = digest(proof)
    source_id = uuid.uuid5(session_id, f"agent-directory:{agent_id}:{run_id}:{version}")
    row = await db.get(InvocationSpan, source_id, populate_existing=True)
    if row is None:
        db.add(
            InvocationSpan(
                id=source_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user.id,
                runtime_task_id=run_id,
                session_id=str(session_id),
                trace_id=f"agent-directory:{run_id}",
                span_id=f"agent-directory:{source_id}",
                span_type=KIND,
                name="agent_directory_observed",
                status="captured",
                started_at=datetime.now(timezone.utc),
                duration_ms=0,
                metadata_json={KIND: proof},
            )
        )
        await db.flush()
    elif row.metadata_json != {KIND: proof}:
        raise _error("content_source_changed")
    return content, {"source_kind": KIND, "source_id": str(source_id), "source_version": version}


async def _load(db, *, tenant_id, dependency):
    try:
        source_id = uuid.UUID(dependency["source_id"])
        row = await db.get(InvocationSpan, source_id, populate_existing=True)
        proof = row.metadata_json.get(KIND) if row is not None and isinstance(row.metadata_json, dict) else None
        external = isinstance(proof, dict) and proof.get("schema") == EXTERNAL_SCHEMA
        if (
            dependency.get("source_kind") != KIND
            or not isinstance(proof, dict)
            or set(proof)
            != (
                {
                    "schema",
                    "agent_id",
                    "session_id",
                    "run_id",
                    "reader_user_id",
                    "agent_owner_id",
                    "inventory",
                    "directory",
                    "directory_sha256",
                    "content_sha256",
                }
                | ({"reader_external_principal_id"} if external else set())
            )
            or proof["schema"] not in {SCHEMA, EXTERNAL_SCHEMA}
            or digest(proof) != dependency["source_version"]
            or row.tenant_id != tenant_id
            or str(row.agent_id) != proof["agent_id"]
            or row.session_id != proof["session_id"]
            or str(row.runtime_task_id) != proof["run_id"]
            or (str(row.user_id) if row.user_id is not None else None) != proof["reader_user_id"]
            or (external and row.user_id is not None)
            or row.span_type != KIND
            or row.name != "agent_directory_observed"
            or row.status != "captured"
            or source_id
            != uuid.uuid5(
                uuid.UUID(proof["session_id"]),
                f"agent-directory:{proof['agent_id']}:{proof['run_id']}:{dependency['source_version']}",
            )
        ):
            raise _error("content_source_changed")
        user, agent = await _scope(
            db,
            tenant_id=tenant_id,
            agent_id=row.agent_id,
            session_id=uuid.UUID(row.session_id),
            user_id=row.user_id,
            run_id=row.runtime_task_id,
            external_principal_id=uuid.UUID(proof["reader_external_principal_id"]) if external else None,
        )
        if str(agent.owner_user_id or agent.creator_id) != proof["agent_owner_id"]:
            raise _error()
        return row, proof, user
    except (KeyError, ValueError, TypeError) as exc:
        raise _error("content_source_changed") from exc


async def _render_observed(db, row, proof):
    from app.runtime.prompt_sections.a2a_collaborators import build_a2a_collaborators_section
    from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model

    current = await build_a2a_collaboration_read_model(db, row.agent_id)
    lookup = {section: {item[key]: item for item in current[section]} for section, key in SECTIONS.items()}
    observed = proof["directory"]
    if (
        not isinstance(proof["inventory"], list)
        or not isinstance(observed, dict)
        or set(observed) != set(SECTIONS)
        or _inventory(observed) != proof["inventory"]
    ):
        raise _error("content_source_changed")
    seen = set()
    for entry in proof["inventory"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"section", "id", "sha256"}
            or entry["section"] not in SECTIONS
            or not isinstance(entry["id"], str)
        ):
            raise _error("content_source_changed")
        key = (entry["section"], entry["id"])
        item = lookup[entry["section"]].get(entry["id"])
        if key in seen or item is None:
            raise _error("content_source_changed")
        seen.add(key)
        if entry["section"] == "collaboration_groups":
            previous = next(group for group in observed["collaboration_groups"] if group["group_id"] == entry["id"])
            if not {member["id"] for member in previous["members"]} <= {member["id"] for member in item["members"]}:
                raise _error()
    content = build_a2a_collaborators_section(observed, max_chars=None)
    if (
        digest(observed) != proof["directory_sha256"]
        or hashlib.sha256(content.encode()).hexdigest() != proof["content_sha256"]
    ):
        raise _error("content_source_changed")
    return content


async def validate_directory(db, tenant_id, dependency, *, user=None, runtime_request=None):
    from app.services.content_input_receipts import _runtime_input_actor

    row, proof, original = await _load(db, tenant_id=tenant_id, dependency=dependency)
    external = proof["schema"] == EXTERNAL_SCHEMA
    if runtime_request is not None:
        principal, reader = await _runtime_input_actor(db, tenant_id, runtime_request)
        if reader.id != original.id:
            raise _error()
        if principal.agent_id != row.agent_id:
            from app.services.project_context import project_source_read_allowed

            if external or not project_source_read_allowed(tenant_id, principal):
                raise _error()
        if external and (
            getattr(reader, "external_principal_id", None) != original.external_principal_id
            or str(principal.session_id) != row.session_id
            or str(principal.runtime_task_id) != str(row.runtime_task_id)
        ):
            raise _error()
    elif external:
        raise _error()
    elif user is None or user.id != original.id:
        raise _error()
    await _render_observed(db, row, proof)


async def directory_page_sources(db, *, agent_id, invocation, content, sources):
    if invocation.tool_name != "read_context_resource" or not isinstance(sources, list) or len(sources) != 1:
        raise _error("content_source_changed")
    row, proof, _user = await _load(db, tenant_id=invocation.tenant_id, dependency=sources[0])
    if (
        row.agent_id != agent_id
        or row.session_id != str(invocation.session_id)
        or row.runtime_task_id != invocation.run_id
    ):
        raise _error()
    rendered = await _render_observed(db, row, proof)
    args = invocation.effective_arguments_json or invocation.provider_arguments_json
    try:
        page = json.loads(content)
        offset, limit = args.get("offset", 0), args.get("limit", 6000)
        end = min(offset + limit, len(rendered))
        if (
            args.get("ref") != "a2a-collaborators"
            or args.get("expected_sha256", proof["content_sha256"]) != proof["content_sha256"]
            or set(page)
            != {
                "schema",
                "status",
                "ref",
                "context_ref",
                "source_ref",
                "sha256",
                "offset",
                "next_offset",
                "complete",
                "total_chars",
                "content",
            }
            or page["schema"] != "hive.agent_context_resource_page.v1"
            or page["status"] != "ok"
            or page["ref"] != "a2a-collaborators"
            or page["source_ref"] != "agent-context://a2a-collaborators"
            or page["context_ref"] != page["source_ref"]
            or page["sha256"] != proof["content_sha256"]
            or any(type(page[k]) is not int for k in ("offset", "next_offset", "total_chars"))
            or type(offset) is not int
            or type(limit) is not int
            or not 256 <= limit <= 12000
            or not 0 <= offset <= len(rendered)
            or page["offset"] != offset
            or page["next_offset"] != end
            or page["total_chars"] != len(rendered)
            or page["content"] != rendered[offset:end]
            or page["complete"] is not (end >= len(rendered))
        ):
            raise _error("content_source_changed")
    except (KeyError, ValueError, TypeError) as exc:
        raise _error("content_source_changed") from exc
    return sources


async def owned_directory_source(db, *, dependency, agent_id, tenant_id, session_id, user_id):
    """Publish owned context with tenant-public peers; retain private group boundaries."""
    row, proof, _user = await _load(db, tenant_id=tenant_id, dependency=dependency)
    return (
        row.user_id is not None
        and row.agent_id == agent_id
        and row.session_id == str(session_id)
        and row.user_id == user_id
        and proof["agent_owner_id"] == str(user_id)
        and all(entry.get("section") in {"same_owner_agents", "public_agents"} for entry in proof["inventory"])
    )
