"""Native command preflight stays bounded as retained conversation history grows."""

import hashlib
from contextvars import ContextVar
import platform
import shlex
import time
import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.hr_creation import HrCreationDraft
from app.models.organization_identity import ContentInputReceipt
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionModelResult, SessionToolInvocation
from app.services.learned_skill_sources import model_source
from app.services.session_model_inputs import _hash
from app.services.workspace_resource_authority import register_workspace_path
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"),
    pytest.mark.skipif(platform.system() != "Darwin", reason="Requires the native macOS command sandbox.")]

PREFLIGHT_STATEMENT_BOUND = 130


async def add_history(n, factory, sessions=50):
    """Seed immutable model/source records and grow the same twelve current files."""
    n.history_sources = getattr(n, "history_sources", [])
    n.history_revision = getattr(n, "history_revision", 0) + 1
    async with factory() as db:
        for index in range(sessions):
            session_id, run_id = uuid.uuid4(), uuid.uuid4()
            session = ChatSession(id=session_id, tenant_id=n.tenant, agent_id=n.agent,
                user_id=n.user, title=f"Prior work {index}")
            db.add(session)
            await db.flush()
            delegated = index == 0
            db.add(RuntimeTask(id=run_id, tenant_id=n.tenant,
                task_type="delegation" if delegated else "web_chat_turn", status="completed",
                parent_agent_id=n.agent, child_agent_id=n.agent, root_user_id=n.user,
                parent_session_id=str(n.session if delegated else session_id), child_session_id=str(session_id),
                prompt="Prior authorized work"))
            await db.flush()
            if delegated:
                session.session_kind, session.runtime_task_id = "delegation_run", run_id
                session.peer_agent_id, session.parent_session_id = n.agent, n.session
            await db.flush()
            for round_index in range(4):
                result_id, command_id = uuid.uuid4(), uuid.uuid4()
                request = {"content": f"Prior authorized request {index}/{round_index}"}
                version = _hash({"command_kind": "start_turn", "request": request})
                db.add(SessionCommand(id=command_id, tenant_id=n.tenant, session_id=session_id,
                    principal_type="user", principal_id=n.user, namespace="human_input",
                    idempotency_key=str(command_id), command_kind="start_turn", request_json=request, target_json={},
                    request_hash=version, target_hash=_hash({}), status="applied",
                    receipt_ref=f"session-command:{command_id}"))
                snapshot = {"input_bindings": {"schema": "hive.model_input_bindings.v1", "dependencies": [
                    {"source_kind": "session_command", "source_id": command_id.hex if round_index == 0 else str(command_id),
                        "source_version": version}, *([n.history_identity] if hasattr(n, "history_identity") else [])],
                    "dependency_coverage": {"complete": True}, "messages": []}, "wire_request": {"messages": []}}
                provider_request = f"history:{result_id}"
                content = f"Prior result {index}/{round_index}"
                model = SessionModelResult(id=result_id, tenant_id=n.tenant, session_id=session_id, run_id=run_id,
                    turn_id=f"turn-{run_id.hex}", round_id=f"{run_id}:round:{round_index}",
                    provider_request_id=provider_request, state="round_committed", model_request_hash=_hash(snapshot),
                    model_request_snapshot_json=snapshot, seal_json={"result_id": str(result_id), "run_id": str(run_id),
                        "provider_request_id": provider_request, "semantic_content": content, "content_hash": _hash(content)})
                db.add(model)
                n.history_sources.append(model_source(model))
                for call_index in range(2 if round_index == 0 else 1):
                    call = f"history-call:{result_id}:{call_index}"
                    tool = "write_file" if round_index == 0 else "read_file"
                    arguments = {"path": f"workspace/report-{index % 12}.txt", "content": content}
                    db.add(SessionToolInvocation(id=uuid.uuid4(), tenant_id=n.tenant, session_id=session_id, run_id=run_id,
                        round_id=model.round_id, provider_request_id=provider_request, provider_tool_use_id=call,
                        tool_name=tool, provider_arguments_json=arguments, invocation_item_id=uuid.uuid4(),
                        args_hash=_hash({"tool_name": tool, "arguments": arguments}), authority_snapshot_hash="a" * 64,
                        effect_idempotency_key=call, effect_state="effect_committed", receipt_ref=f"fixture:{call}"))
            if index < 3:
                db.add(HrCreationDraft(tenant_id=n.tenant, hr_agent_id=n.agent, session_id=session_id,
                    requested_by_user_id=n.user, status="rejected", blueprint_hash=_hash({}), blueprint_json={}))
        for index in range(12):
            path, content = f"workspace/report-{index}.txt", f"History report {index}\n"
            (n.root / path).write_text(content)
            digest = hashlib.sha256(content.encode()).hexdigest()
            await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                owner_user_id=n.user, root_session_id=n.session, source="fixture", content_hash=digest)
            prior = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == n.tenant,
                ContentInputReceipt.carrier_kind == "workspace_artifact",
                ContentInputReceipt.carrier_ref == f"{n.agent}\n{path}").order_by(ContentInputReceipt.revision.desc()).limit(1))
            from app.services.session_model_inputs import _unique_dependencies

            db.add(ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="workspace_artifact",
                carrier_ref=f"{n.agent}\n{path}", revision=prior.revision + 1 if prior else 1, content_sha256=digest,
                dependencies_json=_unique_dependencies([*(prior.dependencies_json if prior else []),
                    *n.history_sources[index::12]])))
        await db.commit()


