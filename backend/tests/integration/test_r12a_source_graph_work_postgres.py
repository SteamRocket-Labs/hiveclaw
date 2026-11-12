"""Source validation work follows the retained graph, not its number of paths."""

import asyncio
import json
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.database import tenant_scoped_session
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionCommand, SessionModelResult, SessionToolInvocation
from app.runtime.context_candidates import ContextMessageOrigin
from app.services.llm_client import LLMMessage
from tests.integration.test_artifact_publication_transactions import stage_report
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _prepare_write, native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_memory, save_group  # noqa: F401
from tests.integration.test_m4e_team_memory_authority_postgres import runtime_team  # noqa: F401
from tests.integration.test_m4f_team_memory_runtime_postgres import readable_team_memory  # noqa: F401
from tests.services.test_session_semantic_history import _seal_round

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

BODY = "# Report\nCOMPANY_SOURCE_MARKER\n"
DERIVED = "R12A_DERIVED_NEWEST_REPORT"
TIMEOUT = 8


def _source(receipt):
    return {"source_kind": "workspace_artifact", "source_id": str(receipt.id),
        "source_version": receipt.content_sha256}


@contextmanager
def _sql():
    statements = []

    def executed(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(Engine, "after_cursor_execute", executed)
    try:
        yield statements
    finally:
        event.remove(Engine, "after_cursor_execute", executed)


async def _measure(n, path, operation):
    started = time.perf_counter()
    result, timed_out = None, False
    with _sql() as statements:
        try:
            result = await asyncio.wait_for(operation(), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            timed_out = True
    row = {"path": path, "N": n.size, "nodes": n.nodes, "edges": n.edges,
        "statements": len(statements), "seconds": round(time.perf_counter() - started, 4),
        "timed_out": timed_out}
    if path == "heartbeat_t2":
        row["events"] = n.t2_events
    print("R12A_GRAPH_SQL", json.dumps(row), flush=True)
    return result, row


async def _graph(n, size, owner_sessionmaker, app_user_sessionmaker, monkeypatch, *, deep_source=False):
    from app.api import files
    from app.models.audit import ChatMessage
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_artifact import ChatArtifact
    from app.services.chat_artifact_delivery import build_artifact_candidate
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.services.session_model_inputs import tool_result_source_hash
    from app.services.session_model_round import prepare_model_request
    from app.services.session_semantic_history import _event_payload
    from app.memory.t0.ledger import replay_t0_session_events, seal_t0_session_segment

    monkeypatch.setattr(files.settings, "AGENT_DATA_DIR", str(n.root.parent))
    n.receipts = []
    n.size = size
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.file.read",
            allowed=True, requires_approval=False))
        n.receipts.append(await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user,
            session=n.session, root=n.root, path="workspace/graph-0.md"))
        await db.commit()
    invocations = []
    for _ in range(2):
        raw, invocation = await memory_call(n, app_user_sessionmaker, "read_file", {"path": "workspace/graph-0.md"})
        assert "COMPANY_SOURCE_MARKER" in raw, raw
        invocations.append(invocation.id)
    async with owner_sessionmaker() as db:
        commands = list(await db.scalars(select(SessionCommand).where(
            SessionCommand.session_id == n.session, SessionCommand.namespace == "human_input")))
        n.commands = [row.id for row in commands]
        shared = [{"source_kind": "session_command", "source_id": str(row.id),
            "source_version": row.request_hash} for row in commands]
        if deep_source:
            # The changed command must be reached through the oldest copy;
            # putting it directly on every receipt would miss a stale-child bug.
            leaf = await db.get(type(n.receipts[0]), n.receipts[0].id)
            leaf.dependencies_json = [shared.pop(0)]
            n.receipts[0] = leaf
        for invocation_id in invocations:
            row = await db.get(SessionToolInvocation, invocation_id)
            payload = _event_payload(await db.get(ChatTranscriptEvent, row.result_event_id))
            shared.append({"source_kind": "session_tool_invocation", "source_id": str(row.id),
                "source_version": tool_result_source_hash(payload)})
        for index in range(1, size):
            n.receipts.append(await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user,
                session=n.session, root=n.root, path=f"workspace/graph-{index}.md",
                dependencies=[*map(_source, n.receipts), *shared]))
        await db.commit()
    n.path = f"workspace/graph-{size - 1}.md"
    # Mechanical snapshot capture avoids measuring publication during fixture setup.
    # The API still performs the real recipient/source checks on these real rows.
    candidate = build_artifact_candidate(agent_id=n.agent, session_id=n.session, runtime_task_id=n.run,
        workspace_root=n.root, path=n.path, source="workspace_read")
    assert candidate
    async with owner_sessionmaker() as db:
        message = ChatMessage(id=uuid.uuid4(), tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            conversation_id=str(n.session), role="assistant", content="Report ready")
        db.add(message)
        await db.flush()
        snapshot = {**candidate["snapshot"], "content_input_receipt_id": str(n.receipts[-1].id)}
        artifact = ChatArtifact(id=uuid.UUID(candidate["artifact_id"]), tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, runtime_task_id=n.run, message_id=message.id, owner_user_id=n.user,
            root_session_id=n.session, authority_state="owned", snapshot_json=snapshot,
            **{key: candidate[key] for key in ("path", "name", "mime_type", "size", "modified_at",
                "preview_kind", "source", "snapshot_hash")})
        db.add(artifact)
        n.artifact = artifact.id
        await db.commit()
    raw, newest_read = await asyncio.wait_for(memory_call(n, app_user_sessionmaker, "read_file",
        {"path": n.path}), TIMEOUT)
    assert "COMPANY_SOURCE_MARKER" in raw, raw
    n.newest_read, n.newest_raw = newest_read, raw
    # A genuine read-tool result enters a committed Web round. Its source-bound
    # output is revalidated at the next provider boundary and by T2.
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        message = LLMMessage(role="tool", content=raw, tool_call_id=newest_read.provider_tool_use_id,
            source_ref=f"session-tool-result:{newest_read.id}")
        request_id = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=4, messages=[message], tools=[],
            provider="synthetic", model="local-reference")
        await _seal_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, run_id=n.run,
            turn_id=f"turn-{n.run.hex}", round_index=4, provider_request_id=request_id, content=DERIVED)
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        assert _source(n.receipts[-1]) in model.model_request_snapshot_json["input_bindings"]["dependencies"]
        n.model = model.id
        transcript = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.result_id == model.id,
            ChatTranscriptEvent.item_kind == "assistant_text"))
        assert transcript is not None
        transcript_id = transcript.id
        await db.commit()
    assert await bridge_transcript_event_to_t0(transcript_event_id=transcript_id)
    sealed = seal_t0_session_segment(agent_id=n.agent, session_id=n.session, reason="turn_stop")
    assert sealed
    events = replay_t0_session_events(agent_id=n.agent, session_id=n.session, data_root=n.root.parent)
    n.segment = next(event.segment_id for event in events if event.content == DERIVED)
    from app.memory.t2.segment_package import select_t2_segment_events

    n.t2_events = sum(event.event_type != "segment_boundary"
        for event in select_t2_segment_events(events, t0_segment_id=n.segment).source_events)
    # Include the retained commands/tools and their producer model results.
    n.nodes = size + len(shared) + 2 + 1 + 3 + int(deep_source)
    n.edges = sum(len(row.dependencies_json) for row in n.receipts) + 10
    return n


