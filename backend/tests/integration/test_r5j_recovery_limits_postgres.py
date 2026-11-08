"""Recovery boundaries and the accepted R5j round-2 private-history limits."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _admitted_write_messages, native_workspace,  # noqa: F401
)
from tests.integration.test_m1g40_session_file_actor_postgres import _files
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4e_team_memory_authority_postgres import runtime_team  # noqa: F401
from tests.integration.test_m4f_team_memory_runtime_postgres import readable_team_memory, leave_team  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _history_messages(db, n):
    from app.kernel.engine import _dicts_to_llm_messages
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.web_chat_runtime import conversation_from_history_messages

    history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
    return _dicts_to_llm_messages(conversation_from_history_messages(history.messages))


async def _tool_round(n, round_index, tool, args, *, leafless=False, capture=None):
    from app.services.session_model_round import commit_sealed_model_round, prepare_model_request, seal_model_response
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import prepare_tool_invocation
    from app.services.web_chat_runtime import _persist_tool_call

    async with n.app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        task = await db.get(RuntimeTask, n.run)
        turn = task.metadata_json.get("turn_id") or f"turn-{n.run.hex}"
        if round_index == 1:
            messages = await _admitted_write_messages(db, n, content="Read the original session record.")
        else:
            messages = await _history_messages(db, n)
        call = f"recovery-call-{round_index}"
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=round_index, messages=messages, tools=[],
            provider="openai", model="synthetic-no-provider")
        await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=round_index, provider_request_id=request,
            response={"content": None, "tool_calls": [{"id": call, "type": "function", "function": {
                "name": tool, "arguments": json.dumps(args)}}], "finish_reason": "tool_calls", "usage": {}})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=round_index, provider_request_id=request)
        invocation = await prepare_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, provider_request_id=request, provider_tool_use_id=call, tool_name=tool, arguments=args)
        await db.commit()
    trace = {}
    value = str(await n.service.execute(tool, args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), tool_call_id=invocation.provider_tool_use_id,
        trace_metadata_sink=trace, emit_runtime_hooks=False))
    if leafless:
        trace["workspace_input_evidence"] = {"status": "unavailable", "reason_code": "fixture_capture_unavailable"}
    trace.update(capture or {})
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": tool, "args": args, "status": "done", "result": value,
        "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
    return value, invocation


async def _build(n, round_index):
    from app.kernel.engine import _llm_messages_to_dicts
    from app.services.session_model_inputs import build_model_input_bindings
    from app.services.session_model_round import prepare_model_request

    async with n.app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        task = await db.get(RuntimeTask, n.run)
        turn = task.metadata_json.get("turn_id") or f"turn-{n.run.hex}"
        messages = await _history_messages(db, n)
        binding = await build_model_input_bindings(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, messages=messages, wire_messages=_llm_messages_to_dicts(messages))
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=round_index, messages=messages, tools=[],
            provider="openai", model="synthetic-no-provider")
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot = result.model_request_snapshot_json
        await db.commit()
    return binding, snapshot


def _codes(snapshot):
    return [(e.get("source_ref"), e.get("status"), e.get("code")) for e in snapshot["input_bindings"]["messages"]]


def _assert_withheld(snapshot, marker):
    assert marker not in json.dumps(snapshot["wire_request"], ensure_ascii=False), snapshot
    assert any(entry["code"] == "model_input_source_unbound"
        for entry in snapshot["input_bindings"]["messages"]), _codes(snapshot)


@pytest.mark.parametrize("change", ["source_to_group", "source_reassigned", "source_deleted", "reader_to_group"])
async def test_private_recovery_rechecks_state_after_successful_read(
    native_workspace, owner_sessionmaker, change,  # noqa: F811
):
    n = native_workspace
    own, _foreign, paths = await _files(n, owner_sessionmaker, "t0")
    value, _invocation = await _tool_round(n, 1, "read_file", {"path": paths[own]})
    assert "OWN_PRIVATE_EVIDENCE" in value
    async with owner_sessionmaker() as db:
        source = await db.get(ChatSession, own)
        if change == "source_to_group":
            source.audience_kind = "group"
        elif change == "source_reassigned":
            source.user_id = n.foreign
        elif change == "source_deleted":
            await db.delete(source)
        else:
            (await db.get(ChatSession, n.session)).audience_kind = "group"
        await db.commit()
    if change == "reader_to_group":
        with pytest.raises(PermissionError, match="^shared_group_runtime_origin_mismatch$"):
            await _build(n, 2)
        async with owner_sessionmaker() as db:
            assert await db.scalar(select(SessionModelResult.id).where(
                SessionModelResult.run_id == n.run, SessionModelResult.round_id == f"{n.run}:round:2")) is None
    else:
        _binding, snapshot = await _build(n, 2)
        _assert_withheld(snapshot, "OWN_PRIVATE_EVIDENCE")


@pytest.mark.parametrize("kind", ["workspace", "skill", "skill_tampered"])
async def test_private_recovery_does_not_skip_an_invalid_leaf(
    native_workspace, owner_sessionmaker, kind,  # noqa: F811
):
    n = native_workspace
    own, _foreign, paths = await _files(n, owner_sessionmaker, "t0")
    value, invocation = await _tool_round(n, 1, "read_file", {"path": paths[own]},
        capture={f"{kind}_input_evidence": {"status": "unavailable", "reason_code": "fixture_invalid_leaf"}}
            if kind != "skill_tampered" else None)
    assert "OWN_PRIVATE_EVIDENCE" in value
    if kind == "skill_tampered":
        from app.models.invocation_span import InvocationSpan

        async with owner_sessionmaker() as db:
            span = await db.scalar(select(InvocationSpan).where(InvocationSpan.runtime_task_id == n.run,
                InvocationSpan.span_type == "skill_input",
                InvocationSpan.metadata_json["skill_input_evidence"]["invocation_id"].astext == str(invocation.id)))
            proof = span.metadata_json["skill_input_evidence"]
            assert proof["sources"] == [] and proof["unbound"] == []
            span.metadata_json = {"skill_input_evidence": {**proof, "tool_result_hash": "0" * 64}}
            await db.commit()
    _binding, snapshot = await _build(n, 2)
    _assert_withheld(snapshot, "OWN_PRIVATE_EVIDENCE")


@pytest.mark.parametrize("tamper", ["hash", "path"])
async def test_private_recovery_requires_the_stored_arguments_hash(
    native_workspace, owner_sessionmaker, tamper,  # noqa: F811
):
    n = native_workspace
    own, _foreign, paths = await _files(n, owner_sessionmaker, "t0")
    value, invocation = await _tool_round(n, 1, "read_file", {"path": paths[own]},
        capture={"skill_input_evidence": None})
    assert "OWN_PRIVATE_EVIDENCE" in value
    async with owner_sessionmaker() as db:
        row = await db.get(SessionToolInvocation, invocation.id)
        if tamper == "hash":
            row.effective_args_hash = "0" * 64
        else:
            row.effective_arguments_json = {**row.effective_arguments_json, "path": paths[own] + ".changed"}
        await db.commit()
    _binding, snapshot = await _build(n, 2)
    _assert_withheld(snapshot, "OWN_PRIVATE_EVIDENCE")


@pytest.mark.parametrize("mode", [None, False, 1, [], {}, ["text"]])
async def test_private_recovery_non_string_mode_is_withheld_without_an_exception(
    native_workspace, owner_sessionmaker, mode,  # noqa: F811
):
    from app.services.session_model_inputs import _hash

    n = native_workspace
    own, _foreign, paths = await _files(n, owner_sessionmaker, "t0")
    value, invocation = await _tool_round(n, 1, "fs_read", {"path": paths[own], "mode": "text"},
        capture={"skill_input_evidence": None})
    assert "OWN_PRIVATE_EVIDENCE" in value
    async with owner_sessionmaker() as db:
        row = await db.get(SessionToolInvocation, invocation.id)
        row.effective_arguments_json = {**row.effective_arguments_json, "mode": mode}
        row.effective_args_hash = _hash({"tool_name": "fs_read", "arguments": row.effective_arguments_json})
        await db.commit()
    _binding, snapshot = await _build(n, 2)
    _assert_withheld(snapshot, "OWN_PRIVATE_EVIDENCE")


async def _own_t0_path(n):
    from app.services.runtime_control_bus import sweep_pending_transcript_t0_bridges

    await sweep_pending_transcript_t0_bridges(limit=500, session_id=n.session)
    return next((n.root / f"memory/t0/sessions/{n.session}").rglob("source.md")).relative_to(n.root).as_posix()


async def test_limit_withheld_direct_read_returns_through_own_t0_history_and_t2_bundle(
    native_workspace, owner_sessionmaker,  # noqa: F811
):
    """R5j round-2 controller ruling accepts own T0/T2 reuse; A4d may reverse this limit."""
    from app.memory.t2.segment_package import _build_authorized_source_bundle
    from app.services.workspace_resource_authority import register_workspace_path

    n = native_workspace
    await _files(n, owner_sessionmaker, "t0")
    marker, path = "ORDINARY_LEAFLESS_FILE_BODY", "workspace/without-source-leaf.md"
    (n.root / path).write_text(marker)
    async with owner_sessionmaker() as db:
        await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
            owner_user_id=n.user, root_session_id=n.session, source="synthetic_legacy_work",
            content_hash=hashlib.sha256((n.root / path).read_bytes()).hexdigest())
        await db.commit()
    value, _invocation = await _tool_round(n, 1, "read_file", {"path": path}, leafless=True)
    assert marker in value
    _binding, direct = await _build(n, 2)
    _assert_withheld(direct, marker)
    path = await _own_t0_path(n)
    assert marker in (n.root / path).read_text()
    value, _invocation = await _tool_round(n, 2, "read_file", {"path": path})
    assert marker in value
    _binding, recovered = await _build(n, 3)
    assert marker in json.dumps(recovered["wire_request"], ensure_ascii=False)
    await _own_t0_path(n)
    bundles = [await _build_authorized_source_bundle(root=n.root.parent, agent_id=n.agent, tenant_id=n.tenant,
        session_id=n.session, t0_segment_id=source.parent.name, package_id="private-history-limit")
        for source in (n.root / f"memory/t0/sessions/{n.session}").rglob("source.md")]
    assert any(bundle is not None and marker in json.dumps(bundle, ensure_ascii=False) for bundle in bundles)


async def test_limit_revoked_team_memory_returns_through_own_t0_history(
    readable_team_memory, owner_sessionmaker,  # noqa: F811
):
    """R5j round-2 controller ruling accepts own-history reuse after revocation, pending A4d."""
    s, workspace, _receipt = readable_team_memory
    n = SimpleNamespace(**{**vars(s.n), "user": s.n.foreign, "session": s.second_session, "run": s.second_run})
    marker = "TEAM_PROJECT_RELEASE_MARKER"
    value, _invocation = await _tool_round(n, 1, "load_memory", {"ids": [f"team_memory:{workspace}:report"]})
    assert marker in value
    await leave_team(owner_sessionmaker, workspace, n.user)
    _binding, direct = await _build(n, 2)
    _assert_withheld(direct, marker)
    path = await _own_t0_path(n)
    assert marker in (n.root / path).read_text()
    value, _invocation = await _tool_round(n, 2, "read_file", {"path": path})
    assert marker in value
    _binding, recovered = await _build(n, 3)
    assert marker in json.dumps(recovered["wire_request"], ensure_ascii=False)
