"""Preserve base disclosure semantics while batching delivered-file sources."""

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent, AgentPermission
from app.models.chat_session import ChatSession
from app.models.organization_identity import ContentInputReceipt
from app.models.tenant import Tenant
from app.models.user import User
from tests.integration.test_r15e_artifact_graph_reads_postgres import graph, native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4g_learned_skill_sources_postgres import learned_skill  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _deliver(n, dependency, owner_sessionmaker, app_user_sessionmaker):
    from app.models.chat_artifact import ChatArtifact

    n.sources = [dependency]
    path = "workspace/review-regression.md"
    body = "Retained review regression result.\n"
    output, _ = await memory_call(n, app_user_sessionmaker, "write_file", {"path": path, "content": body})
    async with owner_sessionmaker() as db:
        artifact = (await db.scalars(select(ChatArtifact).where(
            ChatArtifact.runtime_task_id == n.run, ChatArtifact.path == path))).one()
        assert artifact.snapshot_json["snapshot_storage_path"], output
        receipt = await db.get(ContentInputReceipt, uuid.UUID(artifact.snapshot_json["content_input_receipt_id"]))
        # Isolate this retained source, as the production-shape fixture does
        # when distributing its sources over historical receipt rows.
        receipt.dependencies_json = [dependency]
        await db.commit()
        return f"/agents/{n.agent}/files/artifacts/{artifact.id}/download", body.encode()