async def _browser(n, endpoint):
    return await n.client.get(f"/agents/{n.agent}/files/artifacts/{n.artifact}/{endpoint}")


async def _heartbeat(n):
    # Exact source-admission phase invoked by heartbeat's held T2-job retry,
    # before that job's first model/configuration call. No provider is used.
    from app.memory.t2.segment_package import _build_authorized_source_bundle

    return await _build_authorized_source_bundle(root=n.root.parent, agent_id=n.agent, tenant_id=n.tenant,
        session_id=n.session, t0_segment_id=n.segment, package_id="r12a-measurement")


async def _model(n, app_user_sessionmaker):
    from app.services.learned_skill_sources import model_source
    from app.services.session_model_round import prepare_model_request

    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        source = model_source(await db.get(SessionModelResult, n.model))
        messages = [LLMMessage(role="system", content=DERIVED,
            context_origin=ContextMessageOrigin.capture(kind="history", role="system", content=DERIVED,
                sources=[source], unbound=()))]
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=5, messages=messages, tools=[],
            provider="synthetic", model="local-reference")
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot = row.model_request_snapshot_json
        n.next_model = row.id
        await db.commit()
        return snapshot


async def _delegation(n, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.agents import orchestrator
    from app.config import get_settings
    from app.core.execution_context import ExecutionPrincipal
    from app.kernel.contracts import ExecutionIdentityRef
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.runtime.ccplus_contracts import permission_profile_snapshot
    from app.services import agent_context
    from app.services.agent_manager import AgentManager
    from app.services.delegation_input_origin import capture_delegation_instruction_origin
    from app.services.secrets_provider import get_secrets_provider
    from app.services.web_chat_runtime import _persist_tool_call
    from app.tools.runtime import ToolExecutionContext

    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", n.root.parent)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", n.root.parent)
    monkeypatch.setattr("app.services.agent_manager.settings", get_settings())
    async with owner_sessionmaker() as db:
        model = LLMModel(tenant_id=n.tenant, provider="openai", model="synthetic-local",
            label="Local delegation probe", base_url="http://127.0.0.1:1", max_input_tokens=256_000,
            api_key_encrypted=get_secrets_provider().encrypt("synthetic-local-only"))
        db.add(model)
        await db.flush()
        child = Agent(id=uuid.uuid4(), tenant_id=n.tenant, name="Graph worker", creator_id=n.user,
            owner_user_id=n.user, primary_model_id=model.id)
        db.add(child)
        await db.flush()
        await AgentManager().initialize_agent_files(db, child,
            personality="Prepare referenced draft reports.", original_author_id=n.user)
        await db.commit()
    instruction = f"Prepare the referenced report from these authorized inputs:\n{BODY}\n{DERIVED}"
    arguments = {"agent_name": child.name, "target_agent_id": str(child.id), "message": instruction}
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        # Use the parent's real read result, as the Web loop does, so the child
        # inherits the working copies/commands/tools rather than an invented
        # cross-employee model-memory pointer.
        invocation, _, _ = await _prepare_write(db, n, tool_name="delegate_to_agent", arguments=arguments,
            round_index=5, messages=[LLMMessage(role="tool", content=n.newest_raw,
                tool_call_id=n.newest_read.provider_tool_use_id,
                source_ref=f"session-tool-result:{n.newest_read.id}")])
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": "delegate_to_agent", "args": arguments, "tool_call_id": invocation.provider_tool_use_id,
        "provider_request_id": invocation.provider_request_id, "runtime_task_id": str(n.run),
        "status": "effect_started"})
    origin = await capture_delegation_instruction_origin(ToolExecutionContext(agent_id=n.agent,
        user_id=n.user, tenant_id=str(n.tenant), workspace=n.root, session_id=str(n.session),
        runtime_task_id=str(n.run), tool_call_id=invocation.provider_tool_use_id), arguments,
        recipient_agent_id=child.id)
    assert origin is not None
    run, session = uuid.uuid4(), uuid.uuid4()
    principal = ExecutionPrincipal(tenant_id=n.tenant, source_agent_id=n.agent, requester_user_id=n.user,
        root_session_id=str(n.session), root_runtime_task_id=str(n.run))
    request = orchestrator.AgentDelegationRequest(target=child, target_model=model,
        conversation_messages=[{"role": "user", "content": instruction, "a2a_instruction_origin": origin}],
        owner_id=n.user, session_id=str(session), parent_agent_id=n.agent, parent_session_id=str(n.session),
        tenant_id=n.tenant, runtime_task_id=str(run), root_runtime_task_id=str(n.run), trace_id="r12a-delegation",
        execution_principal=principal.to_evidence(),
        execution_identity=ExecutionIdentityRef(identity_type="delegated_user", identity_id=n.user),
        permission_profile=permission_profile_snapshot(None),
        policy=orchestrator.OrchestrationPolicy(timeout_seconds=TIMEOUT))
    request.execution_receipt = orchestrator._build_delegation_execution_receipt(
        request, task_id=str(run), trace_id=request.trace_id, status="running")
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(id=run, task_type="delegation", status="running", tenant_id=n.tenant,
            parent_agent_id=n.agent, child_agent_id=child.id, parent_session_id=str(n.session),
            child_session_id=str(session), root_session_id=str(n.session), root_user_id=n.user,
            root_runtime_task_id=n.run, prompt=instruction, claimed_by="r12a-delegation-worker",
            claim_version=1, attempt_count=1, claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            metadata_json=orchestrator._build_runtime_task_metadata(request, task_id=str(run))))
        await db.commit()
    n.delegation = request
    return n


