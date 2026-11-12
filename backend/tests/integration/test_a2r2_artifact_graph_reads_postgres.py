"""Production-shaped delivered-file measurements with a local PostgreSQL RTT."""

import hashlib
import json
import uuid
from collections import Counter
from copy import deepcopy

import pytest
from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.chat_artifact import ChatArtifact
from app.models.organization_identity import ContentInputReceipt
from tests.integration.test_r15e_artifact_graph_reads_postgres import BODY, _read, graph  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _six_native_tools(n, app_user_sessionmaker):
    """One admitted command/model round, six real native tool settlements."""
    from app.kernel.engine import _mid_run_items_to_user_messages, _normalize_tool_result_for_llm
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import (
        bind_round_inputs, commit_sealed_model_round, prepare_model_request, seal_model_response,
    )
    from app.services.session_tool_runtime import prepare_tool_invocation
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority

    async def allow(**_kwargs):
        return HookResult()

    specs = [("list_files", {"path": "workspace"})] * 4 + [("get_current_time", {})] * 2
    turn = f"turn-{n.run.hex}"
    calls = [{"id": f"shape-{i}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(specs)]
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        authority = await _authority(db, user_id=n.user, agent_id=n.agent, session_id=n.session)
        admitted = await _accepted_input(db, authority=authority, session_id=n.session, run_id=n.run,
            kind="steer_current_turn", content="Inspect the authorized sales report inputs and current time.")
        await run_user_prompt_admission(db, authority=authority, input_id=admitted.input_id,
            worker_id="production-shape-reference", hook_executor=allow)
        await queue_admitted_human_input(db, authority=authority, input_id=admitted.input_id)
        bound = await bind_round_inputs(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1)
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, messages=_mid_run_items_to_user_messages(bound),
            tools=[{"type": "function", "function": {"name": name}} for name in ("list_files", "get_current_time")],
            provider="synthetic", model="local-reference")
        await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request,
            response={"content": None, "tool_calls": calls, "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 80000, "completion_tokens": 180}})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request)
        invocations = [await prepare_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, provider_request_id=request,
            provider_tool_use_id=call["id"], tool_name=name, arguments=args)
            for call, (name, args) in zip(calls, specs)]
        await db.commit()
    for invocation, (name, args) in zip(invocations, specs):
        trace = {}

        async def start_effect(payload):
            await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session),
                data={"name": name, "args": payload["arguments"], "status": "effect_started",
                    "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": request,
                    "runtime_task_id": str(n.run)})

        raw = await n.service.execute(name, args, agent_id=n.agent, user_id=n.user, session_id=str(n.session),
            runtime_task_id=str(n.run), turn_id=turn, tool_call_id=invocation.provider_tool_use_id,
            emit_runtime_hooks=False, trace_metadata_sink=trace, pre_effect_callback=start_effect)
        await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session),
            data={"name": name, "args": args, "status": "done", "result": str(raw),
                "model_visible_content": _normalize_tool_result_for_llm(raw),
                "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": request,
                "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
    return invocations


async def _production_shape(n, owner_sessionmaker, app_user_sessionmaker):
    from app.memory.types import MemoryItem, MemoryKind
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.models.user import User
    from app.services.agent_context import load_agent_context_resource
    from app.services.agent_directory_sources import observe_directory
    from app.services.content_input_receipts import record_session_actor_identity, record_workspace_receipt
    from app.services.memory_resource_references import _capture_reference
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.session_model_inputs import tool_result_source_hash
    from app.services.session_semantic_history import _event_payload
    from app.services.workspace_resource_authority import register_workspace_path

    # Real-looking reporting context, not a compressible dummy string. Every
    # measured tool model is prepared with this input before its evidence is sealed.
    n.production_context = json.dumps({"reporting_guide": [
        {"account": f"Regional account {i:04d}", "region": ("North", "West", "South")[i % 3],
            "currency": "USD", "booked": 19000 + i * 31, "recognized": 17000 + i * 29,
            "procedure": "Reconcile invoices against signed orders. Explain late bookings, currency changes, "
                "credits and renewals using the retained source report; keep original line-item references.",
            "invoice_ref": f"FY2026-{i:05d}", "owner": f"Sales team {i % 17}"}
        for i in range(1200)]})
    assert len(n.production_context.encode()) >= 300_000
    n.sources = []
    dependencies = [n.common.context, n.common.identity, n.common.policy, n.common.resident,
        *n.common.skills[:8], n.leaves["agent_platform_skill_file"]]
    soul = await load_agent_context_resource(agent_id=n.agent, tenant_id=n.tenant, resource_ref="soul")
    dependencies.append({"source_kind": "agent_context_resource", "source_id": f"{n.agent}\nsoul",
        "source_version": soul.sha256})
    # Seed historical working copies through the existing receipt/manifest writers.
    # Their exact bytes and upstream edges still pass the real browser readers.
    async with owner_sessionmaker() as db:
        previous = None
        for i in range(44):
            path = f"workspace/sales-run-{i:02d}.md"
            (n.root / path).write_text(BODY)
            await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                owner_user_id=n.user, root_session_id=n.session, source="native_tool",
                content_hash=hashlib.sha256(BODY.encode()).hexdigest())
            previous = record_workspace_receipt(db, n.tenant, n.agent, path,
                hashlib.sha256(BODY.encode()).hexdigest(), None,
                additional_dependencies=[{"source_kind": "workspace_artifact", "source_id": str(previous.id),
                    "source_version": previous.content_sha256}] if previous is not None else ())
            n.receipts.append(previous)
        memories = list(await db.scalars(select(ContentInputReceipt).where(
            ContentInputReceipt.tenant_id == n.tenant, ContentInputReceipt.carrier_kind == "explicit_memory")))
        template = memories[0]
        for i in range(4):
            entry = f"shape-memory-{i}"
            path = n.root / f"memory/explicit/entries/{entry}.md"
            path.write_text(f"Retained sales reporting convention {i}.\n")
            receipt = ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="explicit_memory",
                carrier_ref=f"{n.agent}\n{entry}", revision=1,
                content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                authority_json=deepcopy(template.authority_json), dependencies_json=deepcopy(template.dependencies_json))
            db.add(receipt)
            memories.append(receipt)
        dependencies += [{"source_kind": "explicit_memory", "source_id": str(row.id),
            "source_version": row.content_sha256} for row in memories]
        await db.commit()
    # Distinct observations, with one current directory and reader check per ID.
    for i in range(27):
        async with owner_sessionmaker() as db:
            (await db.get(RuntimeTask, n.run)).status = "completed"
            n.run = uuid.uuid4()
            db.add(RuntimeTask(id=n.run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
                parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(n.session),
                child_session_id=str(n.session), root_user_id=n.user, root_session_id=str(n.session),
                root_runtime_task_id=n.run, prompt="Inspect reporting inputs", metadata_json={"turn_id": f"turn-{n.run.hex}"}))
            await db.commit()
        principal = AgentRuntimePrincipal(agent_id=n.agent, requester_user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), purpose="interactive_session")
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            _, directory = await observe_directory(db, tenant_id=n.tenant, agent_id=n.agent, runtime_request=principal)
            dependencies.append(directory)
            for index in ([i, 27] if i == 26 else [i]):
                resource = f"memory/t2/sessions/{n.session}/segments/shape-{index}"
                (n.root / resource).mkdir(parents=True)
                (n.root / resource / "manifest.json").write_text("{}")
                (n.root / resource / "summary.md").write_text("Retained sales reporting index")
                reference = await _capture_reference(db, item=MemoryItem(kind=MemoryKind.EPISODIC, content="",
                    source=resource, metadata={"session_id": str(n.session)}), data_root=n.root.parent,
                    tenant_id=n.tenant, reader=principal, user=await db.get(User, n.user))
                dependencies += reference.source_refs
            if i < 18:
                user = await db.get(User, n.user)
                user.display_name = f"Report owner {i}"
                await db.flush()
                actor, _ = await record_session_actor_identity(db, tenant_id=n.tenant, agent_id=n.agent,
                    session_id=n.session, user_id=n.user, display_name=user.display_name, runtime_task_id=n.run)
                dependencies.append(actor)
            await db.commit()
        if i < 26:
            invocations = await _six_native_tools(n, app_user_sessionmaker)
            async with owner_sessionmaker() as db:
                for invocation in invocations:
                    tool = await db.get(SessionToolInvocation, invocation.id)
                    payload = _event_payload(await db.get(ChatTranscriptEvent, tool.result_event_id))
                    dependencies.append({"source_kind": "session_tool_invocation", "source_id": str(tool.id),
                        "source_version": tool_result_source_hash(payload)})
    # Keep the root at 57 direct dependencies, while its receipt closure retains
    # all the observations. Historical receipts are immutable fixture inputs.
    async with owner_sessionmaker() as db:
        root_sources = dependencies[:56]
        remainder = dependencies[56:]
        for i, receipt in enumerate(n.receipts):
            receipt.dependencies_json = [*receipt.dependencies_json, *remainder[i::len(n.receipts)]]
            await db.merge(receipt)
        await db.commit()
    n.sources = [*root_sources, {"source_kind": "workspace_artifact", "source_id": str(n.receipts[-1].id),
        "source_version": n.receipts[-1].content_sha256}]
    n.path = "workspace/sales-report-production-shape.md"
    output, _ = await memory_call(n, app_user_sessionmaker, "write_file", {"path": n.path, "content": BODY})
    async with owner_sessionmaker() as db:
        artifact = (await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run,
            ChatArtifact.path == n.path))).one()
        assert artifact.snapshot_json["snapshot_storage_path"], output
        n.artifact = artifact.id
        n.receipts.append(await db.get(ContentInputReceipt, uuid.UUID(artifact.snapshot_json["content_input_receipt_id"])))
    n.depth = 44


