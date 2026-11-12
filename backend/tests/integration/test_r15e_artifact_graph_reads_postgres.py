"""Delivered files batch the retained source graph without changing disclosure."""

import hashlib
import inspect
import json
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.database import tenant_scoped_session
from app.models.chat_artifact import ChatArtifact
from app.models.organization_identity import ContentInputReceipt
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
BODY = "# Delivered report\n" + "Sales total: 42.\n" * 400
_SQL_SOURCE = ContextVar("r15e_sql_source", default="request")


@contextmanager
def _profile(monkeypatch, *, delay=0):
    from app.services import content_input_receipts as receipts

    original = receipts._validate_input_dependency_batch

    async def attributed(db, tenant_id, dependencies, **kwargs):
        kind = dependencies[0]["source_kind"] if dependencies else _SQL_SOURCE.get()
        token = _SQL_SOURCE.set(kind)
        try:
            return await original(db, tenant_id, dependencies, **kwargs)
        finally:
            _SQL_SOURCE.reset(token)

    def caller():
        # SQLAlchemy's async bridge keeps the suspended application frames on
        # its parent greenlet. Inspect them only in this diagnostic test.
        from greenlet import getcurrent

        current = getcurrent()
        frame = inspect.currentframe()
        while current is not None:
            while frame is not None:
                if "/app/" in frame.f_code.co_filename:
                    return frame.f_code.co_name
                frame = frame.f_back
            current = current.parent
            frame = current.gr_frame if current is not None else None
        return "framework"

    totals = defaultdict(lambda: {"statements": 0, "sql_seconds": 0.0, "injected_seconds": 0.0})

    def before(_conn, _cursor, _statement, _parameters, context, _many):
        context.r15e = time.perf_counter(), caller(), _SQL_SOURCE.get()
        # sleep() on macOS may add a millisecond of timer coalescing to each
        # 2.35 ms RTT. Use a monotonic deadline so the test injects the stated
        # delay, and report the actual injected wall time as well.
        started = time.perf_counter()
        if delay:
            deadline = started + delay
            while time.perf_counter() < deadline:
                pass
        context.r15e_injected = time.perf_counter() - started if delay else 0

    def after(_conn, _cursor, _statement, _parameters, context, _many):
        started, function, kind = context.r15e
        row = totals[(function, kind)]
        row["statements"] += 1
        row["injected_seconds"] += context.r15e_injected
        row["sql_seconds"] += time.perf_counter() - started

    with monkeypatch.context() as patch:
        patch.setattr(receipts, "_validate_input_dependency_batch", attributed)
        event.listen(Engine, "before_cursor_execute", before)
        event.listen(Engine, "after_cursor_execute", after)
        try:
            yield totals
        finally:
            event.remove(Engine, "before_cursor_execute", before)
            event.remove(Engine, "after_cursor_execute", after)


async def _read(n, endpoint, monkeypatch, *, history=0, delay=0):
    url = (
        f"/agents/{n.agent}/office/artifacts/{n.artifact}/preview" if endpoint == "preview"
        else f"/agents/{n.agent}/files/content?path={n.path}" if endpoint == "workspace"
        else f"/agents/{n.agent}/files/artifacts/{n.artifact}/{endpoint}"
    )
    started = time.perf_counter()
    with _profile(monkeypatch, delay=delay) as totals:
        response = await n.client.get(url)
    row = {"nodes": len(n.receipts), "history": history, "endpoint": endpoint,
        "depth": n.depth, "injected_rtt": delay,
        "live_checkpoints": getattr(n, "live_checkpoints", None),
        "statements": sum(item["statements"] for item in totals.values()),
        "injected_seconds": round(sum(item["injected_seconds"] for item in totals.values()), 6),
        "seconds": round(time.perf_counter() - started, 4), "status": response.status_code,
        "by_function_kind": [{"function": function, "kind": kind,
            "statements": value["statements"], "sql_seconds": round(value["sql_seconds"], 6)}
            for (function, kind), value in sorted(totals.items())]}
    print("R15E_READ", json.dumps(row), flush=True)
    return response, row


