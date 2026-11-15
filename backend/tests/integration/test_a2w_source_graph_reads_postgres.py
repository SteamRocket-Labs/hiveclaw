"""Production-sized source checks retain exact reader and byte contracts."""

import hashlib
import json
import time
import uuid
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text

from app.database import tenant_scoped_session
from app.models.organization_identity import ContentInputReceipt
from tests.integration.test_a2r2_artifact_graph_reads_postgres import _production_shape
from tests.integration.test_r15e_artifact_graph_reads_postgres import BODY, _profile, _read, graph, native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

EXPECTED = {"session_tool_invocation": 260, "workspace_artifact": 66,
    "agent_directory_observation": 38, "session_command": 37, "memory_resource_reference": 28,
    "session_actor_identity": 27, "agent_platform_skill_file": 9, "explicit_memory": 7,
    "agent_context_resource": 2, "agent_identity": 1, "owner_action_policy": 1,
    "runtime_result": 1, "runtime_result_page": 1, "runtime_team_context": 1, "agent_resident_memory": 1}


@pytest.fixture
async def source_graph(graph, owner_sessionmaker):  # noqa: F811
    """Remove completed colleague fixtures before another test's global sweep."""
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.invocation_span import InvocationSpan
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome

    graph.colleague_fixture_runs = []
    try:
        yield graph
    finally:
        async with owner_sessionmaker() as db:
            for run, session in reversed(graph.colleague_fixture_runs):
                # Native final results and transcript rows hold non-cascading
                # references; remove those exact fixture rows in FK order.
                for model in (SessionRunOutcome, SessionModelResult, ChatTranscriptEvent):
                    await db.execute(delete(model).where(
                        model.tenant_id == graph.tenant, model.session_id == session))
                await db.execute(delete(ChatSession).where(
                    ChatSession.tenant_id == graph.tenant, ChatSession.id == session))
                await db.execute(delete(InvocationSpan).where(
                    InvocationSpan.tenant_id == graph.tenant, InvocationSpan.runtime_task_id == run))
                await db.execute(delete(RuntimeTask).where(
                    RuntimeTask.tenant_id == graph.tenant, RuntimeTask.id == run))
            await db.commit()


def _dependency(row):
    return {"source_kind": row.carrier_kind, "source_id": str(row.id), "source_version": row.content_sha256}


async def _closure(n, owner, sources):
    from app.services.content_input_receipts import _preload_execution_sources

    sql = next(value for value in _preload_execution_sources.__code__.co_consts
        if isinstance(value, str) and "WITH RECURSIVE sources" in value)
    async with owner() as db:
        return list(await db.execute(text(sql), {"tenant_id": n.tenant, "dependencies": json.dumps(sources)}))