async def _delegated_first_model(n, monkeypatch):
    from functools import wraps

    import app.runtime.invoker as invoker
    from app.agents import orchestrator
    from app.services import delegation_input_origin, session_model_round
    from app.services.delegation_session_runtime import DelegationSessionRuntime
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, SimulatedWorkerCrash

    # Stop at the real transport boundary, after canonical preparation, without
    # sending model bytes or including final-response settlement in the budget.
    client = ScriptedLLMClient([SimulatedWorkerCrash()])
    first, phases = {}, []
    result, timed_out = None, False
    started = time.perf_counter()
    with _sql() as statements, monkeypatch.context() as patch:
        def observe(module, name, phase):
            original = getattr(module, name)

            @wraps(original)
            async def measured(*args, **kwargs):
                before, at = len(statements), time.perf_counter()
                try:
                    return await original(*args, **kwargs)
                finally:
                    phases.append({"phase": phase, "statements": len(statements) - before,
                        "seconds": round(time.perf_counter() - at, 4)})

            patch.setattr(module, name, measured)

        observe(DelegationSessionRuntime, "attach", "delegation_context")
        observe(delegation_input_origin, "authorize_delegation_instruction", "instruction_validation")
        observe(invoker, "_resolve_memory_context", "memory_context")
        observe(session_model_round, "prepare_model_request", "model_round")
        stream = client.stream

        async def reached(**kwargs):
            first.update(statements=len(statements), seconds=round(time.perf_counter() - started, 4))
            return await stream(**kwargs)

        patch.setattr(client, "stream", reached)
        patch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        request = n.delegation
        token = set_runtime_task_fence(task_id=uuid.UUID(request.runtime_task_id),
            worker_id="r12a-delegation-worker", claim_version=1)
        try:
            result = await asyncio.wait_for(orchestrator._delegate_after_cycle_check(request,
                trace_id=request.trace_id, child_session_id=request.session_id,
                tool_profile=orchestrator._resolve_delegation_tool_profile("worker_safe"),
                is_delegation=True), TIMEOUT)
        except SimulatedWorkerCrash:
            assert len(client.requests) == 1
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            reset_runtime_task_fence(token)
        row = {"path": "delegated_pre_model", "N": n.size, "nodes": n.nodes, "edges": n.edges,
            "statements": first.get("statements", len(statements)),
            "seconds": first.get("seconds", round(time.perf_counter() - started, 4)),
            "timed_out": timed_out, "first_request": bool(first), "phases": phases}
    print("R12A_GRAPH_SQL", json.dumps(row), flush=True)
    return client.requests, result, row


@pytest.mark.parametrize("size", [4, 6, 8, 10, 16])
async def test_delegated_pre_model_source_graph_statement_work(native_workspace, owner_sessionmaker,  # noqa: F811
    app_user_sessionmaker, monkeypatch, size):
    n = await _graph(native_workspace, size, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    await _delegation(n, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    requests, result, row = await _delegated_first_model(n, monkeypatch)
    assert not row["timed_out"] and row["first_request"], (row, result)
    # Two live instruction checks plus fixed child/context assembly: measured
    # Live authority reads retain the same linear bound, including cold setup.
    assert row["statements"] <= 3 * (n.nodes + n.edges) + 400, row
    assert DERIVED in json.dumps(requests) and "COMPANY_SOURCE_MARKER" in json.dumps(requests)
    assert [phase["phase"] for phase in row["phases"]] == [
        "delegation_context", "instruction_validation", "memory_context", "instruction_validation", "model_round"]
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.run_id == uuid.UUID(n.delegation.runtime_task_id)))
        bindings = model.model_request_snapshot_json["input_bindings"]
        assert bindings["dependency_coverage"]["complete"]
        assert _source(n.receipts[-1]) in bindings["dependencies"]
        assert not any(entry.get("status") == "withheld" for entry in bindings["messages"])


async def test_delegated_deep_source_change_withholds_before_first_model(native_workspace, owner_sessionmaker,  # noqa: F811
    app_user_sessionmaker, monkeypatch):
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority

    n = await _graph(native_workspace, 16, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    # This independent real command was never input to any producer model or
    # read tool. Its only incoming edge is on the oldest working copy.
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        authority = await _authority(db, user_id=n.user, agent_id=n.agent, session_id=n.session)
        accepted = await _accepted_input(db, authority=authority, session_id=n.session, run_id=n.run,
            kind="steer_current_turn", content="An independent input used only by the oldest report.")
        command = await db.get(SessionCommand, accepted.command_id)
        bad_source = {"source_kind": "session_command", "source_id": str(command.id),
            "source_version": command.request_hash}
    async with owner_sessionmaker() as db:
        oldest = await db.get(type(n.receipts[0]), n.receipts[0].id)
        oldest.dependencies_json = [bad_source]
        n.receipts[0] = oldest
        await db.commit()
    n.nodes += 1
    n.edges += 1
    await _delegation(n, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    assert all(bad_source not in receipt.dependencies_json
        for receipt in n.receipts[1:])
    async with owner_sessionmaker() as db:
        parent_models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == n.run)))
        assert all(bad_source["source_id"] not in json.dumps(model.model_request_snapshot_json)
            for model in parent_models)
        command = await db.get(SessionCommand, accepted.command_id)
        command.request_hash = "0" * 64
        await db.commit()
    requests, result, row = await _delegated_first_model(n, monkeypatch)
    assert not row["timed_out"] and not requests, row
    assert result.failed and result.terminal_reason == "delegation_instruction_source_access_denied", result
    assert DERIVED not in result.content and "COMPANY_SOURCE_MARKER" not in result.content
    async with owner_sessionmaker() as db:
        assert not list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id == uuid.UUID(n.delegation.runtime_task_id))))
        for receipt in n.receipts:
            current = await db.get(type(receipt), receipt.id, populate_existing=True)
            assert current.dependencies_json == receipt.dependencies_json