@pytest.fixture
async def graph(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import files, office
    from app.memory.types import MemoryItem, MemoryKind
    from app.memory.write_gate import MemoryWriteDecision
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.user import User
    from app.services import agent_context, agent_manager, session_model_round
    from app.services.agent_context import agent_identity_content, load_agent_context_resource
    from app.services.agent_directory_sources import observe_directory
    from app.services.agent_manager import AgentManager
    from app.services.config_versioning import canonical_content_hash
    from app.services.content_input_receipts import record_session_actor_identity
    from app.services.memory_resource_references import _capture_reference
    from app.services.owner_action_policy import resolve_owner_action_policy_prompt_section
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.skill_source_receipts import _platform_skill_sources
    from app.services.skill_installation import install_active_skill_package
    from app.services.skill_seeder import builtin_skill_definitions

    n = native_workspace
    monkeypatch.setattr(files.settings, "AGENT_DATA_DIR", str(n.root.parent))
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(n.root.parent))
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", n.root.parent)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", n.root.parent)
    n.client._transport.app.include_router(office.router)
    n.target.unlink()
    n.target.parent.rmdir()
    n.root.rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, n.agent)
        await AgentManager().initialize_agent_files(db, agent, original_author_id=n.user)
        for capability in ("workspace.file.read", "agent.memory.write", "agent.memory.read"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        identity = {"source_kind": "agent_identity", "source_id": f"{n.agent}\nidentity",
            "source_version": canonical_content_hash(agent_identity_content(agent.name, agent.role_description))}
        await db.commit()
    for skill in builtin_skill_definitions():
        if skill.get("is_default"):
            install_active_skill_package(workspace=n.root, folder_name=skill["folder_name"], files=skill["files"],
                source="platform_init", overwrite=True)

    async def review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "controlled_reference_reviewer"})

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", review)
    for content in ("Sales reports retain the authorized reporting convention.", "Retain the oldest report source."):
        saved, _ = await memory_call(n, app_user_sessionmaker, "save_memory", {"category": "project", "content": content})
        assert "Saved memory" in saved, saved
    tools = []
    for _ in range(8):
        raw, invocation = await memory_call(n, app_user_sessionmaker, "get_current_time", {})
        from app.models.chat_transcript_event import ChatTranscriptEvent
        from app.models.session_v2 import SessionToolInvocation
        from app.services.session_model_inputs import tool_result_source_hash
        from app.services.session_semantic_history import _event_payload

        async with owner_sessionmaker() as db:
            invocation = await db.get(SessionToolInvocation, invocation.id)
            payload = _event_payload(await db.get(ChatTranscriptEvent, invocation.result_event_id))
            tools.append({"source_kind": "session_tool_invocation", "source_id": str(invocation.id),
                "source_version": tool_result_source_hash(payload)})
        assert raw
    principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.user, session_id=str(n.session),
        runtime_task_id=str(n.run), purpose="interactive_session")
    resource = f"memory/t2/sessions/{n.session}/segments/r15e"
    (n.root / resource).mkdir(parents=True)
    for filename in ("manifest.json", "summary.md"):
        (n.root / resource / filename).write_text("{}" if filename.endswith("json") else "Retained report index")
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        memories = list(await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == n.tenant,
            ContentInputReceipt.carrier_kind == "explicit_memory").order_by(ContentInputReceipt.id)))
        assert len(memories) == 2
        memory_sources = [{"source_kind": "explicit_memory", "source_id": str(memory.id),
            "source_version": memory.content_sha256} for memory in memories]
        n.memory = memory_sources[1]
        skills = await _platform_skill_sources(db, n.tenant, n.agent)
        skills = [{"source_kind": "agent_platform_skill_file", "source_id": f"{n.agent}\n{path}\n{rev}",
            "source_version": digest} for path, (rev, digest) in sorted(skills.items())][:10]
        assert len(skills) == 10
        actor, _ = await record_session_actor_identity(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, user_id=n.user, display_name=user.display_name, runtime_task_id=n.run)
        _, directory = await observe_directory(db, tenant_id=n.tenant, agent_id=n.agent, runtime_request=principal)
        reference = await _capture_reference(db, item=MemoryItem(kind=MemoryKind.EPISODIC, content="",
            source=resource, metadata={"session_id": str(n.session)}), data_root=n.root.parent,
            tenant_id=n.tenant, reader=principal, user=user)
        from app.models.session_v2 import SessionCommand

        n.commands = [{"source_kind": "session_command", "source_id": str(command.id),
            "source_version": command.request_hash} for command in await db.scalars(select(SessionCommand).where(
                SessionCommand.session_id == n.session, SessionCommand.namespace == "human_input"))]
        assert n.commands
        await db.commit()
    policy = []
    await resolve_owner_action_policy_prompt_section(agent_id=n.agent, tenant_id=n.tenant, source_refs=policy)
    assert len(policy) == 1
    channels = await load_agent_context_resource(agent_id=n.agent, tenant_id=n.tenant, resource_ref="channels")
    context = {"source_kind": "agent_context_resource", "source_id": f"{n.agent}\nchannels",
        "source_version": channels.sha256}
    resident = {"source_kind": "agent_resident_memory", "source_id": f"{n.agent}\nmemory/self/self.md",
        "source_version": hashlib.sha256((n.root / "memory/self/self.md").read_bytes()).hexdigest()}
    n.sources, n.receipts, n.artifacts, n.history = [], [], [], 0
    n.leaves = {"explicit_memory": memory_sources[0], "session_tool_invocation": tools[0],
        "agent_platform_skill_file": skills[0]}
    n.common = SimpleNamespace(skills=skills[1:], tools=tools[1:], context=context, directory=directory,
        reference=reference.source_refs[0], identity=identity, actor=actor, policy=policy[0], resident=resident)
    prepare = session_model_round.prepare_model_request

    async def expose_sources(db, **kwargs):
        from app.kernel.engine import _context_message

        prefix = _context_message(role="system", kind="dynamic_prompt", content=getattr(n, "production_context", "Prepare the authorized report."),
            sources=n.sources, unbound=())
        return await prepare(db, **{**kwargs, "messages": [prefix, *kwargs["messages"]]})

    monkeypatch.setattr(session_model_round, "prepare_model_request", expose_sources)
    yield n


