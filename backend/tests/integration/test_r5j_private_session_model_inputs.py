"""Private-session recovery reads reach the next real model round without leaves."""

import hashlib
import json
import uuid

import pytest
from sqlalchemy import delete, select, text

from app.database import pin_rls_tenant_context
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _admitted_write_messages,
    native_workspace,  # noqa: F401
)
from tests.integration.test_m1g38_session_recall_actor_postgres import _shared_reader
from tests.integration.test_m1g40_session_file_actor_postgres import _files
from tests.integration.test_unverified_t2_recovery_postgres import t2_work as _t2_work

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

READERS = [("read_file", None), ("read_document", None), ("fs_read", "text"), ("fs_read", "document")]


@pytest.fixture
async def t2_work(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox

    seed = await _t2_work.__wrapped__(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    try:
        yield seed
    finally:
        async with owner_sessionmaker() as db:
            await db.execute(delete(RuntimeTerminalBoundaryOutbox).where(
                RuntimeTerminalBoundaryOutbox.tenant_id == seed["tenant_id"]))
            await db.commit()


async def _read_next_round(n, path, *, tool, mode, leafless=False):
    from app.kernel.engine import _dicts_to_llm_messages, _llm_messages_to_dicts, _mid_run_items_to_user_messages
    from app.services.session_model_inputs import build_model_input_bindings
    from app.services.session_model_round import (
        bind_round_inputs, commit_sealed_model_round, prepare_model_request, seal_model_response,
    )
    from app.services.session_tool_runtime import prepare_tool_invocation
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.web_chat_runtime import _persist_tool_call, conversation_from_history_messages

    async with n.app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        assert tuple((await db.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ))).one()) == (False, False)
        task = await db.get(RuntimeTask, n.run)
        turn = task.metadata_json.get("turn_id") or f"turn-{n.run.hex}"
        if (await db.get(ChatSession, n.session)).audience_kind == "group":
            messages = _mid_run_items_to_user_messages(await bind_round_inputs(db, tenant_id=n.tenant,
                agent_id=n.agent, session_id=n.session, run_id=n.run, turn_id=turn, round_index=1))
        else:
            messages = await _admitted_write_messages(db, n, content="Read the original session record.")
        args = {"path": path, **({"mode": mode} if mode else {})}
        call = "recover-record"
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, messages=messages, tools=[],
            provider="openai", model="synthetic-no-provider")
        await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request,
            response={"content": None, "tool_calls": [{"id": call, "type": "function", "function": {
                "name": tool, "arguments": json.dumps(args)}}], "finish_reason": "tool_calls", "usage": {}})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request)
        invocation = await prepare_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, provider_request_id=request, provider_tool_use_id=call, tool_name=tool, arguments=args)
        await db.commit()
    trace = {}
    value = str(await n.service.execute(tool, args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), tool_call_id=invocation.provider_tool_use_id,
        trace_metadata_sink=trace, emit_runtime_hooks=False))
    if leafless:
        # Reproduce an ordinary read whose existing per-read capture is unavailable.
        trace["workspace_input_evidence"] = {"status": "unavailable", "reason_code": "fixture_capture_unavailable"}
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": tool, "args": args, "status": "done", "result": value,
        "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
    async with n.app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
        messages = _dicts_to_llm_messages(conversation_from_history_messages(history.messages))
        binding = await build_model_input_bindings(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, messages=messages, wire_messages=_llm_messages_to_dicts(messages))
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=2, messages=messages, tools=[],
            provider="openai", model="synthetic-no-provider")
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot = result.model_request_snapshot_json
        await db.commit()
    return value, binding, snapshot


@pytest.mark.parametrize("tool,mode", READERS)
@pytest.mark.parametrize("kind", ["t0", "t2", "legacy", "state"])
async def test_own_private_session_body_reaches_next_model_round(
    native_workspace, owner_sessionmaker, tool, mode, kind,  # noqa: F811
):
    n = native_workspace
    own, _foreign, paths = await _files(n, owner_sessionmaker, "legacy" if kind == "state" else kind)
    path = paths[own]
    if kind == "state":
        path = f"memory/session_state/{own}/snapshot.md"
        (n.root / path).parent.mkdir(parents=True)
        (n.root / path).write_text("OWN_PRIVATE_EVIDENCE")
    body = (n.root / path).read_text().rstrip("\n")
    value, binding, snapshot = await _read_next_round(n, path, tool=tool, mode=mode)
    read_content = json.loads(value)["content"]
    assert body in read_content
    entry = next(e for e in binding["messages"] if e.get("source_ref", "").startswith("session-tool-result:"))
    assert entry["dependency_status"] == "resolved", entry
    reads = [m for m in snapshot["wire_request"]["messages"] if m["role"] == "tool"]
    assert len(reads) == 1 and json.loads(reads[0]["content"])["content"] == read_content, snapshot
    assert snapshot["input_bindings"]["dependency_coverage"]["complete"] is True