@pytest.fixture
async def history_workspace(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch, request):  # noqa: F811
    from app.memory.write_gate import MemoryWriteDecision

    n = native_workspace
    if getattr(request, "param", False):
        n.history_identity = await identity_source(n, owner_sessionmaker)
    async with owner_sessionmaker() as db:
        for capability in ("workspace.command.execute", "workspace.code.execute", "agent.memory.write", "agent.memory.read"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setenv("HIVE_CODE_EXEC_NETWORK_POLICY", "deny-all")

    async def reference_review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "controlled_reference_reviewer"})

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reference_review)
    await add_history(n, owner_sessionmaker)
    for index in range(3):
        saved, _ = await memory_call(n, app_user_sessionmaker, "save_memory",
            {"content": f"Remember project reference {index}.", "category": "project"})
        assert "Saved memory [project]" in saved, saved
    # The retained assistant has already executed commands; compare the same
    # initialized skill catalog at both history sizes.
    prior, _ = await memory_call(n, app_user_sessionmaker, "run_command", {"command": "printf PRIOR_COMMAND_OK"})
    assert "PRIOR_COMMAND_OK" in prior, prior
    prior, _ = await memory_call(n, app_user_sessionmaker, "execute_code", {"language": "python", "code": "print('PRIOR_CODE_OK')"})
    assert "PRIOR_CODE_OK" in prior, prior
    return n


async def identity_source(n, factory):
    from app.models.agent import Agent
    from app.services.agent_context import agent_identity_content
    from app.services.config_versioning import canonical_content_hash

    async with factory() as db:
        agent = await db.get(Agent, n.agent)
        return {"source_kind": "agent_identity", "source_id": f"{n.agent}\nidentity",
            "source_version": canonical_content_hash(agent_identity_content(agent.name, agent.role_description))}


@pytest.fixture
def command_probe(history_workspace, monkeypatch):
    from app.services.code_execution import service

    n, samples = history_workspace, []
    current = ContextVar("measured_command", default=None)
    execute, provider = n.service.execute, service._execute_selected_command

    async def at_provider(*args, **kwargs):
        active = current.get()
        active.update(preflight_statements=active["statements"], preflight_seconds=time.perf_counter() - active["start"])
        result = await provider(*args, **kwargs)
        active["provider_evidence"] = result.evidence
        return result

    async def measured(_service, *args, **kwargs):
        # Include the same fixed policy reads even if the 15-second lookup
        # cache expires while a larger history fixture is being prepared.
        _service.governance_resolver.clear_lookup_cache()
        active = {"start": time.perf_counter(), "statements": 0}
        token = current.set(active)

        def count(*_args):
            if current.get() is active:
                active["statements"] += 1

        event.listen(Engine, "before_cursor_execute", count)
        try:
            return await execute(*args, **kwargs)
        finally:
            event.remove(Engine, "before_cursor_execute", count)
            samples.append({**active, "tool_seconds": time.perf_counter() - active["start"]})
            current.reset(token)

    monkeypatch.setattr(type(n.service), "execute", measured)
    monkeypatch.setattr(service, "_execute_selected_command", at_provider)
    return samples