async def _grow(n, size, owner_sessionmaker, app_user_sessionmaker):
    from app.memory.types import MemoryItem, MemoryKind
    from app.models.runtime_task import RuntimeTask
    from app.services.agent_directory_sources import observe_directory
    from app.services.explicit_memory_authority import latest_memory_receipt
    from app.services.memory_resource_references import _capture_reference
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.models.user import User

    if n.receipts:
        n.receipts, n.artifacts = [], []
        n.leaves.pop("workspace_artifact", None)
    for i in range(size):
        common = n.common
        # Different turns retain different directory/index observations and
        # private entries, as in the production graph. Shared-kind batching
        # must work for distinct IDs as well as repeated graph edges.
        async with owner_sessionmaker() as db:
            (await db.get(RuntimeTask, n.run)).status = "completed"
            n.run = uuid.uuid4()
            db.add(RuntimeTask(id=n.run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
                parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(n.session),
                child_session_id=str(n.session), root_session_id=str(n.session), root_user_id=n.user,
                root_runtime_task_id=n.run, prompt="Prepare the next report", metadata_json={"turn_id": f"turn-{n.run.hex}"}))
            await db.commit()
        if i >= 10:
            n.sources = []
            saved, _ = await memory_call(n, app_user_sessionmaker, "save_memory",
                {"category": "project", "content": f"Retain authorized reporting convention number {i}."})
            assert "Saved memory" in saved, saved
            entry = json.loads((n.root / "memory/explicit/manifest.jsonl").read_text().splitlines()[-1])
            async with owner_sessionmaker() as db:
                receipt = await latest_memory_receipt(db, n.tenant, n.agent, entry["id"])
                n.memory = {"source_kind": "explicit_memory", "source_id": str(receipt.id),
                    "source_version": receipt.content_sha256}
        principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.user, session_id=str(n.session),
            runtime_task_id=str(n.run), purpose="interactive_session")
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            if i >= 4:
                _, common.directory = await observe_directory(db, tenant_id=n.tenant, agent_id=n.agent, runtime_request=principal)
            if i >= 10:
                resource = f"memory/t2/sessions/{n.session}/segments/r15e-{i}"
                (n.root / resource).mkdir(parents=True)
                for name in ("manifest.json", "summary.md"):
                    (n.root / resource / name).write_text("{}" if name.endswith("json") else "Retained index")
                reference = await _capture_reference(db, item=MemoryItem(kind=MemoryKind.EPISODIC, content="",
                    source=resource, metadata={"session_id": str(n.session)}), data_root=n.root.parent,
                    tenant_id=n.tenant, reader=principal, user=await db.get(User, n.user))
                common.reference = reference.source_refs[0]
            await db.commit()
        if size == 8:
            ancestors = n.receipts[:]
        elif i:
            # Keep both graphs at depth seven. The wide graph has five nodes
            # in each intermediate layer and 205 edges, so depth cannot mask
            # statement growth in the number of dependencies.
            layer = 7 if i == 31 else (i - 1) // 5 + 1
            levels = [0 if index == 0 else (index - 1) // 5 + 1 for index in range(i)]
            previous = [row for row, level in zip(n.receipts, levels) if level == layer - 1]
            earlier = [row for row, level in zip(n.receipts, levels) if level < layer - 1]
            ancestors = previous + earlier[-(5 if i == 31 else 3):]
        else:
            ancestors = []
        n.sources = [{"source_kind": "workspace_artifact", "source_id": str(row.id),
            "source_version": row.content_sha256} for row in ancestors]
        if not i:
            n.sources += [*n.leaves.values(), common.context, common.directory, common.reference,
                common.identity, common.policy, common.actor, common.resident]
        else:
            n.sources += common.skills[:9 if i == 31 else 6] + common.tools[:7 if i == 31 else 6]
        if i >= 4:
            n.sources += [n.commands[i % len(n.commands)]]
        for start, sources in ((2, [common.context]), (4, [common.directory]),
            (10, [n.memory, common.reference]), (11, [common.identity, common.policy, common.actor]),
            (27, [common.resident])):
            if i >= start:
                n.sources += sources
        n.path = f"workspace/r15e-{size}-report-{i}.md"
        output, _ = await memory_call(n, app_user_sessionmaker, "write_file", {"path": n.path, "content": BODY})
        async with owner_sessionmaker() as db:
            artifact = (await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run,
                ChatArtifact.path == n.path))).one()
            assert artifact.snapshot_json["snapshot_storage_path"], output
            receipt = await db.get(ContentInputReceipt, uuid.UUID(artifact.snapshot_json["content_input_receipt_id"]))
            n.artifact = artifact.id
            n.artifacts.append(artifact.id)
            n.receipts.append(receipt)
    n.leaves["workspace_artifact"] = {"source_kind": "workspace_artifact", "source_id": str(n.receipts[0].id),
        "source_version": n.receipts[0].content_sha256}
    for kind, source in n.leaves.items():
        if kind != "workspace_artifact":
            assert [i for i, row in enumerate(n.receipts) if source in row.dependencies_json] == [0]
    if size == 32:
        assert n.leaves["workspace_artifact"] not in n.receipts[-1].dependencies_json
    kinds = Counter(item["source_kind"] for row in n.receipts for item in row.dependencies_json)
    depths = {}
    for receipt in n.receipts:
        depths[str(receipt.id)] = 1 + max((depths[item["source_id"]] for item in receipt.dependencies_json
            if item["source_kind"] == "workspace_artifact"), default=-1)
    n.depth = depths[str(n.receipts[-1].id)]
    assert n.depth == 7
    assert kinds["workspace_artifact"] == (28 if size == 8 else 205)
    print("R15E_GRAPH", json.dumps({"nodes": len(n.receipts), "edges": kinds["workspace_artifact"],
        "depth": n.depth, "dependencies": sum(kinds.values()), "kinds": dict(kinds)}), flush=True)