@pytest.mark.parametrize("tool,mode", READERS)
@pytest.mark.parametrize("case", ["group_reader", "group_source", "other_member", "other_agent", "workspace"])
async def test_session_read_exception_does_not_deliver_unowned_or_leafless_files(
    native_workspace, owner_sessionmaker, monkeypatch, tool, mode, case,  # noqa: F811
):
    from app.models.agent import Agent

    n = native_workspace
    own, foreign, paths = await _files(n, owner_sessionmaker, "t0")
    path = paths[foreign if case == "other_member" else own]
    if case == "group_reader":
        await _shared_reader(n, owner_sessionmaker, monkeypatch)
    elif case in {"group_source", "other_agent"}:
        async with owner_sessionmaker() as db:
            source = await db.get(ChatSession, own)
            if case == "group_source":
                source.audience_kind = "group"
            else:
                other = Agent(id=uuid.uuid4(), tenant_id=n.tenant, creator_id=n.user, name="Other employee")
                db.add(other)
                await db.flush()
                source.agent_id = other.id
            await db.commit()
    elif case == "workspace":
        from app.services.workspace_resource_authority import register_workspace_path

        path = "workspace/without-source-leaf.md"
        (n.root / path).write_text("ORDINARY_LEAFLESS_FILE_BODY")
        async with owner_sessionmaker() as db:
            await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                owner_user_id=n.user, root_session_id=n.session, source="synthetic_legacy_work",
                content_hash=hashlib.sha256((n.root / path).read_bytes()).hexdigest())
            await db.commit()
    body = (n.root / path).read_text()
    value, binding, snapshot = await _read_next_round(n, path, tool=tool, mode=mode, leafless=case == "workspace")
    if case == "workspace":
        assert body in value, value
        entry = next(e for e in binding["messages"] if e.get("source_ref", "").startswith("session-tool-result:"))
        assert entry["dependency_status"] == "incomplete"
        assert any(e["code"] == "model_input_source_unbound" for e in snapshot["input_bindings"]["messages"])
    else:
        assert body not in value and "auth_or_permission" in value, value
    assert body not in json.dumps(snapshot["wire_request"], ensure_ascii=False)


async def test_unverified_t2_reference_read_delivers_complete_body(
    t2_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.runtime import invoker
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    s = t2_work
    path = s["package_path"] + "/summary.md"
    body = (s["agent_root"] / path).read_text()
    # This agent has a prior session, so its self-model is no longer the empty
    # bootstrap skeleton (profile_plane.py skips that); a non-empty resident
    # plane is what makes the prompt carry the memory section at all
    # (prompt_builder.py:578 renders it only for a non-empty memory_snapshot).
    self_path = s["agent_root"] / "memory/self/self.md"
    self_path.parent.mkdir(parents=True, exist_ok=True)
    self_path.write_text("# Self\n\nPrior sessions produced reviewed evidence packages.\n")
    # Owner decision 2026-11-12 (改为按需取): the per-turn preselection pointer
    # that used to name this record ("Inspect complete authorized files: …" from
    # the deleted memory_resource_references._render) is gone. The on-demand
    # route is the static Using-Memory-Tools section naming memory/t2/sessions/
    # (app/runtime/prompt_sections/memory.py), then a discovery call, then the
    # read. R5j keeps protecting the same two facts: the old body never reaches
    # the model as if current, and the authorized read delivers it complete.
    client = ScriptedLLMClient([
        {"tool_calls": [{"id": "locate-t2", "type": "function", "function": {
            "name": "list_files", "arguments": json.dumps({"path": "memory/t2/sessions"})}}],
            "finish_reason": "tool_calls"},
        {"tool_calls": [{"id": "descend-session", "type": "function", "function": {
            "name": "list_files", "arguments": json.dumps(
                {"path": f"memory/t2/sessions/{s['previous']}"})}}], "finish_reason": "tool_calls"},
        {"tool_calls": [{"id": "descend-segments", "type": "function", "function": {
            "name": "list_files", "arguments": json.dumps(
                {"path": f"memory/t2/sessions/{s['previous']}/segments"})}}], "finish_reason": "tool_calls"},
        {"tool_calls": [{"id": "recover-t2", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": path})}}], "finish_reason": "tool_calls"},
        {"content": "Read the complete original record.", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    fence = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    assert len(client.requests) == 5
    first = json.dumps(client.requests[0]["messages"], ensure_ascii=False)
    # The static route names the folder this record lives in, not the record.
    assert "memory/t2/sessions/" in first and "read_file" in first, first
    assert "Inspect complete authorized files" not in first
    assert "OLD_UNVERIFIED_T2_BODY" not in first
    locate = [m for m in client.requests[1]["messages"] if m.get("tool_call_id") == "locate-t2"]
    assert len(locate) == 1 and str(s["previous"]) in locate[0]["content"], locate
    descend_session = [m for m in client.requests[2]["messages"] if m.get("tool_call_id") == "descend-session"]
    assert len(descend_session) == 1 and "segments" in descend_session[0]["content"], descend_session
    descend_segments = [m for m in client.requests[3]["messages"] if m.get("tool_call_id") == "descend-segments"]
    assert len(descend_segments) == 1 and "seg-1" in descend_segments[0]["content"], descend_segments
    for request in client.requests[1:4]:
        assert "OLD_UNVERIFIED_T2_BODY" not in json.dumps(request["messages"], ensure_ascii=False)
    reads = [m for m in client.requests[4]["messages"] if m.get("tool_call_id") == "recover-t2"]
    assert len(reads) == 1 and body in json.loads(reads[0]["content"])["content"], client.requests[4]