async def _delegated_result(n, owner, app_factory, sources, *, title):
    """Use native model commit and canonical result encoding, without a provider."""
    from app.kernel.engine import _context_message
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_result import RuntimeResultIntegrationPage, RuntimeResultObject
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.runtime_result_store import (
        RuntimeResultDescriptor, build_runtime_result_integration_pages, encode_runtime_result_payload,
        runtime_result_object_id, runtime_result_ref,
    )
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.direct_invocation_terminal_boundary_processor import _build_direct_terminal_spec

    run, session, child = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    n.colleague_fixture_runs.append((run, session))
    turn = f"turn-{run.hex}"
    n.result_epoch = getattr(n, "result_epoch", 0) + 1
    async with owner() as db:
        db.add(Agent(id=child, tenant_id=n.tenant, creator_id=n.user, owner_user_id=n.user, name="Sales colleague"))
        await db.flush()
        db.add(RuntimeTask(id=run, tenant_id=n.tenant, task_type="delegation", status="running",
            parent_agent_id=n.agent, child_agent_id=child, parent_session_id=str(n.session),
            child_session_id=str(session), root_user_id=n.user, root_session_id=str(n.session),
            root_runtime_task_id=n.run, prompt=title, metadata_json={"turn_id": turn}))
        await db.flush()
        db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=child, user_id=n.user,
            audience_kind="private", source_channel="agent", session_kind="delegation_run",
            runtime_task_id=run, peer_agent_id=n.agent, parent_session_id=n.session))
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_factory) as db:
        # Call the production function saved by the graph fixture so this
        # child's inventory is exactly the supplied historical sources.
        request = await n.prepare_model_request(db, tenant_id=n.tenant, agent_id=child, session_id=session,
            run_id=run, turn_id=turn, round_index=1, messages=[_context_message(role="system",
                kind="dynamic_prompt", content=title, sources=sources, unbound=())], tools=[],
            provider="synthetic", model="local-reference")
        await seal_model_response(db, tenant_id=n.tenant, agent_id=child, session_id=session,
            run_id=run, turn_id=turn, round_index=1, provider_request_id=request,
            response={"content": title, "tool_calls": [], "finish_reason": "stop"})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=child, session_id=session,
            run_id=run, turn_id=turn, round_index=1, provider_request_id=request)
        task = await db.get(RuntimeTask, run)
        task.status, task.result_summary = "completed", title
        task.metadata_json = {"turn_id": turn, "terminal_reason": "turn_stop", "response_complete_payload": {
            "agent_id": str(child), "session_id": str(session), "source": "agent", "messages": [],
            "metadata": {"tenant_id": str(n.tenant), "final_response": title}}}
        await db.flush()
        await _build_direct_terminal_spec(db, task)
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run))).one()
        key = lambda source: tuple(source[field] for field in ("source_kind", "source_id", "source_version"))
        assert sorted(model.model_request_snapshot_json["input_bindings"]["dependencies"], key=key) == sorted(sources, key=key)
        encoded = encode_runtime_result_payload(summary=title)
        result_id = runtime_result_object_id(tenant_id=n.tenant, source_kind="a2a_delegation",
            source_run_id=str(run), sha256=encoded.sha256)
        ref = runtime_result_ref(result_id=result_id, sha256=encoded.sha256)
        db.add(RuntimeResultObject(id=result_id, tenant_id=n.tenant, source_kind="a2a_delegation",
            source_run_id=str(run), payload_bytes=encoded.payload_bytes, sha256=encoded.sha256,
            size_bytes=encoded.size_bytes))
        descriptor = RuntimeResultDescriptor(outbox_id=uuid.uuid4(), mailbox_sequence=n.result_epoch,
            source_kind="a2a_delegation", source_run_id=str(run), task_type="delegation", terminal_status="completed",
            child_session_id=session, child_agent_name="Sales colleague", result_ref=ref,
            result_sha256=encoded.sha256, result_size_bytes=encoded.size_bytes)
        manifest = build_runtime_result_integration_pages([descriptor], page_item_limit=25,
            starting_epoch=n.result_epoch, root_runtime_task_id=n.run, coverage={"complete": True})[0].to_manifest()
        page = RuntimeResultIntegrationPage(id=uuid.uuid4(), tenant_id=n.tenant, parent_agent_id=n.agent,
            parent_session_id=n.session, parent_user_id=n.user, root_runtime_task_id=n.run,
            root_scope_key=str(n.run), integration_epoch=n.result_epoch, delivery_mode="parent_continuation",
            mailbox_sequence_start=n.result_epoch, mailbox_sequence_end=n.result_epoch, item_count=1, manifest_json=manifest,
            manifest_sha256=manifest["manifest_sha256"], status="delivered")
        db.add(page)
        await db.commit()
    return SimpleNamespace(ref=ref, run=run, child=child, session=session, model=model.id,
        result_id=result_id, content=encoded.payload_bytes.decode(),
        result={"source_kind": "runtime_result", "source_id": f"{n.agent}\n{n.session}\n{result_id}",
            "source_version": encoded.sha256},
        page={"source_kind": "runtime_result_page", "source_id": str(page.id), "source_version": page.manifest_sha256})