@pytest.mark.parametrize("size", [4, 6, 8, 10, 16])
async def test_source_graph_statement_work(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch, size):  # noqa: F811
    n = await _graph(native_workspace, size, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    rows = []
    for path, operation in [("download", lambda: _browser(n, "download")),
        ("content", lambda: _browser(n, "content")), ("heartbeat_t2", lambda: _heartbeat(n)),
        ("web_model", lambda: _model(n, app_user_sessionmaker))]:
        result, row = await _measure(n, path, operation)
        rows.append(row)
        if row["timed_out"]:
            continue
        if path in {"download", "content"}:
            assert result.status_code == 200, result.text
            assert (result.content == BODY.encode() if path == "download" else result.json()["content"] == BODY)
        elif path == "heartbeat_t2":
            assert result is not None and DERIVED in json.dumps(result)
        else:
            assert DERIVED in json.dumps(result["wire_request"])
            assert not any(entry.get("status") == "withheld" for entry in result["input_bindings"]["messages"])
    # One source costs a bounded number of indexed authority/source lookups;
    # edges must not multiply completed descendant walks. Tightened from the
    # measured green numbers, with fixed request overhead.
    # T2 at N=4/6/8/10/16: 1097/1277/1457/1637/2177 SQL, 53 events.
    # The endpoint slope is ceil(1080 / 174) = 7 per node+edge. The largest
    # residual is 869 / 53 = 16.4 per event; round up and reserve 2 => 19.
    # This leaves 138..342 statements of margin without an authority snapshot.
    graph_size = n.nodes + n.edges
    bounds = {"download": 3 * graph_size + 100, "content": 3 * graph_size + 100,
        "heartbeat_t2": 19 * n.t2_events + 7 * graph_size, "web_model": 3 * graph_size + 100}
    violations = [row for row in rows if row["timed_out"]
        or row["statements"] > bounds[row["path"]]]
    assert not violations, violations


@pytest.mark.parametrize("path", ["download", "content", "heartbeat_t2", "web_model", "run_read"])
async def test_large_graph_deep_source_change_withholds(native_workspace, owner_sessionmaker,  # noqa: F811
    app_user_sessionmaker, monkeypatch, path):
    n = await _graph(native_workspace, 16, owner_sessionmaker, app_user_sessionmaker, monkeypatch, deep_source=True)
    assert all(str(n.commands[0]) not in {ref["source_id"] for ref in receipt.dependencies_json}
        for receipt in n.receipts[1:])
    async with owner_sessionmaker() as db:
        command = await db.get(SessionCommand, n.commands[0])
        command.request_hash = "0" * 64
        await db.commit()
    if path in {"download", "content"}:
        result = await asyncio.wait_for(_browser(n, path), TIMEOUT)
        assert result.status_code == 403, result.text
        assert "COMPANY_SOURCE_MARKER" not in result.text
    elif path == "heartbeat_t2":
        from app.services import content_input_receipts as receipts
        from app.services.content_input_receipts import ContentInputReceiptError

        denied = []
        validate = receipts._validate_input_dependencies

        async def observe_denial(*args, **kwargs):
            try:
                return await validate(*args, **kwargs)
            except ContentInputReceiptError as exc:
                denied.append((exc.code, exc.status_code))
                raise

        monkeypatch.setattr(receipts, "_validate_input_dependencies", observe_denial)
        result = await asyncio.wait_for(_heartbeat(n), TIMEOUT)
        assert denied and set(denied) == {("content_source_changed", 403)}, denied
        assert result is not None and DERIVED not in json.dumps(result), result
    elif path == "web_model":
        result = await asyncio.wait_for(_model(n, app_user_sessionmaker), TIMEOUT)
        assert DERIVED not in json.dumps(result["wire_request"])
        assert any(row.get("status") == "withheld" and row.get("code") == "content_source_changed"
            for row in result["input_bindings"]["messages"])
    else:
        result = str(await asyncio.wait_for(n.service.execute("read_file", {"path": n.path},
            agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run),
            emit_runtime_hooks=False), TIMEOUT))
        assert "COMPANY_SOURCE_MARKER" not in result
        assert "content_source_changed" in result
        own_copy = await asyncio.wait_for(n.client.get(f"/agents/{n.agent}/files/content", params={"path": n.path}), TIMEOUT)
        assert own_copy.status_code == 200 and own_copy.json()["content"] == BODY
    # Withholding never rewrites the retained source graph or its old model.
    from app.models.organization_identity import ContentInputReceipt

    async with owner_sessionmaker() as db:
        for receipt in n.receipts:
            current = await db.get(ContentInputReceipt, receipt.id, populate_existing=True)
            assert current.dependencies_json == receipt.dependencies_json
        if path == "web_model":
            from app.services.session_model_inputs import load_context_message_origin, _origin_dependencies
            from app.services.learned_skill_sources import model_source

            # A4d round 5: inspect the newly withheld round's retained origin,
            # not just the model sealed before the source changed.
            model = await db.get(SessionModelResult, n.next_model)
            assert model.round_id == f"{n.run}:round:5"
            snapshot = model.model_request_snapshot_json
            withheld = [entry for entry in snapshot["input_bindings"]["messages"] if entry["status"] == "withheld"]
            assert withheld and DERIVED not in json.dumps(snapshot["wire_request"])
            old_source = model_source(await db.get(SessionModelResult, n.model))
            for entry in withheld:
                origin = await load_context_message_origin(db, tenant_id=n.tenant, agent_id=n.agent,
                    source_id=uuid.UUID(entry["source_ref"].split(":", 1)[1]), session_id=n.session)
                assert origin["model_result_id"] == str(model.id)
                assert old_source in origin["dependencies"]
                dependencies, complete = await _origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
                    ref=entry["source_ref"])
                assert complete and old_source in dependencies
                retained = next(source for source in dependencies if source == old_source)
                dependencies, complete = await _origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
                    ref=f"session-model-result:{retained['source_id']}")
                assert complete and _source(n.receipts[-1]) in dependencies