def command_arguments(tool):
    code = ("from pathlib import Path\nprint('R5A_COMMAND_OK')\n"
        "for path in sorted(Path('.').glob('report-*.txt')):\n print(path.read_text())\n")
    return {"language": "python", "code": code} if tool == "execute_code" else {
        "command": "python3 -c " + shlex.quote(code)}


@pytest.mark.parametrize("tool", ["run_command", "execute_code"])
async def test_command_preflight_is_constant_at_double_history(
    history_workspace, command_probe, owner_sessionmaker, app_user_sessionmaker, tool,
):
    n, counts = history_workspace, []
    for scale in (1, 2):
        if scale == 2:
            await add_history(n, owner_sessionmaker)
        raw, _ = await memory_call(n, app_user_sessionmaker, tool, command_arguments(tool))
        assert "R5A_COMMAND_OK" in raw and "PRIVATE_FOREIGN_FILE_BODY" not in raw, raw
        for index in range(12):
            assert f"History report {index}\n" in raw, raw
        sample = command_probe[-1]
        assert sample["provider_evidence"]["isolation"] == "sandbox-exec"
        assert sample["provider_evidence"]["network_policy"] == "deny-all"
        counts.append(sample["preflight_statements"])
    assert counts[0] == counts[1], counts
    assert max(counts) <= PREFLIGHT_STATEMENT_BOUND, counts


@pytest.mark.parametrize("history_workspace", [True], indirect=True)
async def test_identity_bearing_history_preflight_is_constant(
    history_workspace, command_probe, owner_sessionmaker, app_user_sessionmaker,
):
    n, counts = history_workspace, []
    for scale in (1, 2):
        if scale == 2:
            await add_history(n, owner_sessionmaker)
        raw, _ = await memory_call(n, app_user_sessionmaker, "run_command", command_arguments("run_command"))
        assert all(f"History report {i}\n" in raw for i in range(12)), raw
        assert "PRIVATE_FOREIGN_FILE_BODY" not in raw, raw
        counts.append(command_probe[-1]["preflight_statements"])
    print("IDENTITY_HISTORY_COUNTS", counts)
    assert counts[0] == counts[1], counts
    assert max(counts) <= PREFLIGHT_STATEMENT_BOUND, counts


