"""Actual model snapshots retain precise source refs without granting an audience."""

import json

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.kernel.engine import _mid_run_items_to_user_messages
from app.models.session_v2 import SessionCommand, SessionModelResult
from app.services.llm_client import LLMMessage
from app.services.session_model_round import prepare_model_request
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input
from tests.integration.test_m1g28_runtime_source_reads_postgres import _create
from tests.integration.test_m1g29_workspace_input_evidence_postgres import _native_read

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _snapshot(db, tenant, agent, session, run, messages, round_index=1):
    request_id = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
        run_id=run, turn_id=f"turn-{run.hex}", round_index=round_index, messages=messages,
        tools=[], provider="openai", model="synthetic-no-provider")
    result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
    return request_id, result.model_request_snapshot_json


@pytest.mark.parametrize("corrupt_command", [False, True])
async def test_bound_command_is_a_versioned_source_and_wire_claims_cannot_replace_it(
    owner_sessionmaker, app_user_sessionmaker, corrupt_command,
):
    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    messages = _mid_run_items_to_user_messages(bound)
    messages[0].dependencies = [{"source_kind": "public", "source_id": "caller-claim", "source_version": "fake"}]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        command = await db.scalar(select(SessionCommand).where(SessionCommand.session_id == session))
        if corrupt_command:
            command.target_hash = "0" * 64
            await db.commit()
        _request, snapshot = await _snapshot(db, tenant, agent, session, run, messages)
        catalog = snapshot["input_bindings"]
        assert catalog["source_authority"] == "not_evaluated"
        assert "source_ref" not in json.dumps(snapshot["wire_request"])
        assert "caller-claim" not in json.dumps(catalog)
        if corrupt_command:
            assert catalog["dependency_coverage"]["complete"] is False and not catalog["dependencies"]
        else:
            assert catalog["dependency_coverage"]["complete"] is True
            assert catalog["dependencies"] == [{"source_kind": "session_command", "source_id": str(command.id),
                "source_version": command.request_hash}]


async def test_model_reply_inherits_its_frozen_inputs_without_sweeping_unselected_history(
    owner_sessionmaker, app_user_sessionmaker,
):
    from app.services.session_model_round import commit_model_response
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority
    from app.models.chat_session import ChatSession

    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        request, first = await _snapshot(db, tenant, agent, session, run, _mid_run_items_to_user_messages(bound))
        await commit_model_response(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_index=1, provider_request_id=request,
            response={"content": "Reply derived from the admitted input", "tool_calls": [], "finish_reason": "stop", "usage": {}})
        await db.commit()
        user = (await db.get(ChatSession, session)).user_id
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user, agent_id=agent, session_id=session)
        unrelated = await _accepted_input(db, authority=authority, session_id=session, run_id=run,
            kind="steer_current_turn", content="UNSELECTED_HISTORY_MUST_NOT_TAINT_THIS_REQUEST")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        _second, snapshot = await _snapshot(db, tenant, agent, session, run,
            [LLMMessage(role="assistant", content="Reply derived from the admitted input",
                source_ref=f"session-provider-result:{request}")], round_index=2)
        catalog = snapshot["input_bindings"]
        assert catalog["dependency_coverage"]["complete"] is True, catalog
        assert catalog["dependencies"] == first["input_bindings"]["dependencies"]
        assert str(unrelated.command_id) not in json.dumps(catalog)


@pytest.mark.parametrize("tamper", [False, True])
async def test_exact_native_carrier_remains_a_dependency_even_when_its_input_list_is_empty(
    native_workspace, app_user_sessionmaker, owner_sessionmaker, tamper,  # noqa: F811
):
    from app.models.organization_identity import ContentInputReceipt
    from app.services.content_input_receipts import latest_workspace_receipt
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call

    n = native_workspace
    path, body = "workspace/original.md", "Original authenticated carrier body"
    await _create(n, path, body)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        receipt_id, version = receipt.id, receipt.content_sha256
        invocation, args = await _native_read(db, n, path)
    trace = {}
    result = await n.service.execute("read_file", args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), tool_call_id=invocation.provider_tool_use_id,
        emit_runtime_hooks=False, trace_metadata_sink=trace)
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": "read_file", "args": args, "status": "done", "result": str(result),
        "tool_call_id": invocation.provider_tool_use_id, "provider_request_id": invocation.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _execution_evidence(trace)})
    if tamper:
        async with owner_sessionmaker() as db:
            (await db.get(ContentInputReceipt, receipt_id)).dependencies_json = [{"source_kind": "unknown",
                "source_id": "FORGED_NEW_DEPENDENCY", "source_version": "changed"}]
            await db.commit()
    else:
        await _create(n, path, "A later authenticated file revision")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        _request, snapshot = await _snapshot(db, n.tenant, n.agent, n.session, n.run,
            [LLMMessage(role="tool", tool_call_id=invocation.provider_tool_use_id, content=str(result),
                source_ref=f"session-tool-result:{invocation.id}")], round_index=2)
        catalog = snapshot["input_bindings"]
        assert catalog["dependency_coverage"]["complete"] is False  # The call's original plain input had no origin binding.
        carriers = [d for d in catalog["dependencies"] if d["source_kind"] == "workspace_artifact"]
        assert carriers == ([] if tamper else [{"source_kind": "workspace_artifact", "source_id": str(receipt_id), "source_version": version}]), catalog
        assert "FORGED_NEW_DEPENDENCY" not in json.dumps(catalog)