async def _run20_shape(n, owner, app_factory, *, scale=1):
    from app.models.invocation_span import InvocationSpan
    from app.services.session_model_inputs import _hash

    # Double observation volume and retained versions, retaining the same
    # maximum lineage depth and 57 current paths. This models two histories
    # sharing one assistant's capability and configuration singletons.
    await _production_shape(n, owner, app_factory, workspace_nodes=66 * scale, memory_nodes=7 * scale,
        directory_nodes=38 * scale, actor_nodes=27 * scale, tool_rounds=37 * scale, directory_tools=5,
        root_dependencies=72 * scale, workspace_paths=57, workspace_lineages=scale,
        memory_reference_nodes=28 * scale if scale > 1 else None)
    # The previous result retains the same historical graph, excluding the
    # future root receipt. Recursive result reads must do real upstream work.
    historical = [source for source in n.receipts[-1].dependencies_json if source["source_kind"] != "explicit_memory"]
    returns = []
    for index in range(scale):
        simple = await _delegated_result(n, owner, app_factory, historical,
            title="Earlier retained sales result" if index == 0 else f"Earlier retained sales result {index}")
        proof = {"schema": "hive.runtime_team_publication_observation.v1", "content": "Sales colleague is processing.",
        "sources": [], "pending_task_ids": [str(simple.run)], "run_id": str(n.run),
        "agent_id": str(n.agent), "session_id": str(n.session), "user_id": str(n.user)}
        version = _hash(proof)
        identity = uuid.uuid5(n.run, f"runtime-team-publication:{version}")
        async with owner() as db:
            db.add(InvocationSpan(id=identity, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            runtime_task_id=n.run, session_id=str(n.session), trace_id=f"runtime-team:{n.run}",
            span_id=f"runtime-team:{identity}", span_type="runtime_team_context", name="team_publication_observed",
            status="captured", duration_ms=0, started_at=datetime.now(timezone.utc), metadata_json={"observation": proof}))
            await db.commit()
        returns += [simple.result, simple.page,
            {"source_kind": "runtime_team_context", "source_id": f"{n.agent}\n{n.session}\n{identity}",
                "source_version": version}]
    async with owner() as db:
        root = await db.get(ContentInputReceipt, n.receipts[-1].id)
        root.dependencies_json = [*root.dependencies_json, *returns]
        await db.commit()
        n.receipts[-1] = root
        sizes = (await db.execute(text("""SELECT
            (SELECT max(pg_column_size(model_request_snapshot_json)) FROM session_model_results WHERE tenant_id=:tenant),
            (SELECT max(octet_length(model_request_snapshot_json::text)) FROM session_model_results WHERE tenant_id=:tenant),
            (SELECT max(pg_column_size(r)) FROM content_input_receipts r WHERE tenant_id=:tenant)
        """), {"tenant": n.tenant})).one()
        print("A2W_ROW_SIZES", json.dumps({"model_stored_bytes": sizes[0], "model_json_bytes": sizes[1],
            "receipt_stored_bytes": sizes[2]}), flush=True)
        assert sizes[0] >= 268_000 and sizes[1] >= 300_000
    root_sources = [_dependency(n.receipts[-1])]
    closure = await _closure(n, owner, root_sources)
    counts = Counter(kind for kind, _ in closure)
    print("A2W_SHAPE", json.dumps({"scale": scale, "nodes": len(closure), "kinds": counts,
        "direct_dependencies": len(n.receipts[-1].dependencies_json)}), flush=True)
    assert set(counts) == set(EXPECTED), counts
    for kind, count in EXPECTED.items():
        if kind not in {"agent_platform_skill_file", "agent_context_resource", "agent_identity", "owner_action_policy",
            "agent_resident_memory"}:
            count *= scale
        assert .9 * count <= counts[kind] <= 1.1 * count, (kind, counts)
    return root_sources, closure


def _request(n, name, arguments, paths=None):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope

    paths = frozenset(paths or [n.path])
    scope = WorkspaceAuthorityScope(agent_id=n.agent, user_id=n.user, root_session_id=n.session,
        allowed_paths=paths, full_control_paths=paths, operator_view=False, authority_source="fixture")
    return ToolExecutionRequest(tool_name=name, arguments=arguments, context=ToolExecutionContext(
        agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant), session_id=str(n.session),
        runtime_task_id=str(n.run), workspace=n.root / "workspace", workspace_authority_scope=scope))


