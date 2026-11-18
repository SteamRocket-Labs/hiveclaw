"""Live memory checkpoints and sibling graphs retain their base refusals."""

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.organization_identity import ContentInputReceipt
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services import content_input_receipts as sources
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory, memory_call  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_memory, save_group  # noqa: F401
from tests.integration.test_m4e_team_memory_authority_postgres import runtime_team  # noqa: F401
from tests.integration.test_m4f_team_memory_runtime_postgres import readable_team_memory  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _context(n, *, session=None, run=None):
    return ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant),
        session_id=str(session or n.session), runtime_task_id=str(run or n.run), workspace=n.root / "workspace")


async def _memory_receipt(n, entry_id, factory):
    async with tenant_scoped_session(n.tenant, session_factory=factory) as db:
        return await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == n.tenant,
            ContentInputReceipt.carrier_kind == "explicit_memory",
            ContentInputReceipt.carrier_ref == f"{n.agent}\n{entry_id}").order_by(ContentInputReceipt.revision.desc()).limit(1))


async def _withdraw(n, identity, factory):
    async with tenant_scoped_session(n.tenant, session_factory=factory) as db:
        row = await db.get(ContentInputReceipt, identity)
        db.add(ContentInputReceipt(id=uuid.uuid4(), tenant_id=row.tenant_id, carrier_kind=row.carrier_kind,
            carrier_ref=row.carrier_ref, revision=row.revision + 1, content_sha256=row.content_sha256,
            authority_json={**row.authority_json, "state": "revoked"}, dependencies_json=row.dependencies_json))
        await db.commit()