async def test_retained_identity_models_preflight_is_constant(
    history_workspace, command_probe, owner_sessionmaker, app_user_sessionmaker,
):
    n, sources, counts = history_workspace, [], []
    identity = await identity_source(n, owner_sessionmaker)
    path, body = "workspace/report-identity.txt", "Identity history report\n"
    (n.root / path).write_text(body)
    digest = hashlib.sha256(body.encode()).hexdigest()
    for revision in range(1, 4):
        async with owner_sessionmaker() as db:
            session_id, run_id = uuid.uuid4(), uuid.uuid4()
            db.add(ChatSession(id=session_id, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, title="Identity work"))
            await db.flush()
            db.add(RuntimeTask(id=run_id, tenant_id=n.tenant, task_type="web_chat_turn", status="completed",
                parent_agent_id=n.agent, child_agent_id=n.agent, root_user_id=n.user,
                parent_session_id=str(session_id), child_session_id=str(session_id), prompt="Identity work"))
            await db.flush()
            for index in range(12):
                result_id = uuid.uuid4()
                snapshot = {"input_bindings": {"schema": "hive.model_input_bindings.v1", "dependencies": [identity],
                    "dependency_coverage": {"complete": True}, "messages": []}, "wire_request": {"messages": []}}
                provider_request, content = f"identity:{result_id}", f"Identity result {index}"
                model = SessionModelResult(id=result_id, tenant_id=n.tenant, session_id=session_id, run_id=run_id,
                    turn_id=f"turn-{run_id.hex}", round_id=f"{run_id}:round:{index}", provider_request_id=provider_request,
                    state="round_committed", model_request_hash=_hash(snapshot), model_request_snapshot_json=snapshot,
                    seal_json={"result_id": str(result_id), "run_id": str(run_id), "provider_request_id": provider_request,
                        "semantic_content": content, "content_hash": _hash(content)})
                db.add(model)
                sources.append(model_source(model))
            await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                owner_user_id=n.user, root_session_id=n.session, source="fixture", content_hash=digest)
            db.add(ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="workspace_artifact",
                carrier_ref=f"{n.agent}\n{path}", revision=revision, content_sha256=digest, dependencies_json=list(sources)))
            await db.commit()
        raw, _ = await memory_call(n, app_user_sessionmaker, "run_command", command_arguments("run_command"))
        assert body in raw and "PRIVATE_FOREIGN_FILE_BODY" not in raw, raw
        counts.append(command_probe[-1]["preflight_statements"])
    print("RETAINED_IDENTITY_COUNTS", counts)
    assert len(set(counts)) == 1, counts
    assert max(counts) <= PREFLIGHT_STATEMENT_BOUND, counts


async def add_command_sources(n, app_factory, count):
    from app.database import pin_rls_tenant_context
    from app.services.session_model_inputs import _origin_dependencies

    sources = []
    for index in range(count):
        tool = "execute_code" if index % 2 else "run_command"
        arguments = {"language": "python", "code": "print('SOURCE_OK')"} if tool == "execute_code" else {
            "command": "printf SOURCE_OK"}
        raw, invocation = await memory_call(n, app_factory, tool, arguments)
        assert "SOURCE_OK" in raw, raw
        async with app_factory() as db:
            await pin_rls_tenant_context(db, n.tenant)
            dependencies, complete = await _origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
                ref=f"session-tool-result:{invocation.id}")
        assert complete, dependencies
        own = [d for d in dependencies if d["source_kind"] == "session_tool_invocation"
            and d["source_id"] == str(invocation.id)]
        assert len(own) == 1
        sources.extend(own)
    return sources


@pytest.fixture
async def lab_history_workspace(history_workspace, app_user_sessionmaker):
    from app.database import pin_rls_tenant_context
    from app.services.llm_client import LLMMessage
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
        _admitted_write_messages, _prepare_write,
    )

    n = history_workspace
    await add_command_sources(n, app_user_sessionmaker, 13)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
    prior = [LLMMessage(role=m.role, content=m.content, tool_calls=m.tool_calls,
        tool_call_id=m.tool_call_id, source_ref=m.id) for m in history.messages]
    # One actual native producer writes twelve successor receipts, retaining
    # prior file sources and historical command/code tool inputs from the model.
    code = ("from pathlib import Path\nfor i in range(12):\n"
        " Path(f'report-{i}.txt').write_text(f'History report {i}\\nNative successor\\n')\n"
        "print('LAB_RECEIPTS_OK')")
    round_index = n.memory_rounds[str(n.run)] + 1
    n.memory_rounds[str(n.run)] = round_index
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        admitted = await _admitted_write_messages(db, n, round_index=round_index)
        invocation, arguments, turn = await _prepare_write(db, n, messages=[*prior, *admitted], tool_name="run_command",
            arguments={"command": "python3 -c " + shlex.quote(code)}, round_index=round_index)
    trace = {}
    raw = await n.service.execute("run_command", arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
        tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
    assert "LAB_RECEIPTS_OK" in str(raw), raw
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": "run_command", "args": arguments, "status": "done", "result": str(raw),
        "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        latest = list(await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == n.tenant,
            ContentInputReceipt.carrier_kind == "workspace_artifact").distinct(ContentInputReceipt.carrier_ref)
            .order_by(ContentInputReceipt.carrier_ref, ContentInputReceipt.revision.desc())))
        assert len(latest) == 12
        assert all(r.id == uuid.uuid5(invocation.id, f"workspace-content:{r.carrier_ref.split(chr(10), 1)[1]}")
            for r in latest)
        assert all(len([d for d in r.dependencies_json if d["source_kind"] == "session_tool_invocation"]) >= 13
            for r in latest)
    return n