async def _private_memory(n, owner_sessionmaker):
    """A separate source session lets the download's own agent stay unchanged."""
    from app.services.explicit_memory_authority import AUTHORITY_SCHEMA

    agent_id, session_id, receipt_id = (uuid.uuid4() for _ in range(3))
    entry = "private-review-source"
    path = n.root.parent / str(agent_id) / f"memory/explicit/entries/{entry}.md"
    path.parent.mkdir(parents=True)
    path.write_text("Private retained memory source.\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=agent_id, tenant_id=n.tenant, creator_id=n.user, name="Original memory employee"))
        await db.flush()
        db.add(ChatSession(id=session_id, tenant_id=n.tenant, agent_id=agent_id, user_id=n.user,
            audience_kind="private"))
        db.add(ContentInputReceipt(id=receipt_id, tenant_id=n.tenant, carrier_kind="explicit_memory",
            carrier_ref=f"{agent_id}\n{entry}", revision=1, content_sha256=digest,
            authority_json={"schema": AUTHORITY_SCHEMA, "state": "active", "audience_kind": "private",
                "tenant_id": str(n.tenant), "agent_id": str(agent_id), "owner_user_id": str(n.user),
                "source_session_id": str(session_id)}, dependencies_json=[]))
        await db.commit()
    return agent_id, session_id, {"source_kind": "explicit_memory", "source_id": str(receipt_id), "source_version": digest}


@pytest.mark.parametrize("source_hr", [False, True])
async def test_roster_matches_base_null_owner_and_group_filters(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch, source_hr,  # noqa: F811
):
    from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
    from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model
    from tests.integration.test_r15e_artifact_graph_reads_postgres import _profile

    n = native_workspace
    now = datetime.now(timezone.utc)
    ids = {name: uuid.uuid4() for name in ("explicit", "fallback", "legacy", "public", "private", "hr", "inactive", "deleted", "deactivated")}
    group_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        source = await db.get(Agent, n.agent)
        source.owner_user_id = n.user
        if source_hr:
            source.name, source.agent_class = "__system_hr__", "internal_system"
        for name, identity in ids.items():
            db.add(Agent(id=identity, tenant_id=n.tenant, creator_id=n.user if name == "fallback" else n.foreign,
                owner_user_id=n.user if name == "explicit" else None if name in {"legacy", "fallback"} else n.foreign,
                name="__system_hr__" if name == "hr" else name, agent_class="internal_system" if name == "hr" else "internal_tenant",
                status="stopped" if name == "inactive" else "idle",
                deleted_at=now if name == "deleted" else None, deactivated_at=now if name == "deactivated" else None))
        await db.flush()
        for name in ("legacy", "public", "hr", "explicit", "fallback", "inactive", "deleted", "deactivated"):
            db.add(AgentPermission(tenant_id=n.tenant, agent_id=ids[name], scope_type="company", access_level="use"))
        for name, status, expired, source_active in (("live", "active", False, True), ("expired", "active", True, True),
            ("inactive", "revoked", False, True), ("source_inactive", "active", False, False)):
            gid = group_id if name == "live" else uuid.uuid4()
            db.add(AgentCollaborationGroup(id=gid, tenant_id=n.tenant, name=name, created_by_user_id=n.user,
                status=status, expires_at=now - timedelta(days=1) if expired else None))
            await db.flush()
            for key, identity in {"source": n.agent, **ids}.items():
                db.add(AgentCollaborationGroupMember(tenant_id=n.tenant, group_id=gid, agent_id=identity,
                    agent_owner_user_id=n.user if key in {"source", "explicit", "fallback"} else n.foreign,
                    status="revoked" if key == "private" or (key == "source" and not source_active) else "active"))
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        with _profile(monkeypatch) as profile:
            roster = await build_a2a_collaboration_read_model(db, n.agent)
    # These exact sets are recorded from 42184913 on the same fixture. In
    # particular NOT(NULL OR FALSE) excludes a legacy other-creator peer.
    assert {row["id"] for row in roster["same_owner_agents"]} == (set() if source_hr else {str(ids[k]) for k in ("explicit", "fallback")})
    assert {row["id"] for row in roster["public_agents"]} == (set() if source_hr else {str(ids["public"])})
    assert {row["group_id"] for row in roster["collaboration_groups"]} == (set() if source_hr else {str(group_id)})
    if not source_hr:
        assert {row["agent_id"] for row in roster["collaboration_groups"][0]["members"]} == {
            str(ids[key]) for key in ("explicit", "fallback", "legacy", "public")}
    print("A2R2_ROSTER", json.dumps({"source_hr": source_hr,
        "same_owner": [row["name"] for row in roster["same_owner_agents"]],
        "public": [row["name"] for row in roster["public_agents"]],
        "groups": [[row["name"] for row in group["members"]] for group in roster["collaboration_groups"]],
        "statements": sum(row["statements"] for row in profile.values())}), flush=True)
    assert sum(row["statements"] for row in profile.values()) == 1


@pytest.mark.parametrize("source", ["directory", "reference"])
async def test_org_admin_downloads_platform_observer_from_another_home_tenant(
    graph, owner_sessionmaker, app_user_sessionmaker, source,  # noqa: F811
):
    n = graph
    home = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home, name="Platform observer home", slug=f"observer-{home.hex}"))
        await db.flush()
        observer = await db.get(User, n.user)
        observer.role = "org_admin"
        await db.commit()
    url, body = await _deliver(n, getattr(n.common, source), owner_sessionmaker, app_user_sessionmaker)
    from app.core.security import get_current_user

    authenticated_reader = None

    async def current_reader():
        nonlocal authenticated_reader
        if authenticated_reader is not None:
            return authenticated_reader
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            authenticated_reader = await db.get(User, n.user)
            assert authenticated_reader.role == "org_admin"
            db.expunge(authenticated_reader)
        # Authentication has already captured this request's org-admin
        # frame. The live observer identity changes before source reads.
        # Keeping the same subject also preserves base's directory audience.
        async with owner_sessionmaker() as writer:
            observer = await writer.get(User, n.user)
            observer.role, observer.tenant_id = "platform_admin", home
            await writer.commit()
        return authenticated_reader

    n.client._transport.app.dependency_overrides[get_current_user] = current_reader
    n.monkeypatch.setattr("app.api.files._load_download_user_from_jwt", lambda **_kwargs: current_reader())
    response = await n.client.get(url)
    assert authenticated_reader is not None
    assert response.status_code == 200, response.text
    assert response.content == body