async def _measure(name, operation, monkeypatch, delay):
    started = time.perf_counter()
    with _profile(monkeypatch, delay=delay) as totals:
        result = await operation()
    row = {"path": name, "statements": sum(value["statements"] for value in totals.values()),
        "injected_seconds": round(sum(value["injected_seconds"] for value in totals.values()), 6),
        "seconds": round(time.perf_counter() - started, 4), "rtt": delay,
        "by_function_kind": [{"function": function, "kind": kind, **value}
            for (function, kind), value in sorted(totals.items())]}
    print("A2W_MEASURE", json.dumps(row), flush=True)
    return result, row


async def _search_shape(n, owner):
    from app.memory.explicit_overlay import write_explicit_memory_overlay, load_explicit_overlay_entries

    # Preserve historical carrier rows in the artifact graph. New revisions
    # of the original two entries and six new entries make eight active
    # memories, each retaining the report and seventeen directory sources.
    for index in range(6):
        saved = await write_explicit_memory_overlay(n.agent, category="project",
            content=f"Sales reporting convention {index}: retain the authorized reporting period.",
            tenant_id=n.tenant, data_root=n.root.parent)
        assert saved.status == "active", saved
    entries = load_explicit_overlay_entries(n.root.parent, n.agent)
    assert len(entries) == 8
    n.search_memory_sources = []
    async with owner() as db:
        memories = list(await db.scalars(select(ContentInputReceipt).where(
            ContentInputReceipt.tenant_id == n.tenant, ContentInputReceipt.carrier_kind == "explicit_memory")))
        template = deepcopy(memories[0].authority_json)
        # Eighteen distinct directory observations share retained immutable
        # evidence, but each still has its own live reader and roster checks.
        sources = [_dependency(n.receipts[-1]), *[source for row in n.receipts for source in row.dependencies_json
            if source["source_kind"] == "agent_directory_observation"][:17]]
        assert len({source["source_id"] for source in sources}) == 18
        for entry in entries:
            ref = f"{n.agent}\n{entry.entry_id}"
            previous = next((row for row in memories if row.carrier_ref == ref), None)
            row = ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="explicit_memory",
                carrier_ref=ref, revision=previous.revision + 1 if previous else 1, content_sha256=entry.source_sha256)
            db.add(row)
            row.authority_json = {**template, "metadata": entry.metadata}
            row.dependencies_json = sources
            n.model_memory = _dependency(row)
            n.search_memory_sources.append(n.model_memory)
        await db.commit()
    return entries


async def _model_prepare(n, app_factory, monkeypatch, delay):
    from app.kernel.engine import _context_message
    from app.services import session_model_round

    timings = []
    original = session_model_round._validate_or_withhold_model_sources

    async def measured(*args, **kwargs):
        value, row = await _measure("prepare_model_request_sources", lambda: original(*args, **kwargs), monkeypatch, delay)
        bindings = kwargs["input_bindings"]
        assert bindings["status"] == "resolved" and bindings["dependency_coverage"]["complete"]
        assert not any(entry.get("status") == "withheld" for entry in bindings["messages"])
        timings.append(row)
        return value

    run = n.run
    round_index = n.memory_rounds[str(run)] + 1
    with monkeypatch.context() as patch:
        patch.setattr(session_model_round, "_validate_or_withhold_model_sources", measured)
        async with tenant_scoped_session(n.tenant, session_factory=app_factory) as db:
            await n.prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                run_id=run, turn_id=f"turn-{run.hex}", round_index=round_index, messages=[_context_message(role="system",
                    kind="dynamic_prompt", content="Prepare sales analysis", sources=[n.model_memory],
                    unbound=())], tools=[], provider="synthetic", model="local-reference")
            await db.rollback()
    assert len(timings) == 1
    return timings[0]


async def test_runtime_fixture_uses_canonical_native_result(source_graph, owner_sessionmaker, app_user_sessionmaker):
    from app.services.runtime_result_sources import load_canonical_result_sources

    n = source_graph
    result = await _delegated_result(n, owner_sessionmaker, app_user_sessionmaker, [], title="Canonical colleague result")
    from app.models.runtime_result import RuntimeResultObject

    async with owner_sessionmaker() as db:
        bound = await load_canonical_result_sources(db, await db.get(RuntimeResultObject, result.result_id))
        assert bound and bound[0].id == result.run and bound[1] == []