@pytest.mark.parametrize("path", ["download", "heartbeat_t2", "web_model"])
async def test_sources_are_validated_once_and_authority_checks_stay_live(native_workspace,  # noqa: F811
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, path):
    from app.core import permissions
    from app.core.source_read_purpose import current_source_recipient
    from app.models.chat_session import ChatSession
    from app.services import content_input_receipts as receipts

    n = await _graph(native_workspace, 10, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    validations, authority, checks, resolutions = Counter(), Counter(), Counter(), Counter()
    validate, access = receipts._validate_input_dependency_batch, permissions.check_agent_access
    live_access = permissions._agent_access_for_live_row
    facts_access = getattr(permissions, "agent_access_from_facts", None)
    authorize, resolve = receipts._authorize_session_input_source, receipts._resolve_session_input_source

    async def observe_validation(db, tenant, dependencies, **context):
        scope = receipts._DISCLOSURE_SOURCE_VALIDATIONS.get()
        reader = context.get("runtime_request") or context.get("user")
        for source in dependencies:
            identity = tuple(source[key] for key in ("source_kind", "source_id", "source_version"))
            validations[scope, id(reader), current_source_recipient(), identity] += 1
        return await validate(db, tenant, dependencies, **context)

    def count_access(user, agent_id):
        scope = receipts._DISCLOSURE_SOURCE_VALIDATIONS.get()
        if scope is not None and scope.active:
            authority[scope, user.id, agent_id] += 1

    async def observe_access(db, user, agent, **kwargs):
        count_access(user, agent)
        return await access(db, user, agent, **kwargs)

    async def observe_live_access(db, user, agent, tenant_active):
        count_access(user, agent.id)
        return await live_access(db, user, agent, tenant_active)

    async def observe_facts_access(db, user, facts):
        # Preserve the session-source scope of the original hook. Base's
        # workspace ACL uses a separate imported check_agent_access entry.
        if isinstance(facts[5], ChatSession):
            count_access(user, facts[0].id)
        return await facts_access(db, user, facts)

    def authority_key(db, tenant, session, context):
        return (receipts._DISCLOSURE_SOURCE_VALIDATIONS.get(), db.get_transaction(), tenant, session.id,
            id(context.get("user")), id(context.get("runtime_request")))

    async def observe_check(db, tenant, session, **context):
        checks[authority_key(db, tenant, session, context)] += 1
        return await authorize(db, tenant, session, **context)

    async def observe_resolution(db, tenant, session, **context):
        resolutions[authority_key(db, tenant, session, context)] += 1
        return await resolve(db, tenant, session, **context)

    monkeypatch.setattr(receipts, "_validate_input_dependency_batch", observe_validation)
    monkeypatch.setattr(permissions, "check_agent_access", observe_access)
    monkeypatch.setattr(permissions, "_agent_access_for_live_row", observe_live_access)
    if facts_access is not None:
        monkeypatch.setattr(permissions, "agent_access_from_facts", observe_facts_access)
        monkeypatch.setattr(receipts, "agent_access_from_facts", observe_facts_access)
    monkeypatch.setattr(receipts, "_authorize_session_input_source", observe_check)
    monkeypatch.setattr(receipts, "_resolve_session_input_source", observe_resolution)
    operation = {"download": lambda: _browser(n, "download"), "heartbeat_t2": lambda: _heartbeat(n),
        "web_model": lambda: _model(n, app_user_sessionmaker)}[path]
    await asyncio.wait_for(operation(), TIMEOUT)
    assert validations and max(validations.values()) == 1, validations
    observed = {key[-1] for key in validations if key[-1][0] == "workspace_artifact"}
    expected = n.receipts[:-1] if path == "download" else n.receipts
    assert observed == {tuple(_source(receipt).values()) for receipt in expected}
    # R12a review round 1: authority reads stay live within one request.
    assert checks and all(resolutions[key] == count for key, count in checks.items()), (checks, resolutions)
    # N=10: live Agent checks peak at 4 on download, 26 per T2 event,
    # and 37 in the new model round; a one-check authority snapshot must fail.
    assert authority and max(authority.values()) == {
        "download": 4, "heartbeat_t2": 26, "web_model": 37}[path], authority


@pytest.mark.parametrize("path", ["download", "content", "heartbeat_t2", "web_model", "run_read"])
async def test_cycle_is_not_hidden_by_completed_dag_siblings(native_workspace, owner_sessionmaker,  # noqa: F811
    app_user_sessionmaker, monkeypatch, path):
    from app.models.organization_identity import ContentInputReceipt
    from app.services.content_input_receipts import ContentInputReceiptError

    n = await _graph(native_workspace, 10, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    async with owner_sessionmaker() as db:
        # graph-0 completes before graph-1 reaches the back-edge.
        leaf = await db.get(ContentInputReceipt, n.receipts[1].id)
        leaf.dependencies_json = [_source(n.receipts[-1])]
        await db.commit()
    if path in {"download", "content"}:
        response = await asyncio.wait_for(_browser(n, path), TIMEOUT)
        assert response.status_code == 503 and "COMPANY_SOURCE_MARKER" not in response.text
        assert response.json()["detail"]["code"] == "content_source_unavailable"
    elif path == "heartbeat_t2":
        with pytest.raises(ContentInputReceiptError) as error:
            await asyncio.wait_for(_heartbeat(n), TIMEOUT)
        assert error.value.status_code == 503 and error.value.code == "content_source_unavailable"
    elif path == "web_model":
        snapshot = await asyncio.wait_for(_model(n, app_user_sessionmaker), TIMEOUT)
        assert DERIVED not in json.dumps(snapshot["wire_request"])
        assert any(entry.get("status") == "withheld" and entry.get("code") == "content_source_unavailable"
            for entry in snapshot["input_bindings"]["messages"])
    else:
        result = str(await asyncio.wait_for(n.service.execute("read_file", {"path": n.path},
            agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run),
            emit_runtime_hooks=False), TIMEOUT))
        assert "COMPANY_SOURCE_MARKER" not in result and "content_source_unavailable" in result


async def test_browser_group_memory_membership_is_live_after_dependency_walk(group_memory,  # noqa: F811
    owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.core.resource_authority import workspace_resource_id
    from app.models.organization_identity import ContentInputReceipt
    from app.models.security_audit import ResourcePermission
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services import explicit_memory_authority as memory

    g, path = group_memory, "workspace/group-report.md"
    n = g.n
    await save_group(g, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        source = memory.source_dependency(await db.get(ContentInputReceipt, g.source_id))
        await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.foreign, session=n.session,
            root=n.root, path=path, dependencies=[source])
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
        manifest.root_session_id = None
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, path),
            actions=["read"], effect="allow", conditions={}))
        await db.commit()
    route = f"/agents/{n.agent}/files/content"
    allowed = await n.client.get(route, params={"path": path})
    assert allowed.status_code == 200 and allowed.json()["content"] == BODY, allowed.text
    walk, revoked = memory._validate_input_dependencies, False

    async def revoke_after_walk(db, tenant, dependencies, **context):
        nonlocal revoked
        await walk(db, tenant, dependencies, **context)
        if g.source_id in context.get("active_workspace_receipts", ()) and not revoked:
            g.members.remove("memory-author")
            revoked = True

    monkeypatch.setattr(memory, "_validate_input_dependencies", revoke_after_walk)
    response = await n.client.get(route, params={"path": path})
    assert revoked
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "content_source_access_denied", response.text
    assert "COMPANY_SOURCE_MARKER" not in response.text


