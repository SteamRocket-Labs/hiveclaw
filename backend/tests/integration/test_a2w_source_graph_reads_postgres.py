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


async def _run20_shape(n, owner, app_factory):
    from app.models.invocation_span import InvocationSpan
    from app.services.session_model_inputs import _hash

    await _production_shape(n, owner, app_factory, workspace_nodes=66, memory_nodes=7,
        directory_nodes=38, actor_nodes=27, tool_rounds=37, directory_tools=5, root_dependencies=72,
        workspace_paths=57)
    # The previous result retains the same historical graph, excluding the
    # future root receipt. Recursive result reads must do real upstream work.
    historical = [source for source in n.receipts[-1].dependencies_json if source["source_kind"] != "explicit_memory"]
    simple = await _delegated_result(n, owner, app_factory, historical, title="Earlier retained sales result")
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
        root = await db.get(ContentInputReceipt, n.receipts[-1].id)
        root.dependencies_json = [*root.dependencies_json, simple.result, simple.page,
            {"source_kind": "runtime_team_context", "source_id": f"{n.agent}\n{n.session}\n{identity}",
                "source_version": version}]
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
    print("A2W_SHAPE", json.dumps({"nodes": len(closure), "kinds": counts,
        "direct_dependencies": len(n.receipts[-1].dependencies_json)}), flush=True)
    assert set(counts) == set(EXPECTED), counts
    for kind, count in EXPECTED.items():
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




async def test_run20_source_paths(source_graph, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.services import content_input_receipts as receipts
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.tools.handlers.context_resources import read_runtime_result
    from app.tools.handlers.memory import search_memory

    n = source_graph
    monkeypatch.setattr("app.tools.handlers.context_resources.async_session", app_user_sessionmaker)
    roots, closure = await _run20_shape(n, owner_sessionmaker, app_user_sessionmaker)
    entries = await _search_shape(n, owner_sessionmaker)
    # A second colleague's final carries this graph and distinct actor
    # observations. Its closure is the measured 524-node return workload.
    from app.models.user import User
    from app.services.content_input_receipts import record_session_actor_identity

    added = []
    async with tenant_scoped_session(n.tenant, session_factory=owner_sessionmaker) as db:
        actor = await db.get(User, n.user)
        for index in range(524 - len(closure) - 1):
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
    print("A2W_RESULT_SHAPE", json.dumps({"nodes": len(large_closure)}), flush=True)
    assert 524 * .9 <= len(large_closure) <= 524 * 1.1
    paths = sorted({row.carrier_ref.split("\n", 1)[1] for row in n.receipts})
    assert len(paths) == 57
    memory_closure = await _closure(n, owner_sessionmaker, n.search_memory_sources)
    model_closure = await _closure(n, owner_sessionmaker, [n.model_memory])
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
    search_output = None
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
        if search_output is None:
            search_output = str(result)
        assert str(result) == search_output
        assert "Sales reporting convention" in str(result), result
        read = _request(n, "read_runtime_result", {"result_ref": large.ref})
        result, row = await _measure("read_runtime_result_524", lambda: read_runtime_result(read), monkeypatch, delay)
        measurements.append(row)
        payload = json.loads(str(result))
        assert payload["status"] == "ok" and payload["content"] == large.content, payload
        measurements.append(await _model_prepare(n, app_user_sessionmaker, monkeypatch, delay))
    violations = []
    for row in measurements:
        bound = 5 if row["path"] in {"execution_57_files", "read_runtime_result_524"} else 3
        value = {key: row[key] for key in ("path", "statements", "seconds")}
        value.update(live_checkpoints=budgets[row["path"]], C=40, time_bound=bound)
        print("A2W_BOUND", json.dumps(value), flush=True)
        if row["statements"] > budgets[row["path"]] + 40 or (
            row.get("rtt", row.get("injected_rtt")) and row["seconds"] > bound):
            violations.append(value)
    assert not violations, violations