async def test_nested_catalog_refreshes_current_agent_owner(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.models.agent import Agent
    from app.services.content_input_receipts import group_source_read, _input_row

    n = native_workspace
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        with group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True):
            await _input_row(db, Agent, n.agent)
            async with owner_sessionmaker() as writer:
                (await writer.get(Agent, n.agent)).owner_user_id = n.foreign
                await writer.commit()
            with group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True):
                current = await _input_row(db, Agent, n.agent)
                assert current.owner_user_id == n.foreign




async def test_catalogue_preserves_existing_large_transcript_read_columns(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services.content_input_receipts import group_source_read, _input_row, _preload_execution_sources
    from app.services.session_model_inputs import _hash
    from app.services.session_semantic_history import _event_payload
    from app.services.source_read_queries import transcript_event_columns
    from app.tools.output_limits import TOOL_OUTPUT_BYTES, TOOL_OUTPUT_TRUNCATION
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n = native_workspace
    path = "workspace/transcript-projection.txt"
    (n.root / "workspace").mkdir(exist_ok=True)
    (n.root / path).write_text("Projection source")
    _content, invocation = await memory_call(n, app_user_sessionmaker, "read_file", {"path": path})
    large = "甲" * (TOOL_OUTPUT_BYTES // 3 + 20)
    async with owner_sessionmaker() as db:
        current = await db.get(SessionToolInvocation, invocation.id)
        result_event_id = current.result_event_id
        assert result_event_id is not None
        event = await db.get(ChatTranscriptEvent, result_event_id)
        payload = {**_event_payload(event), "content": large, "content_hash": _hash(large)}
        event.content = large
        event.metadata_json = {**event.metadata_json, "v2_payload": payload}
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        expected = _event_payload(await db.get(ChatTranscriptEvent, result_event_id))
        assert expected["content"].endswith(TOOL_OUTPUT_TRUNCATION)
        assert expected["content_hash"] != _hash(expected["content"])
        dependency = {"source_kind": "session_tool_invocation", "source_id": str(invocation.id),
            "source_version": _hash(large)}
        with group_source_read(batch_inputs=True, source_catalog=True):
            await _preload_execution_sources(db, n.tenant, [dependency])
            assert _event_payload(await _input_row(db, ChatTranscriptEvent, result_event_id)) == payload
        with group_source_read(batch_inputs=True, source_catalog=True) as frame:
            transcript_event_columns(frame)
            await _preload_execution_sources(db, n.tenant, [dependency])
            assert _event_payload(await _input_row(db, ChatTranscriptEvent, result_event_id)) == expected


@pytest.mark.parametrize("runtime", [False, True])
async def test_catalogue_preserves_legacy_top_level_tool_payload(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, runtime,  # noqa: F811
):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.models.user import User
    from app.services.content_input_receipts import (
        group_source_read, _preload_execution_sources, _validate_input_dependencies,
    )
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.session_model_inputs import tool_result_source_hash
    from app.services.session_semantic_history import _event_payload
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n = native_workspace
    path = "workspace/legacy-tool.txt"
    (n.root / "workspace").mkdir(exist_ok=True)
    (n.root / path).write_text("Legacy tool source")
    _content, invocation = await memory_call(n, app_user_sessionmaker, "read_file", {"path": path})
    async with owner_sessionmaker() as db:
        current = await db.get(SessionToolInvocation, invocation.id)
        event = await db.get(ChatTranscriptEvent, current.result_event_id)
        payload = _event_payload(event)
        event.schema_version = 1
        event.metadata_json = {**payload}
        await db.commit()
    source = {"source_kind": "session_tool_invocation", "source_id": str(invocation.id),
        "source_version": tool_result_source_hash(payload)}
    principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), purpose="interactive_session")
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        context = {"user": None if runtime else user, "runtime_request": principal if runtime else None}
        await _validate_input_dependencies(db, n.tenant, [source], **context)
        with group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True):
            await _preload_execution_sources(db, n.tenant, [source])
            await _validate_input_dependencies(db, n.tenant, [source], **context)