async def test_download_manifest_quarantine_is_live_between_copies(native_workspace,  # noqa: F811
    owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services import content_input_receipts as receipts

    n = await _graph(native_workspace, 4, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    first = n.receipts[0]
    async with owner_sessionmaker() as db:
        copy = ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind=first.carrier_kind,
            carrier_ref=first.carrier_ref, revision=first.revision + 1, content_sha256=first.content_sha256,
            dependencies_json=first.dependencies_json, authority_json=first.authority_json)
        db.add(copy)
        (await db.get(ContentInputReceipt, n.receipts[-1].id)).dependencies_json = [_source(first), _source(copy)]
        await db.commit()
    allowed = await _browser(n, "download")
    assert allowed.status_code == 200 and allowed.content == BODY.encode(), allowed.text
    validate, revoked = receipts._validate_workspace_dependency, False
    visited = []

    async def quarantine_between_copies(db, tenant, dependency, **context):
        nonlocal revoked
        visited.append(dependency["source_id"])
        await validate(db, tenant, dependency, **context)
        if dependency == _source(first) and not revoked:
            async with owner_sessionmaker() as writer:
                manifest = await writer.scalar(select(WorkspaceResourceManifest).where(
                    WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == "workspace/graph-0.md"))
                manifest.authority_state = "quarantined"
                await writer.commit()
            revoked = True

    monkeypatch.setattr(receipts, "_validate_workspace_dependency", quarantine_between_copies)
    response = await _browser(n, "download")
    assert revoked and visited == [str(first.id), str(copy.id)], visited
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "content_source_access_denied", response.text
    assert "COMPANY_SOURCE_MARKER" not in response.text


@pytest.mark.parametrize("kind,code,status", [
    ("workspace_artifact", "content_source_unavailable", 503),
    ("explicit_memory", "explicit_memory_source_cycle", 409),
    ("team_memory", "team_memory_source_unverified", 403),
    ("project_resource", "project_source_cycle", 503),
    ("trigger_write", "trigger_context_access_denied", 403),
    ("trigger_snapshot", "trigger_context_access_denied", 403),
    ("native_trigger_context", "native_trigger_context_changed", 403),
])
async def test_source_back_edge_preserves_each_kind_error(native_workspace, owner_sessionmaker,  # noqa: F811
    app_user_sessionmaker, monkeypatch, kind, code, status):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.user import User
    from app.services import content_input_receipts as receipts
    from app.services import native_trigger_context, project_context, trigger_context_sources
    from app.services.explicit_memory_authority import AUTHORITY_SCHEMA

    n, identity = native_workspace, uuid.uuid4()
    source = {"source_kind": kind, "source_id": str(identity), "source_version": "f" * 64}
    async with owner_sessionmaker() as db:
        if kind in {"workspace_artifact", "explicit_memory", "team_memory"}:
            carrier_ref = (f"business_team:{uuid.uuid4()}\ncycle" if kind == "team_memory"
                else f"{n.agent}\nworkspace/cycle.md")
            db.add(ContentInputReceipt(id=identity, tenant_id=n.tenant, carrier_kind=kind,
                carrier_ref=carrier_ref, revision=1, content_sha256=source["source_version"],
                dependencies_json=[source], authority_json={"schema": AUTHORITY_SCHEMA, "state": "active",
                    "tenant_id": str(n.tenant), "agent_id": str(n.agent), "audience_kind": "private",
                    "owner_user_id": str(n.user), "source_session_id": str(n.session)}))
            await db.commit()
    batch, entered = receipts._validate_input_dependency_batch, False
    active = {"project_resource": project_context._ACTIVE_RECEIPTS,
        "trigger_write": trigger_context_sources._ACTIVE, "trigger_snapshot": trigger_context_sources._ACTIVE,
        "native_trigger_context": native_trigger_context._ACTIVE}.get(kind)

    async def enter_back_edge(db, tenant, dependencies, **context):
        nonlocal entered
        if entered:
            return await batch(db, tenant, dependencies, **context)
        entered = True
        # Isolate the dispatcher boundary at a back-edge. The common walker
        # establishes visiting; the real kind validator classifies re-entry.
        token = active.set(active.get() | {identity}) if active is not None else None
        try:
            await receipts._validate_input_dependencies(db, tenant, dependencies,
                **{**context, "active_workspace_receipts": frozenset({identity})})
        finally:
            if active is not None:
                active.reset(token)

    monkeypatch.setattr(receipts, "_validate_input_dependency_batch", enter_back_edge)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        with receipts.group_source_read() as scope:
            with pytest.raises(receipts.ContentInputReceiptError) as denied:
                await receipts._validate_input_dependencies(db, n.tenant, [source], user=user)
            assert (denied.value.code, denied.value.status_code) == (code, status)
            if kind == "workspace_artifact":
                assert str(denied.value) == "The workspace source dependency chain is cyclic."
            causes = {"team_memory": "team_memory_receipt_unavailable",
                "trigger_write": "cyclic_trigger_write", "trigger_snapshot": "cyclic_trigger_sources",
                "native_trigger_context": "cyclic_context_source"}
            if kind in causes:
                # The team row and carrier are structurally valid; only the
                # active id can enter its unavailable-row branch. The other
                # kinds must preserve the cycle cause, not an invalid-row one.
                assert str(denied.value.__cause__) == causes[kind]
            assert not scope.verified and not getattr(scope, "visiting", ())
            assert not getattr(scope, "revisiting", ())


async def test_unclassified_model_cycle_fails_without_recursing_forever(native_workspace,  # noqa: F811
    owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.user import User
    from app.services.content_input_receipts import ContentInputReceiptError, validate_browser_input_dependencies
    from app.services.learned_skill_sources import model_source

    n = await _graph(native_workspace, 4, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    async with owner_sessionmaker() as db:
        model = await db.get(SessionModelResult, n.model)
        source = model_source(model)
        (await db.get(ContentInputReceipt, n.receipts[0].id)).dependencies_json = [source]
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        with pytest.raises(ContentInputReceiptError) as denied:
            await asyncio.wait_for(validate_browser_input_dependencies(db, n.tenant, user, [source]), TIMEOUT)
        assert (denied.value.code, denied.value.status_code) == ("content_source_unavailable", 503)


async def test_t2_omits_cyclic_team_event_and_keeps_its_sibling(readable_team_memory,  # noqa: F811
    owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.memory.t0.ledger import replay_t0_session_events, seal_t0_session_segment
    from app.services import content_input_receipts as receipts
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.explicit_memory_authority import latest_memory_receipt, source_dependency
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.services.session_model_round import prepare_model_request

    s, _workspace, receipt = readable_team_memory
    n = s.n
    bad, good = "CYCLIC_TEAM_EVENT_MARKER", "SAFE_SIBLING_EVENT_MARKER"
    team_source = {"source_kind": "team_memory", "source_id": str(receipt.id), "source_version": receipt.content_sha256}
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        private = source_dependency(await latest_memory_receipt(db, n.tenant, n.agent, s.entry_id))
    for index, content, source in [(10, bad, team_source), (11, good, private)]:
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            message = LLMMessage(role="system", content=content, context_origin=ContextMessageOrigin.capture(
                kind="history", role="system", content=content, sources=[source], unbound=()))
            request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=index, messages=[message], tools=[],
                provider="synthetic", model="local-reference")
            await _seal_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, run_id=n.run,
                turn_id=f"turn-{n.run.hex}", round_index=index, provider_request_id=request, content=content)
            model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
            transcript = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.result_id == model.id,
                ChatTranscriptEvent.item_kind == "assistant_text"))
            transcript_id = transcript.id
            await db.commit()
        assert await bridge_transcript_event_to_t0(transcript_event_id=transcript_id)
    assert seal_t0_session_segment(agent_id=n.agent, session_id=n.session, reason="turn_stop")
    events = replay_t0_session_events(agent_id=n.agent, session_id=n.session, data_root=n.root.parent)
    n.segment = next(event.segment_id for event in events if event.content == bad)
    assert next(event.segment_id for event in events if event.content == good) == n.segment
    before = await _heartbeat(n)
    assert before is not None and all(marker in json.dumps(before) for marker in (bad, good))
    async with owner_sessionmaker() as db:
        (await db.get(type(receipt), receipt.id)).dependencies_json = [team_source]
        await db.commit()
    validate, codes = receipts._validate_input_dependencies, []

    async def observe_denial(*args, **kwargs):
        try:
            return await validate(*args, **kwargs)
        except ContentInputReceiptError as exc:
            codes.append((exc.code, exc.status_code))
            raise

    monkeypatch.setattr(receipts, "_validate_input_dependencies", observe_denial)
    result = await _heartbeat(n)
    assert codes and set(codes) == {("team_memory_source_unverified", 403)}, codes
    assert result is not None and good in json.dumps(result) and bad not in json.dumps(result), result