async def test_memory_bulk_refresh_is_not_a_call_snapshot(
    shared_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.services import explicit_memory_authority as memory
    from app.tools.handlers import memory as tools

    n = shared_memory.n
    entries = load_explicit_overlay_entries(n.root.parent, n.agent)
    observed = []

    async def stage(*_args, **_kwargs):
        async with memory.tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            first = await memory._latest_entry_receipts(db, n.tenant, n.agent, entries)
            receipt = first[memory.memory_ref(n.agent, shared_memory.entry_id)]
            assert receipt.authority_json["state"] == "active"
            await _withdraw(n, receipt.id, owner_sessionmaker)
            refreshed = await memory._latest_entry_receipts(db, n.tenant, n.agent, entries)
            observed.append(refreshed[memory.memory_ref(n.agent, shared_memory.entry_id)].authority_json["state"])
        return "checkpoint probe"

    monkeypatch.setattr(tools, "_search_memory", stage)
    await tools.search_memory(n.agent, {"query": "release"}, execution_context=_context(n))
    assert observed == ["revoked"]


@pytest.mark.parametrize("tool", ["search_memory", "load_memory"])
async def test_final_memory_record_refreshes_after_selection(
    shared_memory, owner_sessionmaker, monkeypatch, tool,
):
    from app.services import explicit_memory_tool_sources as output

    n = shared_memory.n
    receipt = await _memory_receipt(n, shared_memory.entry_id, owner_sessionmaker)
    original = output.record_memory_result
    changed = []

    async def record(request, **kwargs):
        if request.tool_name == tool and kwargs.get("entries") and not changed:
            await _withdraw(n, receipt.id, owner_sessionmaker)
            changed.append(receipt.id)
        return await original(request, **kwargs)

    monkeypatch.setattr(output, "record_memory_result", record)
    args = {"query": "release", "scope": "facts"} if tool == "search_memory" else {"ids": [shared_memory.entry_id]}
    result = str(await n.service.execute(tool, args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert changed == [receipt.id]
    assert "ORCHID_PRIVATE_MARKER" not in result, result
    assert '"reason_code": "explicit_memory_source_changed"' in result, result


async def test_memory_post_frame_actor_refreshes_current_reader(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.services import explicit_memory_authority as memory
    from app.tools.handlers import memory as tools

    n = native_workspace

    async def stage(*_args, **_kwargs):
        request = ToolExecutionRequest("load_memory", {}, _context(n))
        async with memory.tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            with sources.group_source_read(batch_inputs=True, source_catalog=True):
                await memory._actor(db, n.tenant, request)
                async with owner_sessionmaker() as writer:
                    (await writer.get(ChatSession, n.session)).user_id = n.foreign
                    await writer.commit()
            with pytest.raises(sources.ContentInputReceiptError) as denied:
                await memory._actor(db, n.tenant, request)
            assert (denied.value.code, denied.value.status_code) == ("content_source_access_denied", 403)
        return "checkpoint probe"

    monkeypatch.setattr(tools, "_search_memory", stage)
    await tools.search_memory(n.agent, {"query": "release"}, execution_context=_context(n))


async def test_group_member_removal_during_final_memory_walk_is_rechecked(
    group_memory, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.services import explicit_memory_authority as memory, explicit_memory_tool_sources as output

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    original_entry, original_walk = output._authorize_entry, memory._validate_input_dependencies
    recording, removed = [], []

    async def entry(*args, **kwargs):
        recording.append(True)
        try:
            return await original_entry(*args, **kwargs)
        finally:
            recording.pop()

    async def walk(*args, **kwargs):
        if recording and g.source_id in kwargs.get("active_workspace_receipts", ()) and not removed:
            g.members.remove("memory-author")
            removed.append(g.source_id)
        return await original_walk(*args, **kwargs)

    monkeypatch.setattr(output, "_authorize_entry", entry)
    monkeypatch.setattr(memory, "_validate_input_dependencies", walk)
    # Current work stays private; only the recalled entry's origin group changes.
    result = str(await n.service.execute("search_memory", {"query": "release", "scope": "facts"},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert removed == [g.source_id]
    assert "GROUP_RELEASE_MARKER" not in result, result
    assert '"reason_code": "group_membership_required"' in result, result


async def test_after_memory_session_does_not_reuse_a_killed_task(
    group_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.services import explicit_memory_tool_sources as output, shared_group_authority as groups

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    original_actor, original_record = groups.load_shared_group_runtime_actor, output.record_memory_result
    held_tasks, changed = [], []

    async def actor(db, *, task, session):
        # A nested validation may retain an ORM row throughout the call.
        held_tasks.append(task)
        return await original_actor(db, task=task, session=session)

    async def record(request, **kwargs):
        if request.tool_name == "load_memory" and kwargs.get("entries") and not changed:
            async with owner_sessionmaker() as writer:
                task = await writer.get(RuntimeTask, g.runs[n.user])
                task.status, task.claimed_by, task.claim_expires_at = "killed", None, None
                await writer.commit()
            changed.append(g.runs[n.user])
        return await original_record(request, **kwargs)

    monkeypatch.setattr(groups, "load_shared_group_runtime_actor", actor)
    monkeypatch.setattr(output, "record_memory_result", record)
    result = str(await n.service.execute("load_memory", {"ids": [g.entry_id]}, agent_id=n.agent, user_id=n.user,
        session_id=str(g.session), runtime_task_id=str(g.runs[n.user]), emit_runtime_hooks=False))
    assert held_tasks and changed == [g.runs[n.user]]
    assert "GROUP_RELEASE_MARKER" not in result, result
    assert '"reason_code": "content_source_access_denied"' in result, result


async def test_team_entry_denial_keeps_independent_explicit_result(
    readable_team_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.services import explicit_memory_authority as memory, team_memory_runtime as teams

    s, _workspace, team = readable_team_memory
    n = s.n
    saved, _ = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"category": "project", "content": "Remember the independent calibration CEDAR_REFERENCE"})
    assert "Saved memory" in saved
    entry = next(row for row in load_explicit_overlay_entries(n.root.parent, n.agent) if "CEDAR_REFERENCE" in row.content)
    origin = await _memory_receipt(n, entry.entry_id, owner_sessionmaker)
    async with owner_sessionmaker() as writer:
        (await writer.get(ContentInputReceipt, team.id)).dependencies_json = [memory.source_dependency(origin)]
        await writer.commit()
    original, changed = teams.read_entries, []

    async def read(**kwargs):
        if not changed:
            await _withdraw(n, origin.id, owner_sessionmaker)
            changed.append(origin.id)
        return await original(**kwargs)

    monkeypatch.setattr(teams, "read_entries", read)
    # Select the independent fact; the withdrawn calibration is only a team dependency.
    result = str(await n.service.execute("search_memory", {"query": "release", "scope": "facts", "limit": 1},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))
    assert changed == [origin.id]
    assert "ORCHID_PRIVATE_MARKER" in result, result
    assert "TEAM_PROJECT_RELEASE_MARKER" not in result, result
    assert '"code": "team_memory_source_unavailable"' in result, result


async def _historical_memories(n, factory):
    from app.services.explicit_memory_authority import AUTHORITY_SCHEMA

    receipts = []
    async with tenant_scoped_session(n.tenant, session_factory=factory) as db:
        for index in range(2):
            entry = f"after-walk-{index}"
            path = n.root / "memory/explicit/entries" / (entry + ".md")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"PRIVATE_HISTORY_{index}\n")
            row = ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="explicit_memory",
                carrier_ref=f"{n.agent}\n{entry}", revision=1, content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                authority_json={"schema": AUTHORITY_SCHEMA, "state": "active", "audience_kind": "private",
                    "tenant_id": str(n.tenant), "agent_id": str(n.agent), "owner_user_id": str(n.user),
                    "source_session_id": str(n.session)}, dependencies_json=[])
            db.add(row)
            receipts.append(row)
        await db.commit()
    return receipts


@pytest.mark.parametrize(("surface", "mutation", "code"), [
    ("browser", "withdrawal", "explicit_memory_source_revoked"),
    ("runtime", "withdrawal", "explicit_memory_source_revoked"),
    ("runtime", "reader", "content_source_access_denied"),
])
async def test_memory_after_vector_follows_both_node_walks(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch, surface, mutation, code,  # noqa: F811
):
    from app.services import explicit_memory_authority as memory

    n = native_workspace
    receipts = await _historical_memories(n, owner_sessionmaker)
    dependencies = [memory.source_dependency(row) for row in receipts]
    original, visited = memory._validate_input_dependencies, []

    async def walk(*args, **kwargs):
        active = kwargs.get("active_workspace_receipts", ())
        for row in receipts:
            if row.id not in active or row.id in visited:
                continue
            visited.append(row.id)
            if row.id == receipts[1].id:
                if mutation == "withdrawal":
                    await _withdraw(n, row.id, owner_sessionmaker)
                else:
                    async with owner_sessionmaker() as writer:
                        (await writer.get(User, n.user)).is_active = False
                        await writer.commit()
        return await original(*args, **kwargs)

    monkeypatch.setattr(memory, "_validate_input_dependencies", walk)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        reader = await db.get(User, n.user)
        with sources.group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True):
            await sources._preload_execution_sources(db, n.tenant, dependencies)
            with pytest.raises(sources.ContentInputReceiptError) as denied:
                await sources._validate_input_dependencies(db, n.tenant, dependencies,
                    **({"user": reader} if surface == "browser" else {
                        "runtime_request": ToolExecutionRequest("load_memory", {}, _context(n))}))
            assert (denied.value.code, denied.value.status_code) == (code, 403)
    assert visited == [row.id for row in receipts]


async def _sibling_sources(n, owner, count):
    from tests.integration.test_artifact_publication_transactions import stage_report

    result = []
    for index in range(count):
        path = f"workspace/sibling-{index}.md"
        async with owner() as db:
            receipt = await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user, session=n.session,
                root=n.root, path=path)
            await db.commit()
        result.append({"source_kind": "workspace_artifact", "source_id": str(receipt.id),
            "source_version": receipt.content_sha256})
    return result


async def test_sibling_workspace_subtrees_share_one_walk_checkpoint(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from tests.integration.test_r15e_artifact_graph_reads_postgres import _profile
    from app.services.explicit_memory_authority import source_dependency

    n = native_workspace
    children = await _sibling_sources(n, owner_sessionmaker, 5)
    roots = await _historical_memories(n, owner_sessionmaker)
    # A non-workspace provenance root has K independent workspace children,
    # so no workspace ancestor can conceal per-sibling read points.
    async with owner_sessionmaker() as writer:
        for root, count in zip(roots, (1, 5)):
            (await writer.get(ContentInputReceipt, root.id)).dependencies_json = children[:count]
        await writer.commit()
    original, observations, totals = sources._workspace_checkpoint_rows, [], []

    async def rows(*args, **kwargs):
        observations.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(sources, "_workspace_checkpoint_rows", rows)
    for root, count in zip(roots, (1, 5)):
        dependencies = [source_dependency(root)]
        observations.clear()
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            reader = await db.get(User, n.user)
            with sources.group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True):
                await sources._preload_execution_sources(db, n.tenant, dependencies)
                with _profile(monkeypatch, delay=0) as profile:
                    await sources._validate_input_dependencies(db, n.tenant, dependencies, user=reader)
                totals.append(sum(row["statements"] for row in profile.values()))
        assert len(observations) == 2, (count, len(observations))
    assert totals[0] == totals[1], totals


async def test_execution_capture_keeps_authorized_paths_without_source_replay(
    native_workspace, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.models.workspace_resource import WorkspaceResourceManifest
    from tests.integration.test_artifact_publication_transactions import stage_report
    from tests.integration.test_a2w_source_graph_reads_postgres import _request

    n = native_workspace
    paths = ["workspace/copy-a.md", "workspace/copy-b.md"]
    originals = []
    async with owner_sessionmaker() as db:
        for index, path in enumerate(paths):
            source = await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user, session=n.session,
                root=n.root, path=f"workspace/source-{index}.md")
            originals.append(source)
            await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user, session=n.session, root=n.root,
                path=path, dependencies=[{"source_kind": "workspace_artifact", "source_id": str(source.id),
                    "source_version": source.content_sha256}])
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == "workspace/source-0.md"))
        manifest.authority_state = "quarantined"
        await db.commit()
    original, calls = sources._workspace_checkpoint_rows, []

    async def rows(*args, **kwargs):
        calls.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(sources, "_workspace_checkpoint_rows", rows)
    n.path = paths[0]
    request = _request(n, "execute_code", {"language": "python", "code": "print('ok')"}, paths)
    result = await sources.authorize_workspace_execution_inputs(request)
    assert result is None, result
    # owner 11-18 decision A: neither copy is withheld when one upstream manifest is quarantined.
    assert request.context.workspace_authority_scope.allowed_paths == frozenset(paths)
    assert [row.path for row in request.context.workspace_authority_scope.input_reads] == paths
    assert calls == []

    # owner 11-18 decision A: both private copies of the quarantined source remain available.
    async with owner_sessionmaker() as db:
        await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user, session=n.session, root=n.root,
            path="workspace/copy-c.md", dependencies=[{"source_kind": "workspace_artifact",
                "source_id": str(originals[0].id), "source_version": originals[0].content_sha256}])
        await db.commit()
    calls.clear()
    request = _request(n, "execute_code", {"language": "python", "code": "print('ok')"}, [*paths, "workspace/copy-c.md"])
    result = await sources.authorize_workspace_execution_inputs(request)
    assert result is None, result
    assert request.context.workspace_authority_scope.allowed_paths == frozenset([*paths, "workspace/copy-c.md"])
    assert [row.path for row in request.context.workspace_authority_scope.input_reads] == [*paths, "workspace/copy-c.md"]
    assert calls == []