async def test_catalogue_defers_malformed_nested_source_until_its_ordered_check(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.models.agent import Agent
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.models.user import User
    from app.services.agent_context import agent_identity_content
    from app.services.config_versioning import canonical_content_hash
    from app.services.content_input_receipts import (
        ContentInputReceiptError, group_source_read, _preload_execution_sources, _validate_input_dependencies,
    )
    from app.services.session_model_inputs import tool_result_source_hash
    from app.services.session_semantic_history import _event_payload
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n = native_workspace
    path = "workspace/ordered-source.txt"
    (n.root / "workspace").mkdir(exist_ok=True)
    (n.root / path).write_text("Ordered source")
    _content, invocation = await memory_call(n, app_user_sessionmaker, "read_file", {"path": path})
    async with owner_sessionmaker() as db:
        current = await db.get(SessionToolInvocation, invocation.id)
        event = await db.get(ChatTranscriptEvent, current.result_event_id)
        later = {"source_kind": "session_tool_invocation", "source_id": str(invocation.id),
            "source_version": tool_result_source_hash(_event_payload(event))}
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.session_id == current.session_id, SessionModelResult.run_id == current.run_id,
            SessionModelResult.provider_request_id == current.provider_request_id))
        snapshot = deepcopy(model.model_request_snapshot_json)
        snapshot["input_bindings"]["dependencies"].append(
            {"source_kind": "agent_identity", "source_id": None, "source_version": "0" * 64})
        model.model_request_snapshot_json = snapshot
        agent = await db.get(Agent, n.agent)
        first = {"source_kind": "agent_identity", "source_id": f"{n.agent}\nidentity",
            "source_version": canonical_content_hash(agent_identity_content(agent.name, agent.role_description))}
        agent.deactivated_at = datetime.now(timezone.utc)
        await db.commit()
    sources = [first, later]
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        with pytest.raises(ContentInputReceiptError) as original:
            await _validate_input_dependencies(db, n.tenant, sources, user=user)
        assert (original.value.status_code, original.value.code) == (403, "content_source_access_denied")
        with group_source_read(batch_inputs=True, cache_authority=False, source_catalog=True):
            await _preload_execution_sources(db, n.tenant, sources)
            with pytest.raises(ContentInputReceiptError) as batched:
                await _validate_input_dependencies(db, n.tenant, sources, user=user)
        assert (batched.value.status_code, batched.value.code, str(batched.value)) == (
            original.value.status_code, original.value.code, str(original.value))


@pytest.fixture(scope="module")
def scaling_counts():
    return {}


@pytest.mark.parametrize(("scale", "volume"), [pytest.param(1, 0, id="1"), pytest.param(2, 0, id="2"),
    pytest.param(1, 1, id="1-lab"), pytest.param(2, 1, id="2-lab"),
    pytest.param(1, 5, id="1-lab5"), pytest.param(2, 5, id="2-lab5")])