async def _graph_live_checkpoints(n, owner_sessionmaker):
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation

    # Walk the fixture's retained rows, independently of production discovery.
    # A dropped CTE node must not also shrink this test's statement budget.
    async with owner_sessionmaker() as db:
        receipts = list(await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == n.tenant)))
        tools = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.tenant_id == n.tenant)))
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.tenant_id == n.tenant)))
        spans = list(await db.scalars(select(InvocationSpan).where(InvocationSpan.tenant_id == n.tenant)))
    children = {(row.carrier_kind, str(row.id)): list(row.dependencies_json or []) for row in receipts}
    model_inputs = {(row.session_id, row.run_id, row.provider_request_id):
        row.model_request_snapshot_json["input_bindings"]["dependencies"] for row in models}
    for tool in tools:
        dependencies = list(model_inputs[tool.session_id, tool.run_id, tool.provider_request_id])
        for span in spans:
            metadata = span.metadata_json or {}
            for key in ("workspace_execution_evidence", "workspace_input_evidence", "memory_result"):
                evidence = metadata.get(key) or {}
                if evidence.get("invocation_id") != str(tool.id):
                    continue
                dependencies.extend((evidence.get("execution_inputs") or {}).get("sources", []))
                dependencies.extend(evidence.get("sources", []))
                if evidence.get("content_input_receipt_id"):
                    dependencies.append({"source_kind": evidence.get("carrier_kind", "workspace_artifact"),
                        "source_id": evidence["content_input_receipt_id"]})
        children["session_tool_invocation", str(tool.id)] = dependencies
    pending, sources = [("workspace_artifact", str(n.receipts[-1].id))], set()
    while pending:
        source = pending.pop()
        if source in sources:
            continue
        sources.add(source)
        pending.extend((item["source_kind"], item["source_id"]) for item in children.get(source, []))
    kinds = Counter(kind for kind, _raw in sources)
    # R12a/R7b require one fresh read at each live check point. Directory
    # scope/current roster, memory before/after, and skill access/current
    # package are independent points; none can borrow another point's rows.
    points = {"workspace_artifact": 1, "session_tool_invocation": 1, "session_command": 1,
        "session_actor_identity": 1, "memory_resource_reference": 1, "agent_directory_observation": 2,
        "explicit_memory": 2, "agent_platform_skill_file": 2, "agent_context_resource": 1,
        "agent_identity": 1, "owner_action_policy": 1, "agent_resident_memory": 1}
    # The root is already read by artifact ingress, accounted for in C.
    print("R15E_FIXTURE_SOURCES", json.dumps(dict(kinds)), flush=True)
    # Channels retain base's two independent configuration queries, in
    # addition to the context reader check. They occur once per dependency.
    return (sum(points[kind] * count for kind, count in kinds.items()) - 1
        + 2 * sum(kind == "agent_context_resource" and raw.split("\n", 1)[-1] == "channels" for kind, raw in sources))