async def test_t2_model_trigger_back_edge_keeps_its_class_and_sibling(native_workspace,  # noqa: F811
    owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from datetime import datetime, timezone

    from app.memory.t0.ledger import replay_t0_session_events, seal_t0_session_segment
    from app.models.invocation_span import InvocationSpan
    from app.services import content_input_receipts as receipts, trigger_context_sources
    from app.services.learned_skill_sources import model_source
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.services.session_model_inputs import _hash
    from app.services.session_model_round import prepare_model_request

    n = await _graph(native_workspace, 4, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    bad, good = "MODEL_TRIGGER_CYCLE_MARKER", "MODEL_TRIGGER_SAFE_SIBLING"
    async with owner_sessionmaker() as db:
        invocation = await db.get(SessionToolInvocation, n.newest_read.id)
        # TW's previous-source inventory is a tool-origin pointer, independent
        # of the model-source hash. Inject its corrupt edge below while keeping
        # the real model, trigger validator, active check and T2 packaging path.
        proof = {"schema": "hive.trigger_write.v1", "invocation_id": str(invocation.id),
            "owner_user_id": str(n.user), "effective_args_hash": invocation.effective_args_hash,
            "previous_sources": {field: {"kind": "tool", "invocation_id": str(invocation.id)}
                for field in ("name", "type", "config", "reason")}}
        identity = uuid.uuid5(invocation.id, "trigger-write-evidence")
        db.add(InvocationSpan(id=identity, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=n.run, trace_id=f"r12a:{identity}",
            span_id=f"trigger-write:{identity}", span_type="trigger_write", name="trigger_write",
            status="captured", duration_ms=0, started_at=datetime.now(timezone.utc), metadata_json={"write": proof}))
        trigger_source = {"source_kind": "trigger_write", "source_id": str(identity), "source_version": _hash(proof)}
        model = await db.get(SessionModelResult, n.model)
        snapshot = deepcopy(model.model_request_snapshot_json)
        snapshot["input_bindings"]["dependencies"] = [trigger_source]
        model.model_request_snapshot_json, model.model_request_hash = snapshot, _hash(snapshot)
        source = model_source(model)
        await db.commit()
    cyclic = False
    resolve_origin = trigger_context_sources._origin_dependencies

    async def original_sources(db, *, tenant_id, agent_id, ref):
        if ref == f"session-tool-result:{n.newest_read.id}":
            assert tenant_id == n.tenant and agent_id == n.agent
            return ([source] if cyclic else []), True
        return await resolve_origin(db, tenant_id=tenant_id, agent_id=agent_id, ref=ref)

    monkeypatch.setattr(trigger_context_sources, "_origin_dependencies", original_sources)
    for index, content, dependency in [(5, bad, source), (6, good, _source(n.receipts[0]))]:
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            message = LLMMessage(role="system", content=content, context_origin=ContextMessageOrigin.capture(
                kind="history", role="system", content=content, sources=[dependency], unbound=()))
            request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=index, messages=[message], tools=[],
                provider="synthetic", model="local-reference")
            await _seal_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, run_id=n.run,
                turn_id=f"turn-{n.run.hex}", round_index=index, provider_request_id=request, content=content)
            model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
            transcript = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.result_id == model.id,
                ChatTranscriptEvent.item_kind == "assistant_text"))
            transcript_id = transcript.id
            await db.commit()
        assert await bridge_transcript_event_to_t0(transcript_event_id=transcript_id)
    assert seal_t0_session_segment(agent_id=n.agent, session_id=n.session, reason="turn_stop")
    events = replay_t0_session_events(agent_id=n.agent, session_id=n.session, data_root=n.root.parent)
    n.segment = next(event.segment_id for event in events if event.content == bad)
    assert next(event.segment_id for event in events if event.content == good) == n.segment
    before = await _heartbeat(n)
    assert before is not None and all(marker in json.dumps(before) for marker in (bad, good))
    cyclic = True
    validate, codes = receipts._validate_input_dependencies, []

    async def observe_denial(*args, **kwargs):
        try:
            return await validate(*args, **kwargs)
        except receipts.ContentInputReceiptError as exc:
            codes.append((exc.code, exc.status_code, str(exc.__cause__)))
            raise

    monkeypatch.setattr(receipts, "_validate_input_dependencies", observe_denial)
    result = await asyncio.wait_for(_heartbeat(n), TIMEOUT)
    assert codes and set(codes) == {("trigger_context_access_denied", 403, "cyclic_trigger_write")}, codes
    assert result is not None and good in json.dumps(result) and bad not in json.dumps(result), result