async def test_run20_source_paths(source_graph, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
    scaling_counts, scale, volume, tmp_path):
    from app.services import content_input_receipts as receipts
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.tools.handlers.context_resources import read_runtime_result
    from app.tools.handlers.memory import search_memory

    print("A2W_CASE", json.dumps({"scale": scale, "volume": volume}), flush=True)
    n = source_graph
    monkeypatch.setattr("app.tools.handlers.context_resources.async_session", app_user_sessionmaker)
    roots, closure = await _run20_shape(n, owner_sessionmaker, app_user_sessionmaker, scale=scale)
    entries = await _search_shape(n, owner_sessionmaker)
    # A second colleague's final carries this graph and distinct actor
    # observations. Its closure is the measured 524-node return workload.
    from app.models.user import User
    from app.services.content_input_receipts import record_session_actor_identity

    added = []
    async with tenant_scoped_session(n.tenant, session_factory=owner_sessionmaker) as db:
        actor = await db.get(User, n.user)
        for index in range(524 * scale - len(closure) - 1):
            actor.display_name = f"Sales owner historical name {index}"
            await db.flush()
            source, _ = await record_session_actor_identity(db, tenant_id=n.tenant, agent_id=n.agent,
                session_id=n.session, user_id=n.user, display_name=actor.display_name, runtime_task_id=n.run)
            added.append(source)
        await db.commit()
    large = await _delegated_result(n, owner_sessionmaker, app_user_sessionmaker, [*roots, *added],
        title="Colleague sales result after checking all reporting inputs.")
    large_sources = [{"source_kind": "session_model_result", "source_id": str(large.model)}]
    large_closure = await _closure(n, owner_sessionmaker, large_sources)
    print("A2W_RESULT_SHAPE", json.dumps({"scale": scale, "nodes": len(large_closure)}), flush=True)
    assert 524 * scale * .9 <= len(large_closure) <= 524 * scale * 1.1
    paths = sorted({row.carrier_ref.split("\n", 1)[1] for row in n.receipts})
    assert len(paths) == 57
    memory_closure = await _closure(n, owner_sessionmaker, n.search_memory_sources)
    model_closure = await _closure(n, owner_sessionmaker, [n.model_memory])
    if volume:
        from tests.integration._a2w_volume import add_unrelated_history

        await add_unrelated_history(n, owner_sessionmaker, volume)
        assert set(await _closure(n, owner_sessionmaker, n.search_memory_sources)) == set(memory_closure)
        assert set(await _closure(n, owner_sessionmaker, roots)) == set(closure)
    # Count semantic live checkpoints from the fixture graph, independently
    # of the measured query implementation. C=40 allows ten immutable kind
    # loaders, fifteen endpoint/tenant ingress reads and fifteen fixed
    # witness/renderer reads. Manifest/latest/reader is one joined checkpoint;
    # directory scope/roster, memory before/after and skill access/package
    # remain independent. Runtime results additionally check publication and
    # their canonical terminal binding; no phase can reuse another's facts.
    points = {kind: 1 for kind in EXPECTED}
    points.update(agent_directory_observation=2, explicit_memory=2, agent_platform_skill_file=2,
        session_model_result=2, runtime_result=5)

    def live_points(sources):
        return sum(points[kind] for kind, _ in sources) + 2 * sum(kind == "agent_context_resource"
            and raw.split("\n", 1)[-1] == "channels" for kind, raw in sources)

    budgets = {path: live_points(closure) for path in ("download", "content", "preview", "read_file_member", "read_file_runtime")}
    budgets.update(execution_57_files=live_points(closure) + 2 * len(paths),
        search_memory_8_entries=2 * live_points(memory_closure), read_runtime_result_524=live_points(large_closure),
        prepare_model_request_sources=live_points(model_closure))
    measurements = []
    # Compare exact rendered bytes with independent phase-scoped reads, including
    # the visible legacy recall count; denying every entry must not look faster.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def independent_phases(context):
        with receipts.group_source_read(source_catalog=True):
            yield

    search = _request(n, "search_memory", {"query": "sales", "scope": "all"})
    with monkeypatch.context() as patch:
        patch.setattr(receipts, "memory_read_call", independent_phases)
        search_output = str(await search_memory(n.agent, search.arguments, str(n.tenant),
            execution_context=search.context))
    for delay in (0, .006):
        for endpoint in ("download", "content", "preview"):
            response, row = await _read(n, endpoint, monkeypatch, delay=delay)
            row["path"] = endpoint
            measurements.append(row)
            assert response.status_code == (400 if endpoint == "preview" else 200), response.text
            if endpoint != "preview":
                assert (response.content if endpoint == "download" else response.json()["content"].encode()) == BODY.encode()
        request = _request(n, "read_file", {"path": n.path})
        observed, row = await _measure("read_file_member", lambda: receipts.authorize_workspace_receipt_read(request),
            monkeypatch, delay)
        measurements.append(row)
        assert observed.receipt_id == n.receipts[-1].id, observed
        # The private tool path publishes to its member. This second call
        # starts with the explicit runtime principal and still uses the same
        # real authorization entry; the output recipient remains unchanged.
        runtime = _request(n, "read_file", {"path": n.path})
        principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), purpose="interactive_session")
        original_actor = receipts._runtime_input_actor

        async def runtime_actor(db, tenant_id, request):
            return await original_actor(db, tenant_id, principal if request is runtime else request)

        with monkeypatch.context() as patch:
            patch.setattr(receipts, "_runtime_input_actor", runtime_actor)
            observed, row = await _measure("read_file_runtime", lambda: receipts.authorize_workspace_receipt_read(runtime),
                monkeypatch, delay)
        measurements.append(row)
        assert observed.receipt_id == n.receipts[-1].id, observed
        execution = _request(n, "execute_code", {"language": "python", "code": "print('sales')"}, paths)
        result, row = await _measure("execution_57_files", lambda: receipts.authorize_workspace_execution_inputs(execution),
            monkeypatch, delay)
        measurements.append(row)
        assert result is None, result
        assert len(execution.context.workspace_authority_scope.input_reads) == 57
        search = _request(n, "search_memory", {"query": "sales", "scope": "all"})
        result, row = await _measure("search_memory_8_entries", lambda: search_memory(n.agent,
            search.arguments, str(n.tenant), execution_context=search.context), monkeypatch, delay)
        measurements.append(row)
        assert str(result) == search_output
        assert "Sales reporting convention" in str(result), result
        read = _request(n, "read_runtime_result", {"result_ref": large.ref})
        result, row = await _measure("read_runtime_result_524", lambda: read_runtime_result(read), monkeypatch, delay)
        measurements.append(row)
        payload = json.loads(str(result))
        assert payload["status"] == "ok" and payload["content"] == large.content, payload
        measurements.append(await _model_prepare(n, app_user_sessionmaker, monkeypatch, delay))
    violations = []
    # These constants include ingress and each independent live checkpoint.
    # Recall keeps the admission, selection and emitted-result judges, using
    # a fresh vector before and after the call. Retained evidence loads once.
    constants = {"download": 48, "content": 48, "preview": 51, "read_file_member": 44,
        "read_file_runtime": 44, "execution_57_files": 48, "search_memory_8_entries": 160,
        "read_runtime_result_524": 60, "prepare_model_request_sources": 80}
    counts = scaling_counts.setdefault(volume, {})
    counts[scale] = {row["path"]: row["statements"] for row in measurements
        if not row.get("rtt", row.get("injected_rtt"))}
    if scale == 2:
        assert 1 in counts, "Run both graph scales to verify constant statement work."
        print("A2W_SCALING", json.dumps({"volume": volume, "counts": counts}), flush=True)
    for row in measurements:
        bound = 5 if row["path"] in {"execution_57_files", "read_runtime_result_524"} else 3
        value = {key: row[key] for key in ("path", "statements", "seconds")}
        value.update(scale=scale, volume=volume, live_checkpoints=budgets[row["path"]], C=40, time_bound=bound,
            constant=constants[row["path"]])
        print("A2W_BOUND", json.dumps(value), flush=True)
        if row["statements"] > min(budgets[row["path"]] + 40, constants[row["path"]]) or (
            row.get("rtt", row.get("injected_rtt")) and row["seconds"] > bound):
            violations.append(value)
    from pathlib import Path
    import os
    import sys
    from tests.integration._a2w_volume import measure_catalogue

    directory = Path(os.environ["FULL_RUN"]) / "evidence" / f"{sys.argv[1]}-g0" if os.environ.get(
        "FULL_RUN") else tmp_path / "g0"
    await measure_catalogue(n, app_user_sessionmaker, monkeypatch, directory, scale=scale, volume=volume)
    if scale == 2 and volume == 0 and any(value["path"] == "search_memory_8_entries" for value in violations):
        # Diagnose a failed CPU bound separately; profiling never contributes
        # overhead to the measured local/6 ms samples or their assertions.
        import cProfile
        import pstats

        profile = cProfile.Profile()
        profile.enable()
        try:
            profiled = await search_memory(n.agent, search.arguments, str(n.tenant), execution_context=search.context)
        finally:
            profile.disable()
        assert str(profiled) == search_output
        output = tmp_path / "a2w-doubled-search.prof"
        profile.dump_stats(output)
        stats = pstats.Stats(profile)
        rows = sorted(({"function": f"{file}:{line}:{name}", "calls": calls, "cpu_seconds": total,
            "cumulative_seconds": cumulative} for (file, line, name), (_primitive, calls, total, cumulative, _callers)
            in stats.stats.items()), key=lambda row: row["cumulative_seconds"], reverse=True)[:30]
        print("A2W_CPU_PROFILE", json.dumps({"path": str(output), "functions": rows}), flush=True)
    if scale == 2:
        # Report all path/CPU measurements before the unchanged strict gate.
        assert counts[2] == counts[1], counts
    assert not violations, violations