async def test_command_preflight_batches_lab_receipts_at_double_history(
    lab_history_workspace, command_probe, owner_sessionmaker, app_user_sessionmaker,
):
    n, counts = lab_history_workspace, []
    for scale in (1, 2):
        if scale == 2:
            await add_history(n, owner_sessionmaker)
        raw, _ = await memory_call(n, app_user_sessionmaker, "run_command", command_arguments("run_command"))
        assert all(f"History report {i}\n" in raw for i in range(12)), raw
        counts.append(command_probe[-1]["preflight_statements"])
    print("LAB_HISTORY_COUNTS", counts)
    assert counts[0] == counts[1], counts
    assert max(counts) <= PREFLIGHT_STATEMENT_BOUND, counts


async def test_command_preflight_batches_real_tool_sources(
    history_workspace, command_probe, owner_sessionmaker, app_user_sessionmaker,
):
    n, sources, counts = history_workspace, [], []
    path, content = "workspace/report-probe.txt", "Real command source report\n"
    (n.root / path).write_text(content)
    digest = hashlib.sha256(content.encode()).hexdigest()
    for revision, extra in enumerate((1, 4, 8), 1):
        sources.extend(await add_command_sources(n, app_user_sessionmaker, extra))
        async with owner_sessionmaker() as db:
            await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                owner_user_id=n.user, root_session_id=n.session, source="fixture", content_hash=digest)
            db.add(ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="workspace_artifact",
                carrier_ref=f"{n.agent}\n{path}", revision=revision, content_sha256=digest,
                dependencies_json=list(sources)))
            await db.commit()
        raw, _ = await memory_call(n, app_user_sessionmaker, "run_command", command_arguments("run_command"))
        assert content in raw and "PRIVATE_FOREIGN_FILE_BODY" not in raw, raw
        counts.append(command_probe[-1]["preflight_statements"])
    print("TOOL_SOURCE_COUNTS", counts)
    assert len(set(counts)) == 1, counts
    assert max(counts) <= PREFLIGHT_STATEMENT_BOUND, counts


async def test_command_preflight_batches_current_files(
    history_workspace, command_probe, owner_sessionmaker, app_user_sessionmaker,
):
    n, counts = history_workspace, []
    for scale in (1, 2):
        if scale == 2:
            async with owner_sessionmaker() as db:
                for index in range(12, 24):
                    path, content = f"workspace/report-{index}.txt", f"History report {index}\n"
                    (n.root / path).write_text(content)
                    digest = hashlib.sha256(content.encode()).hexdigest()
                    await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                        owner_user_id=n.user, root_session_id=n.session, source="fixture", content_hash=digest)
                    db.add(ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="workspace_artifact",
                        carrier_ref=f"{n.agent}\n{path}", revision=1, content_sha256=digest,
                        dependencies_json=n.history_sources[index % 12::12]))
                await db.commit()
        raw, _ = await memory_call(n, app_user_sessionmaker, "run_command", command_arguments("run_command"))
        assert all(f"History report {i}\n" in raw for i in range(12 * scale)), raw
        counts.append(command_probe[-1]["preflight_statements"])
    print("FILE_COUNTS", counts)
    assert counts[0] == counts[1], counts
    assert max(counts) <= PREFLIGHT_STATEMENT_BOUND, counts