async def test_group_memory_preserves_missing_session_error_after_recursion(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from inspect import signature
    from app.services import explicit_memory_authority as memory
    from app.services.content_input_receipts import ContentInputReceiptError, group_source_read

    n = native_workspace
    _, source_session, dependency = await _private_memory(n, owner_sessionmaker)
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        session = await db.get(ChatSession, source_session)
        session.audience_kind, session.user_id = "group", None
        session.source_channel = "feishu"
        session.group_installation_ref, session.group_subject_id = "review-installation", "review-group"
        receipt = await db.get(ContentInputReceipt, uuid.UUID(dependency["source_id"]))
        # Seed a retained group source for the administrator's real business
        # inspection path; its bytes and empty upstream graph stay identical.
        receipt.authority_json = {**receipt.authority_json, **memory.audience_for_session(session)}
        await db.commit()
    validate, reached_memory_recursion = memory._validate_input_dependencies, False

    async def delete_between_memory_checkpoints(db, tenant_id, dependencies, **context):
        nonlocal reached_memory_recursion
        if uuid.UUID(dependency["source_id"]) in context.get("active_workspace_receipts", ()) and not reached_memory_recursion:
            async with owner_sessionmaker() as writer:
                await writer.delete(await writer.get(ChatSession, source_session))
                await writer.commit()
            reached_memory_recursion = True
        return await validate(db, tenant_id, dependencies, **context)

    monkeypatch.setattr(memory, "_validate_input_dependencies", delete_between_memory_checkpoints)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        # 42184913 predates source_catalog and already reads these sources live.
        catalog = {"source_catalog": True} if "source_catalog" in signature(group_source_read).parameters else {}
        with group_source_read(batch_inputs=True, cache_authority=False, **catalog) as scope:
            scope.browser_reader = user
            with pytest.raises(ContentInputReceiptError) as denied:
                await memory.validate_browser_memory_dependency(db, n.tenant, dependency, user=user)
    assert reached_memory_recursion
    assert denied.value.status_code == 403
    assert denied.value.code == "explicit_memory_group_source_changed"


async def test_directory_scope_recovers_platform_observer_and_checks_selected_tenant(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.services.agent_directory_sources import _scope
    from app.services.content_input_receipts import ContentInputReceiptError, group_source_read

    n, home = native_workspace, uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home, name="Global observer home", slug=f"scope-{home.hex}"))
        await db.flush()
        observer = await db.get(User, n.user)
        observer.role, observer.tenant_id = "platform_admin", home
        (await db.get(User, n.foreign)).role = "org_admin"
        await db.commit()
    arguments = dict(tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, user_id=n.user, run_id=n.run)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        browser = await db.get(User, n.foreign)
        with group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True) as scope:
            scope.browser_reader = browser
            observer, agent = await _scope(db, **arguments)
            assert observer.id == n.user
            assert observer.role == "platform_admin"
            assert observer.tenant_id == n.tenant
            assert agent.id == n.agent
            with monkeypatch.context() as patch:
                patch.setattr("app.database.get_current_tenant_id", lambda: home)
                with pytest.raises(ContentInputReceiptError) as denied:
                    await _scope(db, **arguments)
                assert denied.value.code == "content_source_access_denied"


@pytest.mark.parametrize("replacement_access", ["deactivated", "no_grant", "new_only"])
async def test_explicit_memory_download_rechecks_current_session_agent_after_recursion(
    graph, owner_sessionmaker, app_user_sessionmaker, monkeypatch, replacement_access,  # noqa: F811
):
    from app.services import explicit_memory_authority as memory

    n = graph
    original, source_session, dependency = await _private_memory(n, owner_sessionmaker)
    url, body = await _deliver(n, dependency, owner_sessionmaker, app_user_sessionmaker)
    replacement = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=replacement, tenant_id=n.tenant, creator_id=n.foreign if replacement_access == "no_grant" else n.user,
            name="Current source employee", deactivated_at=datetime.now(timezone.utc) if replacement_access == "deactivated" else None))
        await db.commit()
    validate, reached_memory_recursion = memory._validate_input_dependencies, False

    async def rebind_between_memory_checkpoints(db, tenant_id, dependencies, **context):
        nonlocal reached_memory_recursion
        if uuid.UUID(dependency["source_id"]) in context.get("active_workspace_receipts", ()) and not reached_memory_recursion:
            async with owner_sessionmaker() as writer:
                (await writer.get(ChatSession, source_session)).agent_id = replacement
                if replacement_access == "new_only":
                    (await writer.get(Agent, original)).deactivated_at = datetime.now(timezone.utc)
                await writer.commit()
            reached_memory_recursion = True
        return await validate(db, tenant_id, dependencies, **context)

    monkeypatch.setattr(memory, "_validate_input_dependencies", rebind_between_memory_checkpoints)
    response = await n.client.get(url)
    assert reached_memory_recursion, response.text
    assert response.status_code == (200 if replacement_access == "new_only" else 403), response.text
    if replacement_access == "new_only":
        assert response.content == body
    else:
        assert response.json()["detail"]["code"] == "content_source_access_denied"
        assert body not in response.content


