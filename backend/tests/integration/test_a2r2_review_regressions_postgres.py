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


async def test_org_admin_downloads_platform_observer_from_another_home_tenant(
    graph, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    n = graph
    home = uuid.uuid4()
    url, body = await _deliver(n, n.common.directory, owner_sessionmaker, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home, name="Platform observer home", slug=f"observer-{home.hex}"))
        await db.flush()
        observer = await db.get(User, n.user)
        observer.role, observer.tenant_id = "platform_admin", home
        reader = await db.get(User, n.foreign)
        reader.role = "org_admin"
        await db.commit()
    from app.core.security import get_current_user

    async def current_reader():
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            return await db.get(User, n.foreign)

    n.client._transport.app.dependency_overrides[get_current_user] = current_reader
    n.monkeypatch.setattr("app.api.files._load_download_user_from_jwt", lambda **_kwargs: current_reader())
    response = await n.client.get(url)
    assert response.status_code == 200, response.text
    assert response.content == body


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