@pytest.mark.parametrize("change", ["model_withdrawn", "command_tampered"])
async def test_new_command_rechecks_history_sources_and_preserves_other_inputs(
    history_workspace, command_probe, owner_sessionmaker, app_user_sessionmaker, change,
):
    n = history_workspace
    arguments = command_arguments("run_command")
    before, _ = await memory_call(n, app_user_sessionmaker, "run_command", arguments)
    assert "History report 0\n" in before, before
    async with owner_sessionmaker() as db:
        source = await db.get(SessionModelResult, uuid.UUID(n.history_sources[0]["source_id"]))
        if change == "model_withdrawn":
            source.state = "failed"
        else:
            command = source.model_request_snapshot_json["input_bindings"]["dependencies"][0]
            row = await db.get(SessionCommand, uuid.UUID(command["source_id"]))
            row.request_json = {"content": "Changed after receipt capture"}
        await db.commit()
    after, _ = await memory_call(n, app_user_sessionmaker, "run_command", arguments)
    assert "R5A_COMMAND_OK" in after and "History report 0\n" not in after, after
    assert "History report 1\n" in after and "PRIVATE_FOREIGN_FILE_BODY" not in after, after
    assert len(command_probe) == 2 and all("provider_evidence" in sample for sample in command_probe)


@pytest.mark.parametrize("change", ["user_inactive", "permission_removed", "session_moved"])
async def test_next_preflight_rechecks_foreign_transaction_revocation(
    history_workspace, owner_sessionmaker, app_user_sessionmaker, change,
):
    from app.database import pin_rls_tenant_context
    from app.models.agent import Agent, AgentPermission
    from app.models.user import User
    from app.services.content_input_receipts import authorize_workspace_execution_inputs
    from app.services.workspace_resource_authority import load_workspace_authority_scope
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    n = history_workspace
    permission = AgentPermission(tenant_id=n.tenant, agent_id=n.agent, scope_type="user", scope_id=n.user)
    if change == "permission_removed":
        async with owner_sessionmaker() as db:
            (await db.get(Agent, n.agent)).creator_id = n.foreign
            db.add(permission)
            await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        actor = await db.get(User, n.user)
        scope = await load_workspace_authority_scope(db, actor, agent_id=n.agent, session_id=n.session)
    request = ToolExecutionRequest("run_command", {"command": "printf CHECK"}, ToolExecutionContext(
        agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant), session_id=str(n.session),
        runtime_task_id=str(n.run), workspace=n.root, workspace_authority_scope=scope))
    assert await authorize_workspace_execution_inputs(request) is None
    assert len(request.context.workspace_authority_scope.input_reads) == 12
    async with owner_sessionmaker() as writer:
        if change == "user_inactive":
            (await writer.get(User, n.user)).is_active = False
        elif change == "permission_removed":
            await writer.delete(await writer.get(AgentPermission, permission.id))
        else:
            (await writer.get(ChatSession, n.session)).user_id = n.foreign
        await writer.commit()
    request.context.workspace_authority_scope = scope
    denial = await authorize_workspace_execution_inputs(request)
    assert denial is not None or not request.context.workspace_authority_scope.input_reads


async def test_preflight_reuse_preserves_transitive_receipt_cycle_rejection(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.database import pin_rls_tenant_context
    from app.services.content_input_receipts import (
        ContentInputReceiptError, _validate_input_dependencies, group_source_read,
    )
    from app.services import content_input_receipts
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    n, dependencies, receipts = native_workspace, [], []
    async with owner_sessionmaker() as db:
        for index in range(2):
            path, content = f"workspace/cycle-{index}.txt", f"Cycle reference {index}"
            (n.root / path).write_text(content)
            digest = hashlib.sha256(content.encode()).hexdigest()
            await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                owner_user_id=n.user, root_session_id=n.session, source="fixture", content_hash=digest)
            receipt = ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="workspace_artifact",
                carrier_ref=f"{n.agent}\n{path}", revision=1, content_sha256=digest,
                dependencies_json=list(dependencies))
            db.add(receipt)
            receipts.append(receipt)
            dependencies.append({"source_kind": "workspace_artifact", "source_id": str(receipt.id),
                "source_version": digest})
        receipts[0].dependencies_json = [dependencies[1]]
        await db.commit()
    request = ToolExecutionRequest("run_command", {"command": "printf CHECK"}, ToolExecutionContext(
        agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant), session_id=str(n.session),
        runtime_task_id=str(n.run), workspace=n.root))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        with group_source_read(batch_inputs=True):
            if hasattr(content_input_receipts, "_preload_execution_sources"):
                await content_input_receipts._preload_execution_sources(db, n.tenant, dependencies)
            # Each real file's retained inputs reach the other file and return to itself.
            for dependency in dependencies:
                with pytest.raises(ContentInputReceiptError) as denied:
                    await _validate_input_dependencies(db, n.tenant, [dependency], runtime_request=request)
                assert (denied.value.code, denied.value.status_code) == ("content_source_unavailable", 503)


