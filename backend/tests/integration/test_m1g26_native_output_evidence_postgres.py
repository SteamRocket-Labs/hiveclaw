"""Native publication binds captured bytes to the canonical execution, privately."""

import hashlib
import json
import os
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace, _admitted_write_messages, _prepare_write  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def execute_write(db, n):
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.services.content_input_receipts import record_workspace_receipt

    assert n.target.read_bytes() == b"PRIVATE_FOREIGN_FILE_BODY"
    record_workspace_receipt(db, n.tenant, n.agent, n.path,
        hashlib.sha256(b"PRIVATE_FOREIGN_FILE_BODY").hexdigest(), None)
    db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
        resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, n.path),
        actions=["read", "write"], conditions={}))
    await db.commit()
    invocation, arguments, turn = await _prepare_write(db, n, messages=await _admitted_write_messages(db, n))
    before = hashlib.sha256(n.target.read_bytes()).hexdigest()
    trace = {}
    raw = await n.service.execute("write_file", arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
        tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
    assert n.target.read_text() == arguments["content"], raw
    return invocation, arguments, before, str(raw), trace


async def test_actual_executor_persists_canonical_private_before_and_after_evidence(
    native_workspace, app_user_sessionmaker,  # noqa: F811
):
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        invocation, arguments, before, raw, _trace = await execute_write(db, n)
        row = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "workspace-output-evidence"))
        assert row is not None, _trace.get("workspace_execution_evidence")
        proof = row.metadata_json["workspace_execution_evidence"]
        assert proof["invocation_id"] == str(invocation.id)
        assert proof["effective_args_hash"] == invocation.args_hash
        result = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == invocation.provider_request_id))
        assert proof["model_result_id"] == str(result.id)
        assert proof["model_request_hash"] == result.model_request_hash
        assert proof["lineage"][0]["before_state"]["sha256"] == before
        assert proof["states"][n.path]["sha256"] == hashlib.sha256(arguments["content"].encode()).hexdigest()
        assert "PRIVATE_FOREIGN_FILE_BODY" not in json.dumps(proof)
        assert raw


@pytest.mark.parametrize("changed", [False, True])
async def test_callback_publishes_only_the_version_captured_by_its_executor(
    native_workspace, app_user_sessionmaker, changed,  # noqa: F811
):
    from app.models.chat_artifact import ChatArtifact
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        invocation, arguments, before, raw, trace = await execute_write(db, n)
        if changed:
            info = n.target.stat()
            n.target.write_text("FORGED_LATER_OUTPUT".ljust(len(arguments["content"])))
            os.utime(n.target, ns=(info.st_atime_ns, info.st_mtime_ns))
        envelopes = await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
            "name": "write_file", "args": arguments, "status": "done", "result": raw,
            "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
            "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
        assert "FORGED_LATER_OUTPUT" not in json.dumps(envelopes), envelopes
        assert before not in json.dumps(envelopes)
        rows = (await db.scalars(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent))).all()
        if changed:
            assert not rows
            results = [item["payload"] for item in envelopes if item["item_kind"] == "tool_result"]
            assert results[0]["metadata"]["artifact_delivery"]["status"] == "unknown"
            assert results[0]["outcome"] == "success"
        else:
            assert len(rows) == 1
            assert rows[0].snapshot_json["execution_evidence_ref"] == f"workspace-output:{uuid.uuid5(invocation.id, 'workspace-output-evidence')}"
            assert rows[0].snapshot_json["tool_invocation_id"] == str(invocation.id)
        await db.refresh(invocation)
        assert invocation.effect_state == "effect_committed"


@pytest.mark.parametrize("loss", ["missing", "agent", "model_hash", "model_bytes", "arguments", "result"])
async def test_unbound_or_missing_execution_evidence_withholds_only_publication(
    native_workspace, app_user_sessionmaker, loss,  # noqa: F811
):
    from app.models.chat_artifact import ChatArtifact
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        invocation, arguments, before, raw, trace = await execute_write(db, n)
        row = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "workspace-output-evidence"))
        assert row is not None
        if loss == "missing":
            await db.delete(row)
        elif loss == "agent":
            row.agent_id = None
        elif loss == "model_bytes":
            model = await db.scalar(select(SessionModelResult).where(
                SessionModelResult.provider_request_id == invocation.provider_request_id))
            model.model_request_snapshot_json = {**model.model_request_snapshot_json,
                "messages": [{"role": "user", "content": "A different, unverified input"}]}
        else:
            keys = {"model_hash": "model_request_hash", "arguments": "effective_args_hash", "result": "tool_result_hash"}
            row.metadata_json = {"workspace_execution_evidence": {
                **row.metadata_json["workspace_execution_evidence"], keys[loss]: "f" * 64}}
        await db.commit()
        envelopes = await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
            "name": "write_file", "args": arguments, "status": "done", "result": raw,
            "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
            "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
        assert not (await db.scalars(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent))).all()
        result = next(item["payload"] for item in envelopes if item["item_kind"] == "tool_result")
        assert result["outcome"] == "success" and result["metadata"]["artifact_delivery"]["status"] == "unknown"
        assert not result["parts"] and before not in json.dumps(envelopes)
        await db.refresh(invocation)
        assert invocation.effect_state == "effect_committed"
        assert n.target.read_text() == arguments["content"]


@pytest.mark.parametrize("when_changed", [None, "before_tool_callback", "before_final_message"])
async def test_final_message_reuses_the_native_version_without_current_file_fallback(
    native_workspace, app_user_sessionmaker, when_changed,  # noqa: F811
):
    from app.models.audit import ChatMessage
    from app.models.chat_artifact import ChatArtifact
    from app.services.chat_artifact_delivery import create_chat_artifacts_for_message, read_chat_artifact_snapshot_content
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        invocation, arguments, _before, raw, trace = await execute_write(db, n)
        if when_changed == "before_tool_callback":
            n.target.write_text("LATE_UNVERIFIED_FINAL_BYTES")
        await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
            "name": "write_file", "args": arguments, "status": "done", "result": raw,
            "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
            "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
        initial = (await db.scalars(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent))).all()
        if when_changed == "before_final_message":
            n.target.write_text("LATE_UNVERIFIED_FINAL_BYTES")
        message = ChatMessage(id=uuid.uuid4(), tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            role="assistant", content="The captured report", conversation_id=str(n.session))
        db.add(message)
        await db.flush()
        parts = await create_chat_artifacts_for_message(db=db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, runtime_task_id=n.run, message_id=message.id, paths=[n.path],
            workspace_root=n.root, rebind_existing_to_message=True)
        await db.commit()
        rows = (await db.scalars(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent))).all()
        if when_changed == "before_tool_callback":
            assert not parts and not rows
        else:
            assert len(parts) == len(initial) == len(rows) == 1
            assert rows[0].id == initial[0].id and rows[0].message_id == message.id
            content = read_chat_artifact_snapshot_content(rows[0], n.root)
            assert content["content"] == arguments["content"]
        assert "LATE_UNVERIFIED_FINAL_BYTES" not in json.dumps(parts)