@pytest.mark.parametrize("kind", ["session_actor_identity", "explicit_memory"])
async def test_visible_source_with_invisible_agent_keeps_base_error_code(
    graph, owner_sessionmaker, app_user_sessionmaker, kind,  # noqa: F811
):
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_session_actor_identity, group_source_read
    from app.services.explicit_memory_authority import validate_browser_memory_dependency

    n, home = graph, uuid.uuid4()
    if kind == "explicit_memory":
        source_agent, _session, dependency = await _private_memory(n, owner_sessionmaker)
    else:
        source_agent, dependency = n.agent, n.common.actor
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home, name="Invisible source company", slug=f"invisible-{home.hex}"))
        await db.flush()
        (await db.get(Agent, source_agent)).tenant_id = home
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        with group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True) as scope:
            scope.browser_reader = user
            with pytest.raises(ContentInputReceiptError) as denied:
                if kind == "explicit_memory":
                    await validate_browser_memory_dependency(db, n.tenant, dependency, user=user)
                else:
                    await _validate_session_actor_identity(db, n.tenant, dependency, user=user, runtime_request=None)
        assert denied.value.status_code == 403
        assert denied.value.code == "content_source_access_denied"


@pytest.mark.parametrize("seal_after_preload", [False, True])
async def test_learned_skill_source_sees_live_seal_for_tool_only_model(
    graph, owner_sessionmaker, app_user_sessionmaker, seal_after_preload,  # noqa: F811
):
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.content_input_receipts import _preload_execution_sources, group_source_read
    from app.services.learned_skill_sources import model_source, validate_model_source

    n, tool_dependency = graph, graph.leaves["session_tool_invocation"]
    async with owner_sessionmaker() as db:
        tool = await db.get(SessionToolInvocation, uuid.UUID(tool_dependency["source_id"]))
        result = (await db.scalars(select(SessionModelResult).where(SessionModelResult.session_id == tool.session_id,
            SessionModelResult.run_id == tool.run_id, SessionModelResult.provider_request_id == tool.provider_request_id))).one()
        dependency, seal = model_source(result), deepcopy(result.seal_json)
        assert seal["result_id"] == str(result.id)
        if seal_after_preload:
            result.seal_json = None
            await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        with group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True) as scope:
            scope.browser_reader = user
            await _preload_execution_sources(db, n.tenant, [tool_dependency])
            if seal_after_preload:
                async with owner_sessionmaker() as writer:
                    (await writer.get(SessionModelResult, result.id)).seal_json = seal
                    await writer.commit()
            checked, _session = await validate_model_source(db, n.tenant, dependency, user=user)
            assert checked.seal_json == seal


async def test_remote_action_span_is_read_live_after_preload(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.models.invocation_span import InvocationSpan
    from app.services.content_input_receipts import _input_row, _preload_execution_sources, group_source_read

    n, span_id = native_workspace, uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(InvocationSpan(id=span_id, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            runtime_task_id=n.run, session_id=str(n.session), trace_id=str(n.run), span_id=str(span_id),
            span_type="remote_action", name="remote_action", status="running",
            started_at=datetime.now(timezone.utc), metadata_json={"phase": "running"}))
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        with group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True):
            await _preload_execution_sources(db, n.tenant, [{"source_kind": "agent_directory_observation",
                "source_id": str(span_id), "source_version": "fixture"}])
            first = await _input_row(db, InvocationSpan, span_id)
            assert first.metadata_json == {"phase": "running"}
            async with owner_sessionmaker() as writer:
                row = await writer.get(InvocationSpan, span_id)
                row.status, row.metadata_json = "completed", {"phase": "completed"}
                await writer.commit()
            second = await _input_row(db, InvocationSpan, span_id)
            assert second.status == "completed"
            assert second.metadata_json == {"phase": "completed"}