@pytest.mark.parametrize("reason", ["no_task", "delegation", "no_receipts"])
async def test_execution_skips_source_preload_when_no_receipt_needs_validation(
    history_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch, reason,
):
    from dataclasses import replace
    from app.database import pin_rls_tenant_context
    from app.models.user import User
    from app.services import content_input_receipts as receipts
    from app.services.workspace_resource_authority import load_workspace_authority_scope
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    n, calls = history_workspace, []
    async with owner_sessionmaker() as db:
        if reason == "delegation":
            (await db.get(RuntimeTask, n.run)).task_type = "delegation"
            await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        scope = await load_workspace_authority_scope(db, await db.get(User, n.user),
            agent_id=n.agent, session_id=n.session)
    if reason == "no_receipts":
        scope = replace(scope, allowed_paths=frozenset())

    async def preload(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(receipts, "_preload_execution_sources", preload)
    request = ToolExecutionRequest("run_command", {"command": "printf CHECK"}, ToolExecutionContext(
        agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant), session_id=str(n.session),
        runtime_task_id=None if reason == "no_task" else str(n.run),
        workspace=n.root, workspace_authority_scope=scope))
    assert await receipts.authorize_workspace_execution_inputs(request) is None
    reads = request.context.workspace_authority_scope.input_reads
    if reason == "no_receipts":
        assert reads == ()
    else:
        assert {f"workspace/report-{i}.txt" for i in range(12)} <= {read.path for read in reads}
        assert all(read.receipt_id is not None and read.revision > 0 for read in reads)
    assert calls == []


@pytest.mark.parametrize("change", ["caller_inactive", "owner_inactive", "tenant_inactive", "agent_inactive", "task_terminal"])
async def test_environment_checks_refresh_after_a_foreign_commit(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, change,  # noqa: F811
):
    from app.database import pin_rls_tenant_context
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.execution_environments import EnvironmentCommandScope, EnvironmentError
    from datetime import datetime, timezone

    n = native_workspace
    async with owner_sessionmaker() as writer:
        (await writer.get(Agent, n.agent)).owner_user_id = n.foreign
        await writer.commit()
    current = EnvironmentCommandScope(n.tenant, n.agent, n.user, "fresh-authority", n.run)
    async with app_user_sessionmaker() as inspecting:
        await pin_rls_tenant_context(inspecting, n.tenant)
        await current._authority(inspecting)
        async with owner_sessionmaker() as writer:
            if change in {"caller_inactive", "owner_inactive"}:
                (await writer.get(User, n.user if change == "caller_inactive" else n.foreign)).is_active = False
            elif change == "tenant_inactive":
                (await writer.get(Tenant, n.tenant)).is_active = False
            elif change == "agent_inactive":
                (await writer.get(Agent, n.agent)).deactivated_at = datetime.now(timezone.utc)
            else:
                (await writer.get(RuntimeTask, n.run)).status = "completed"
            await writer.commit()
        with pytest.raises(EnvironmentError) as denied:
            await current._authority(inspecting, before_command=True)
        assert denied.value.status == 403
        assert denied.value.code == ("environment_runtime_claim_unavailable" if change == "task_terminal"
            else "environment_authority_unavailable")