async def test_loop_command_back_edge_reaches_explicit_memory_cycle(shared_memory,  # noqa: F811
    owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.user import User
    from app.services import trigger_instruction_sources
    from app.services.content_input_receipts import ContentInputReceiptError, validate_browser_input_dependencies
    from app.services.explicit_memory_authority import latest_memory_receipt, source_dependency
    from app.services.session_model_inputs import _hash

    s, n = shared_memory, shared_memory.n
    async with owner_sessionmaker() as db:
        memory = await latest_memory_receipt(db, n.tenant, n.agent, s.entry_id)
        explicit = source_dependency(memory)
        command = await db.scalar(select(SessionCommand).where(
            SessionCommand.session_id == n.session, SessionCommand.namespace == "human_input"))
        command.target_json = {**command.target_json, "runtime_metadata": {"source": "loop_same_session"}}
        command.target_hash = _hash(command.target_json)
        source = {"source_kind": "session_command", "source_id": str(command.id), "source_version": command.request_hash}
        (await db.get(ContentInputReceipt, memory.id)).dependencies_json = [source]
        await db.commit()
    extracted = []

    async def cyclic_original_sources(db, *, tenant_id, agent_id, row, command):
        # Inject the corrupt edge at the scheduled-origin inventory boundary;
        # the real command validation must pass its active set on re-entry.
        assert tenant_id == n.tenant and agent_id == n.agent and row.command_id == command.id
        assert command.target_json["runtime_metadata"]["source"] == "loop_same_session"
        extracted.append(str(command.id))
        return [explicit], True

    monkeypatch.setattr(trigger_instruction_sources, "native_trigger_input_sources", cyclic_original_sources)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        with pytest.raises(ContentInputReceiptError) as denied:
            await asyncio.wait_for(validate_browser_input_dependencies(db, n.tenant, user, [source]), TIMEOUT)
        assert (denied.value.code, denied.value.status_code) == ("explicit_memory_source_cycle", 409)
        assert str(denied.value) == "The memory source chain is cyclic."
        assert extracted == [source["source_id"], source["source_id"]]