async def test_production_shaped_delivered_reads(graph, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.services import content_input_receipts as receipts

    n = graph
    await _production_shape(n, owner_sessionmaker, app_user_sessionmaker)
    # Reuse the exact recursive discovery statement used by production; the
    # measurements below include it when the production reader issues it.
    closure_sql = next(value for value in receipts._preload_execution_sources.__code__.co_consts
        if isinstance(value, str) and "WITH RECURSIVE sources" in value)
    async with owner_sessionmaker() as db:
        closure = list(await db.execute(text(closure_sql), {"dependencies": json.dumps([{
            "source_kind": "workspace_artifact", "source_id": str(n.receipts[-1].id)}]), "tenant_id": n.tenant}))
        sizes = (await db.execute(text("""SELECT
            (SELECT max(octet_length(model_request_snapshot_json::text)) FROM session_model_results WHERE tenant_id=:tenant),
            (SELECT max(pg_column_size(model_request_snapshot_json)) FROM session_model_results WHERE tenant_id=:tenant),
            (SELECT max(octet_length(metadata_json::text)) FROM chat_transcript_events WHERE tenant_id=:tenant)
        """), {"tenant": n.tenant})).one()
    counts = Counter(kind for kind, _ in closure)
    expected = {"session_tool_invocation": 162, "workspace_artifact": 45, "memory_resource_reference": 28,
        "agent_directory_observation": 27, "session_command": 27, "session_actor_identity": 18,
        "agent_platform_skill_file": 9, "explicit_memory": 6, "agent_context_resource": 2,
        "agent_identity": 1, "owner_action_policy": 1, "agent_resident_memory": 1}
    print("A2R2_SHAPE", json.dumps({"nodes": len(closure), "kinds": dict(counts), "largest_model_json_bytes": sizes[0],
        "largest_model_stored_bytes": sizes[1], "largest_transcript_json_bytes": sizes[2],
        "root_dependencies": len(n.receipts[-1].dependencies_json)}), flush=True)
    assert set(counts) == set(expected), counts
    for kind, count in expected.items():
        assert count * .9 <= counts[kind] <= count * 1.1, counts
    assert sizes[0] >= 300_000
    measurements = []
    for delay in (0, .00235):
        for endpoint in ("download", "content", "preview"):
            response, row = await _read(n, endpoint, monkeypatch, delay=delay)
            measurements.append(row)
            assert response.status_code == (400 if endpoint == "preview" else 200), response.text
            if endpoint != "preview":
                assert (response.content if endpoint == "download" else response.json()["content"].encode()) == BODY.encode()
    assert all(row["seconds"] <= 2.0 for row in measurements if row["injected_rtt"]), measurements