async def test_context_kind_is_part_of_a_malformed_binding_projection(
    native_workspace, app_user_sessionmaker,  # noqa: F811
):
    n = native_workspace
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        reader, agent = await db.get(User, n.user), await db.get(Agent, n.agent)
        expected_name = agent.name
        with sources.group_source_read(batch_inputs=True, source_catalog=True) as frame:
            for kind in ("agent_resident_memory", "agent_identity"):
                frame.source_ids[kind] = {f"{n.agent}\nmalformed-binding"}
            with sources.source_read_checkpoint(kind="agent_context_resource", phase="mixed-source-batch"):
                await sources._context_checkpoint_facts(db, n.tenant, reader, "agent_resident_memory")
                identity = await sources._context_checkpoint_facts(db, n.tenant, reader, "agent_identity")
                assert identity[n.agent, None][0].name == expected_name


async def test_root_sessions_share_the_loaded_workspace_checkpoint(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.workspace_resource_authority import _canonical_root_session
    from tests.integration.test_r15e_artifact_graph_reads_postgres import _profile

    n = native_workspace
    children = await _sibling_sources(n, owner_sessionmaker, 5)
    identities = [uuid.uuid4() for _ in children]
    async with owner_sessionmaker() as writer:
        for index, identity in enumerate(identities):
            writer.add(ChatSession(id=identity, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
                audience_kind="private", root_session_id=identity))
            manifest = await writer.scalar(select(WorkspaceResourceManifest).where(
                WorkspaceResourceManifest.agent_id == n.agent,
                WorkspaceResourceManifest.path == f"workspace/sibling-{index}.md"))
            manifest.root_session_id = identity
        await writer.commit()
    totals = []
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        with sources.group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True):
            await sources._preload_execution_sources(db, n.tenant, children)
            async with sources.workspace_read_checkpoint(db, n.tenant):
                group = sources._workspace_reader_group(db, n.tenant, user, None)
                group.before = await sources._workspace_checkpoint_rows(db, n.tenant, user)
                for count in (1, 5):
                    with sources.source_read_checkpoint(kind="workspace_artifact", phase=object()):
                        with _profile(monkeypatch, delay=0) as profile:
                            for identity in identities[:count]:
                                actual = await _canonical_root_session(db, agent_id=n.agent, user=user,
                                    resource_tenant_id=n.tenant, session_id=identity)
                                assert actual == (identity, True)
                        totals.append(sum(row["statements"] for row in profile.values()))
    assert totals == [1, 1], totals
