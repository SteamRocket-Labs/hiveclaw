"""Round-seven review windows retain the baseline's source refusals."""

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.database import tenant_scoped_session
from app.models.organization_identity import ContentInputReceipt
from app.models.user import User
from app.services import content_input_receipts as sources
from app.tools.runtime import ToolExecutionRequest
from tests.integration.test_a2w_review_boundaries_postgres import (
    _context, _historical_memories, _memory_receipt, _withdraw,
)
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory, memory_call  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_memory  # noqa: F401
from tests.integration.test_m4e_team_memory_authority_postgres import runtime_team  # noqa: F401
from tests.integration.test_m4f_team_memory_runtime_postgres import readable_team_memory  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_failed_single_root_drain_rewalks_shared_memory_for_both_model_items(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,
):
    from app.services.explicit_memory_authority import source_dependency
    from app.services.session_model_round import _validate_or_withhold_model_sources
    from tests.integration.test_a4c_one_item_withheld import _bindings
    from tests.integration.test_m4c_explicit_memory_scope_postgres import new_owner_work

    n = native_workspace
    parents = await _historical_memories(n, owner_sessionmaker)
    memory = ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="explicit_memory",
        carrier_ref=f"{n.agent}\nshared-history", revision=1, content_sha256=parents[0].content_sha256,
        authority_json=parents[0].authority_json, dependencies_json=[])
    (n.root / "memory/explicit/entries/shared-history.md").write_bytes(
        (n.root / "memory/explicit/entries/after-walk-0.md").read_bytes())
    async with owner_sessionmaker() as writer:
        writer.add(memory)
        for parent in parents:
            (await writer.get(ContentInputReceipt, parent.id)).dependencies_json = [source_dependency(memory)]
        await writer.commit()
    roots = [source_dependency(parent) for parent in parents]
    await _withdraw(n, memory.id, owner_sessionmaker)
    session, run = await new_owner_work(n, owner_sessionmaker)
    messages = [{"role": "system", "content": f"WITHDRAWN_MEMORY_COPY_{index}"} for index in range(2)]
    bindings = _bindings(2, {index: [dependency] for index, dependency in enumerate(roots)})
    wire = {"messages": [dict(message) for message in messages]}
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await _validate_or_withhold_model_sources(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=session, run_id=run, input_bindings=bindings, messages=messages, wire_request=wire)
    assert [(entry["status"], entry.get("code")) for entry in bindings["messages"]] == [
        ("withheld", "explicit_memory_source_revoked"), ("withheld", "explicit_memory_source_revoked")]
    assert bindings["dependencies"] == []
    assert all("WITHDRAWN_MEMORY_COPY" not in message["content"] for message in wire["messages"])


@pytest.mark.parametrize("tool", ["load_memory", "search_memory"])
async def test_team_memory_final_reader_is_fresh_inside_the_tool(
    readable_team_memory, owner_sessionmaker, monkeypatch, tool,
):
    from app.services import team_memory_runtime as teams
    from app.tools.handlers import memory as tools

    shared, workspace, _receipt = readable_team_memory
    n = shared.n
    original, checks = teams._runtime_input_actor, []
    read, outcomes = teams.read_entries, []

    async def actor(db, tenant_id, request):
        if inspect.currentframe().f_back.f_code.co_name == "read_entries":
            checks.append(True)
            if len(checks) == 2:
                async with owner_sessionmaker() as writer:
                    (await writer.get(User, n.user)).is_active = False
                    await writer.commit()
        return await original(db, tenant_id, request)

    async def observe_read(**kwargs):
        try:
            result = await read(**kwargs)
        except sources.ContentInputReceiptError as exc:
            outcomes.append(exc.code)
            raise
        outcomes.append("accepted")
        return result

    monkeypatch.setattr(teams, "_runtime_input_actor", actor)
    monkeypatch.setattr(teams, "read_entries", observe_read)
    if tool == "load_memory":
        result = await tools._load_scoped_memory(n.agent, {"ids": [f"team_memory:{workspace}:report"]}, _context(n))
    else:
        result = await tools.search_memory(n.agent, {"query": "release", "scope": "facts"},
            execution_context=_context(n))
    assert len(checks) == 2
    assert outcomes == ["content_source_access_denied"]
    assert "TEAM_PROJECT_RELEASE_MARKER" not in str(result), result
    assert '"reason_code": "content_source_access_denied"' in str(result), result


async def test_record_latest_refresh_follows_the_second_entry_walk(
    shared_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.services import explicit_memory_authority as memory, explicit_memory_tool_sources as output

    n = shared_memory.n
    saved, _invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "Remember the second independent record RECORD_SECOND_MARKER", "category": "project"})
    assert "Saved memory" in saved
    entries = load_explicit_overlay_entries(n.root.parent, n.agent)
    first = next(entry for entry in entries if entry.entry_id == shared_memory.entry_id)
    second = next(entry for entry in entries if "RECORD_SECOND_MARKER" in entry.content)
    pairs = [(entry, await _memory_receipt(n, entry.entry_id, owner_sessionmaker)) for entry in (first, second)]
    original, changed = memory._validate_input_dependencies, []

    async def walk(*args, **kwargs):
        if pairs[1][1].id in kwargs.get("active_workspace_receipts", ()) and not changed:
            await _withdraw(n, pairs[1][1].id, owner_sessionmaker)
            changed.append(pairs[1][1].id)
        return await original(*args, **kwargs)

    monkeypatch.setattr(memory, "_validate_input_dependencies", walk)
    with pytest.raises(sources.ContentInputReceiptError) as denied:
        await output.record_memory_result(ToolExecutionRequest("search_memory", {}, _context(n)),
            content="ORCHID_PRIVATE_MARKER RECORD_SECOND_MARKER", entries=pairs,
            session_factory=app_user_sessionmaker)
    assert changed == [pairs[1][1].id]
    assert (denied.value.code, denied.value.status_code) == ("explicit_memory_source_changed", 403)


async def test_two_uncached_walks_do_not_share_the_after_audience_actor(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,
):
    from app.services.explicit_memory_authority import source_dependency

    n = native_workspace
    receipts = await _historical_memories(n, owner_sessionmaker)
    dependencies = [source_dependency(row) for row in receipts]
    request = ToolExecutionRequest("load_memory", {}, _context(n))
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        with sources.group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True):
            await sources._preload_execution_sources(db, n.tenant, dependencies)
            await sources._validate_input_dependencies(db, n.tenant, dependencies[:1], runtime_request=request)
            async with owner_sessionmaker() as writer:
                (await writer.get(User, n.user)).is_active = False
                await writer.commit()
            with pytest.raises(sources.ContentInputReceiptError) as denied:
                await sources._validate_input_dependencies(db, n.tenant, dependencies[1:], runtime_request=request)
            assert (denied.value.code, denied.value.status_code) == ("content_source_access_denied", 403)


async def test_hidden_skill_folder_is_not_an_original_platform_source(
    native_workspace, app_user_sessionmaker,
):
    from app.services.skill_source_receipts import platform_file_read_source

    n = native_workspace
    invocation = SimpleNamespace(tool_name="read_file", effect_state="effect_committed", tenant_id=n.tenant,
        effective_arguments_json={"path": "skills/.cache/x.md"})
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        with sources.group_source_read(batch_inputs=True, source_catalog=True):
            assert await platform_file_read_source(db, agent_id=n.agent, invocation=invocation,
                content="A hidden working file") is None