async def _history(n, count, app_user_sessionmaker):
    from app.kernel.engine import _context_message
    from app.services.session_model_inputs import capture_context_message_origins

    # Real compiler history in the same session, unrelated to the pinned graph.
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        for index in range(count):
            await capture_context_message_origins(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                run_id=n.run, result_id=uuid.uuid4(), messages=[_context_message(role="system", kind="frozen_prompt",
                    content=f"Later reporting context {index}.\n" * 100, unbound=())])
        await db.commit()
    n.history += count


async def test_delivered_reads_batch_graph_and_ignore_later_history(
    graph, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    n, measurements = graph, {}
    for size in (8, 32):
        await _grow(n, size, owner_sessionmaker, app_user_sessionmaker)
        n.live_checkpoints = await _graph_live_checkpoints(n, owner_sessionmaker)
        for history in (3, 30):
            await _history(n, 3 if history == 3 else 27, app_user_sessionmaker)
            for endpoint in ("download", "content", "preview", "workspace"):
                response, row = await _read(n, endpoint, monkeypatch, history=n.history)
                measurements[size, history, endpoint] = row
                if endpoint == "preview":
                    # Markdown shares the exact artifact authorization path,
                    # then the real Office renderer rejects this file format.
                    assert response.status_code == 400, response.text
                else:
                    assert response.status_code == 200, response.text
                    assert (response.content if endpoint == "download" else response.json()["content"].encode()) == BODY.encode()
    for endpoint in ("download", "content", "preview", "workspace"):
        for size in (8, 32):
            assert measurements[size, 30, endpoint]["statements"] <= measurements[size, 3, endpoint]["statements"] + 5
        # C=40: at most 10 immutable/discovery loads, 15 endpoint/tenant/reader
        # ingress statements, and 15 fixed context/witness/renderer statements.
        # All graph-dependent reads must fit the independently counted points.
        for size in (8, 32):
            assert measurements[size, 30, endpoint]["statements"] <= measurements[size, 30, endpoint]["live_checkpoints"] + 40, measurements


async def test_every_read_rechecks_deep_leaves_and_reader(
    graph, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.models.user import User
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.api import files
    from app.core.security import get_current_user
    from app.database import pin_rls_tenant_context

    n = graph
    await _grow(n, 32, owner_sessionmaker, app_user_sessionmaker)
    mutations = ("explicit_memory", "workspace_artifact", "session_tool_invocation", "agent_platform_skill_file", "reader")
    for kind in mutations:
        restore = None
        async with owner_sessionmaker() as db:
            if kind == "explicit_memory":
                row = await db.get(ContentInputReceipt, uuid.UUID(n.leaves[kind]["source_id"]))
                old = deepcopy(row.authority_json)
                row.authority_json = {**old, "state": "revoked"}
                restore = type(row), row.id, "authority_json", old
            elif kind == "workspace_artifact":
                row = await db.scalar(select(WorkspaceResourceManifest).where(
                    WorkspaceResourceManifest.agent_id == n.agent,
                    WorkspaceResourceManifest.path == n.receipts[0].carrier_ref.split("\n", 1)[1]))
                restore = type(row), row.id, "authority_state", row.authority_state
                row.authority_state = "quarantined"
            elif kind == "session_tool_invocation":
                tool = await db.get(SessionToolInvocation, uuid.UUID(n.leaves[kind]["source_id"]))
                row = await db.get(ChatTranscriptEvent, tool.result_event_id)
                old = deepcopy(row.metadata_json)
                row.metadata_json = {**old, "v2_payload": {**old["v2_payload"], "content": "Changed clock result"}}
                restore = type(row), row.id, "metadata_json", old
            elif kind == "reader":
                async def foreign_user():
                    async with app_user_sessionmaker() as reader_db:
                        await pin_rls_tenant_context(reader_db, n.tenant)
                        return await reader_db.get(User, n.foreign)

                app = n.client._transport.app
                old_reader = app.dependency_overrides[get_current_user]
                old_download = files._load_download_user_from_jwt
                app.dependency_overrides[get_current_user] = foreign_user
                monkeypatch.setattr(files, "_load_download_user_from_jwt", lambda **_kwargs: foreign_user())
            else:
                path = n.root / n.leaves[kind]["source_id"].split("\n")[1]
                old_bytes = path.read_bytes()
                path.write_bytes(old_bytes + b"\nChanged skill source\n")
            await db.commit()
        try:
            for endpoint in ("download", "content", "preview"):
                response, _ = await _read(n, endpoint, monkeypatch)
                print("R15E_LEAF", json.dumps({"kind": kind, "endpoint": endpoint,
                    "status": response.status_code, "detail": response.json().get("detail")}), flush=True)
                assert response.status_code == 403, response.text
                detail = response.json()["detail"]
                expected = {"explicit_memory": "explicit_memory_browser_denied",
                    "workspace_artifact": "content_source_access_denied", "session_tool_invocation": "content_source_changed",
                    "agent_platform_skill_file": "content_source_changed"}
                assert detail == "No access to this agent" if kind == "reader" else detail["code"] == expected[kind]
        finally:
            if kind == "reader":
                app.dependency_overrides[get_current_user] = old_reader
                monkeypatch.setattr(files, "_load_download_user_from_jwt", old_download)
            elif restore:
                model, identity, field, value = restore
                async with owner_sessionmaker() as db:
                    setattr(await db.get(model, identity), field, value)
                    await db.commit()
            else:
                path.write_bytes(old_bytes)
        for endpoint in ("download", "content"):
            response, _ = await _read(n, endpoint, monkeypatch)
            assert response.status_code == 200, response.text
            assert (response.content if endpoint == "download" else response.json()["content"].encode()) == BODY.encode()
