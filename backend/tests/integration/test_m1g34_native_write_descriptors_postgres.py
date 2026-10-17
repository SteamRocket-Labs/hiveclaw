"""Native text writes bind predecessor and output bytes to their actual descriptor."""

import hashlib
import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.database import pin_rls_tenant_context
from app.models.invocation_span import InvocationSpan
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _prepare(n, db_factory, case):
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_tool_runtime import prepare_tool_invocation

    before = b"PRIVATE_FOREIGN_FILE_BODY\r\n"
    n.target.write_bytes(before)
    tool, _, mode = case.partition(":")
    editing = tool == "edit_file" or mode == "edit"
    arguments = {"path": n.path, **({"old_text": "PRIVATE_FOREIGN", "new_text": "AUTHORIZED"} if editing else {"content": "Authorized replacement\n"})}
    if mode:
        arguments["mode"] = mode
        if editing:
            arguments["old_string"], arguments["new_string"] = arguments.pop("old_text"), arguments.pop("new_text")
    expected = b"AUTHORIZED_FILE_BODY\n" if editing else arguments["content"].encode()
    async with db_factory() as db:
        await pin_rls_tenant_context(db, n.tenant)
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, n.path),
            actions=["read", "write"], conditions={}))
        await db.commit()
        turn, call = f"turn-{n.run.hex}", f"descriptor-{n.run.hex}"
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, messages=[{"role": "user", "content": "Update the file"}],
            tools=[], provider="openai", model="synthetic-no-provider")
        await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request,
            response={"content": None, "tool_calls": [{"id": call, "type": "function", "function": {
                "name": tool, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls", "usage": {}})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request)
        invocation = await prepare_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, provider_request_id=request, provider_tool_use_id=call, tool_name=tool, arguments=arguments)
        await db.commit()
    return SimpleNamespace(n=n, tool=tool, arguments=arguments, before=before, expected=expected, invocation=invocation, turn=turn)


@pytest.fixture(params=["write_file", "edit_file", "fs_write:write", "fs_write:edit"])
async def write_case(native_workspace, app_user_sessionmaker, request):  # noqa: F811
    return await _prepare(native_workspace, app_user_sessionmaker, request.param)


async def _execute(s, trace):
    return str(await s.n.service.execute(s.tool, s.arguments, agent_id=s.n.agent, user_id=s.n.user,
        session_id=str(s.n.session), runtime_task_id=str(s.n.run), turn_id=s.turn,
        tool_call_id=s.invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace))


async def _proof(db_factory, s):
    async with db_factory() as db:
        await pin_rls_tenant_context(db, s.n.tenant)
        span = await db.get(InvocationSpan, uuid.uuid5(s.invocation.id, "workspace-output-evidence"))
        assert span is not None
        return span.metadata_json["workspace_execution_evidence"]


async def test_real_writer_records_exact_descriptor_predecessor_and_output(write_case, app_user_sessionmaker):
    s, trace = write_case, {}
    raw = await _execute(s, trace)
    assert s.n.target.read_bytes() == s.expected, raw
    proof = await _proof(app_user_sessionmaker, s)
    observation = proof["descriptor_writes"][0]
    assert observation["path"] == s.n.path
    assert observation["verified"] is True
    assert observation["expected_exists"] is True
    assert observation["expected_predecessor_sha256"] == observation["observed_predecessor_sha256"] == hashlib.sha256(s.before).hexdigest()
    assert observation["expected_output_sha256"] == observation["observed_output_sha256"] == hashlib.sha256(s.expected).hexdigest()
    assert observation["descriptor"] == {"device": s.n.target.stat().st_dev, "inode": s.n.target.stat().st_ino}
    assert "PRIVATE_FOREIGN_FILE_BODY" not in json.dumps(proof)


def _patch_open(monkeypatch, callback):
    from app.services import workspace_resource_authority as authority
    from app.services.agent_tool_domains import workspace as domain

    original = authority.open_workspace_file
    @contextmanager
    def interleaved(workspace, path, *, mode="rb", **kwargs):
        if mode in {"wb", "r+b"}:
            callback("before", None)
        with original(workspace, path, mode=mode, **kwargs) as handle:
            if mode in {"wb", "r+b"}:
                callback("opened", handle)
            yield handle
            if mode in {"wb", "r+b"}:
                handle.flush()
                callback("after", handle)
    monkeypatch.setattr(authority, "open_workspace_file", interleaved)
    monkeypatch.setattr(domain, "open_workspace_file", interleaved)


async def test_changed_predecessor_is_not_overwritten_even_without_optional_trace(write_case, monkeypatch):
    s = write_case
    concurrent = s.before + b"INDEPENDENT_ADDITION\n"
    def change(phase, _handle):
        if phase == "before":
            s.n.target.write_bytes(concurrent)
    _patch_open(monkeypatch, change)
    raw = await _execute(s, None if s.tool == "write_file" else {})
    assert s.n.target.read_bytes() == concurrent, raw
    assert "workspace_predecessor_changed" in raw


async def test_file_appearing_after_absence_observation_is_not_truncated(
    native_workspace, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    s = await _prepare(native_workspace, app_user_sessionmaker, "write_file")
    s.n.target.unlink()
    appeared = b"INDEPENDENT_NEW_FILE"
    def change(phase, _handle):
        if phase == "before":
            s.n.target.write_bytes(appeared)
    _patch_open(monkeypatch, change)
    raw = await _execute(s, {})
    assert s.n.target.read_bytes() == appeared, raw
    assert "workspace_predecessor_changed" in raw


async def test_new_file_is_created_exclusively_with_a_verified_output(
    native_workspace, app_user_sessionmaker,  # noqa: F811
):
    s = await _prepare(native_workspace, app_user_sessionmaker, "write_file")
    s.n.target.unlink()
    raw = await _execute(s, {})
    assert s.n.target.read_bytes() == s.expected, raw
    observation = (await _proof(app_user_sessionmaker, s))["descriptor_writes"][0]
    assert observation["verified"] is True and observation["expected_exists"] is False
    assert observation["observed_predecessor_sha256"] is None


async def test_edit_checks_the_exact_raw_bytes_it_uses_after_descriptor_preflight(
    native_workspace, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    s = await _prepare(native_workspace, app_user_sessionmaker, "edit_file")
    concurrent = s.before + b"UNVERIFIED_ADDITIONAL_INPUT\n"
    def change(phase, _handle):
        if phase == "opened":
            s.n.target.write_bytes(concurrent)
    _patch_open(monkeypatch, change)
    raw = await _execute(s, {})
    assert s.n.target.read_bytes() == concurrent, raw
    assert "workspace_predecessor_changed" in raw


async def test_optional_rewind_size_limit_does_not_limit_native_byte_verification(
    native_workspace, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.services import session_workspace_snapshot as snapshots

    s = await _prepare(native_workspace, app_user_sessionmaker, "write_file")
    monkeypatch.setattr(snapshots, "MAX_WORKSPACE_SNAPSHOT_FILE_BYTES", 1)
    trace = {}
    raw = await _execute(s, trace)
    assert s.n.target.read_bytes() == s.expected, raw
    assert (await _proof(app_user_sessionmaker, s))["descriptor_writes"][0]["verified"] is True
    assert trace["workspace_mutation_state_errors"][s.n.path]


async def test_post_write_bytes_must_match_the_actual_writer_before_success(write_case, monkeypatch):
    s = write_case
    late = b"OUT_OF_BAND_POST_WRITE_BYTES"
    def change(phase, _handle):
        if phase == "after":
            s.n.target.write_bytes(late)
    _patch_open(monkeypatch, change)
    raw = await _execute(s, {})
    assert s.n.target.read_bytes() == late
    assert "workspace_resource_changed" in raw and '"unknown"' in raw, raw


async def test_artifact_capture_cannot_replace_the_checked_writer_output_with_later_bytes(
    write_case, app_user_sessionmaker, monkeypatch,
):
    from sqlalchemy import select
    from app.models.chat_artifact import ChatArtifact
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from app.tools import service

    s = write_case
    capture = service._capture_workspace_mutation_evidence
    def interleave(**kwargs):
        s.n.target.write_bytes(b"FOREIGN_BYTES_BEFORE_ARTIFACT_CAPTURE")
        return capture(**kwargs)
    monkeypatch.setattr(service, "_capture_workspace_mutation_evidence", interleave)
    trace = {}
    raw = await _execute(s, trace)
    envelopes = await _persist_tool_call(agent_id=s.n.agent, user_id=s.n.user, session_id=str(s.n.session), data={
        "name": s.tool, "args": s.arguments, "status": "done", "result": raw,
        "tool_call_id": s.invocation.provider_tool_use_id, "provider_request_id": s.invocation.provider_request_id,
        "runtime_task_id": str(s.n.run), "tool_execution_evidence": _execution_evidence(trace)})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.n.tenant)
        assert not (await db.scalars(select(ChatArtifact).where(ChatArtifact.agent_id == s.n.agent))).all(), envelopes
    results = [item["payload"] for item in envelopes if item["item_kind"] == "tool_result"]
    assert results[0]["metadata"]["artifact_delivery"]["status"] == "unknown"
    assert results[0]["outcome"] == "success"  # The write completed; only publication lost its proof.


async def test_backend_without_the_checked_descriptor_cannot_claim_its_evidence(
    native_workspace, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    s = await _prepare(native_workspace, app_user_sessionmaker, "write_file")
    class UnobservedBackend:
        name = "unobserved-test-backend"
        async def execute(self, request, executor):
            s.n.target.write_bytes(s.expected)
            return "Backend claims it wrote the file"
    monkeypatch.setattr(s.n.service, "backend", UnobservedBackend())
    await _execute(s, {})
    proof = await _proof(app_user_sessionmaker, s)
    assert proof["descriptor_writes"][0]["verified"] is False