async def test_explicit_facts_preserve_platform_admin_owner_bypass(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from fastapi import HTTPException
    from app.core.permissions import agent_access_from_facts, check_agent_access
    from app.services.source_read_queries import source_reader_statement

    n, home = native_workspace, uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home, name="Owner home", slug=f"owner-{home.hex}"))
        await db.flush()
        owner = await db.get(User, n.foreign)
        owner.tenant_id, owner.is_active = home, False
        (await db.get(User, n.user)).role = "platform_admin"
        (await db.get(Agent, n.agent)).owner_user_id = n.foreign
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        with pytest.raises(HTTPException) as base_denied:
            await check_agent_access(db, user, n.agent)
        assert base_denied.value.status_code == 410
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        # A source point can refresh the reader's role after it selected the
        # ordinary joined path. Its tenant-visible owner join is insufficient
        # for the now-global reader; retain the base audited owner lookup.
        facts = (await db.execute(source_reader_statement(user, n.agent))).first()
        assert facts[0].owner is None
        with pytest.raises(HTTPException) as joined_denied:
            await agent_access_from_facts(db, user, facts)
        assert joined_denied.value.status_code == base_denied.value.status_code


@pytest.mark.parametrize("seal_after_preload", [False, True])
async def test_download_retains_learned_skill_with_tool_only_model_source(
    learned_skill, owner_sessionmaker, app_user_sessionmaker, monkeypatch, seal_after_preload,  # noqa: F811
):
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.config_revision import ConfigRevision
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services import skill_source_receipts as skills
    from app.services.config_versioning import save_revision
    from app.services.learned_skill_sources import model_source
    from app.services.session_model_inputs import tool_result_source_hash
    from app.services.session_semantic_history import _event_payload

    shared, submission = learned_skill
    n = shared.n
    async with owner_sessionmaker() as db:
        tool = await db.get(SessionToolInvocation, submission.id)
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.session_id == tool.session_id,
            SessionModelResult.run_id == tool.run_id, SessionModelResult.provider_request_id == tool.provider_request_id))).one()
        model_id, seal = model.id, deepcopy(model.seal_json)
        tool_dependency = {"source_kind": "session_tool_invocation", "source_id": str(tool.id),
            "source_version": tool_result_source_hash(_event_payload(await db.get(ChatTranscriptEvent, tool.result_event_id)))}
        install = (await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == n.tenant,
            ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.is_active.is_(True),
            ConfigRevision.content["folder_name"].astext == n.first_learned_skill_path.split("/")[1]))).one()
        content = deepcopy(install.content)
        # Retain the actual submitted method's sealed model as review evidence.
        # All candidate, installation and file validators remain enabled.
        content["learning"]["review_dependencies"].append(model_source(model))
        install = await save_revision(db, tenant_id=n.tenant, entity_type="agent_skill_install",
            entity_id=install.entity_id, change_source="evolve", changed_by_user_id=n.user,
            changed_by_agent_id=n.agent, content=content, change_message="Retain the submitted method's sealed model source")
        dependency = {"source_kind": "learned_skill_file", "source_id":
            f"{n.agent}\n{n.first_learned_skill_path}\n{install.id}", "source_version": content["files"][n.first_learned_skill_path]}
        await db.commit()
    url, body = await _deliver(n, dependency, owner_sessionmaker, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        artifact = await db.get(ChatArtifact, uuid.UUID(url.split("/")[-2]))
        receipt = await db.get(ContentInputReceipt, uuid.UUID(artifact.snapshot_json["content_input_receipt_id"]))
        # The closure discovers the model through this tool, not a direct
        # model dependency. The Skill's review evidence is read later.
        receipt.dependencies_json = [dependency, tool_dependency]
        if seal_after_preload:
            (await db.get(SessionModelResult, model_id)).seal_json = None
        await db.commit()
    validate, restored = skills.validate_skill_file_dependency, False

    async def restore_at_skill_point(db, tenant_id, source, **context):
        nonlocal restored
        if source == dependency and seal_after_preload and not restored:
            async with owner_sessionmaker() as writer:
                (await writer.get(SessionModelResult, model_id)).seal_json = seal
                await writer.commit()
            restored = True
        return await validate(db, tenant_id, source, **context)

    monkeypatch.setattr(skills, "validate_skill_file_dependency", restore_at_skill_point)
    response = await n.client.get(url)
    assert restored is seal_after_preload
    assert response.status_code == 200, response.text
    assert response.content == body
