"""Native command preflight stays bounded as retained conversation history grows."""

import hashlib
from contextvars import ContextVar
import platform
import shlex
import time
import uuid

import pytest
from sqlalchemy import event
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

PREFLIGHT_STATEMENT_BOUND = 300


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
                        "source_version": version}],
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
            db.add(ContentInputReceipt(id=uuid.uuid4(), tenant_id=n.tenant, carrier_kind="workspace_artifact",
                carrier_ref=f"{n.agent}\n{path}", revision=n.history_revision, content_sha256=digest,
                dependencies_json=n.history_sources[index::12]))
        await db.commit()


@pytest.fixture
async def history_workspace(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.memory.write_gate import MemoryWriteDecision

    n = native_workspace
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


async def test_disclosure_rechecks_the_runtime_reader_after_transaction_commit(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.database import pin_rls_tenant_context
    from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor, group_source_read
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    n = native_workspace
    request = ToolExecutionRequest("run_command", {"command": "printf CHECK"}, ToolExecutionContext(
        agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant), session_id=str(n.session),
        runtime_task_id=str(n.run), workspace=n.root))
    async with app_user_sessionmaker() as db:
        with group_source_read():
            await pin_rls_tenant_context(db, n.tenant)
            _, actor = await _runtime_input_actor(db, n.tenant, request)
            assert actor.id == n.user
            await db.commit()
            async with owner_sessionmaker() as writer:
                (await writer.get(ChatSession, n.session)).user_id = n.foreign
                await writer.commit()
            await pin_rls_tenant_context(db, n.tenant)
            with pytest.raises(ContentInputReceiptError) as revoked:
                await _runtime_input_actor(db, n.tenant, request)
            assert revoked.value.status_code == 403
            assert revoked.value.code == "content_source_access_denied"
