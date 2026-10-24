"""Native discovery results remain usable by the next governed model round."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from uuid import UUID

from tests.integration import test_unverified_t2_recovery_postgres as t2_support
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = t2_support.t2_work


@pytest.mark.parametrize("tool_name,arguments", [
    ("read_ledger", {}),
    ("list_files", {"path": "workspace"}),
    ("tool_search", {"query": "list_employees"}),
])
async def test_native_empty_discovery_retains_complete_next_round_sources(
    t2_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tool_name, arguments,
):
    import app.runtime.invoker as invoker
    from app.database import pin_rls_tenant_context
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    s = t2_work
    call = {"id": "call-native-discovery", "type": "function", "function": {
        "name": tool_name, "arguments": json.dumps(arguments),
    }}
    client = ScriptedLLMClient([
        {"content": "Inspect the current work.", "tool_calls": [call], "finish_reason": "tool_calls"},
        {"content": "360", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, s["run_id"])
        run.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await db.commit()
    fence = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    assert len(client.requests) == 2
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s["tenant_id"])
        models = list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id == s["run_id"]).order_by(SessionModelResult.round_id)))
        calls = list(await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == s["run_id"])))
        assert len(models) == 2 and len(calls) == 1
        assert calls[0].tool_name == tool_name and calls[0].effect_state == "effect_committed"
        assert calls[0].result_event_id is not None
        bindings = [model.model_request_snapshot_json["input_bindings"] for model in models]
        (s["agent_root"].parent / "native-discovery-bindings.json").write_text(json.dumps(bindings, indent=2))
        assert bindings[0]["dependency_coverage"]["complete"] is True
        assert bindings[1]["dependency_coverage"]["complete"] is True, bindings[1]
        if tool_name != "tool_search":
            from app.models.chat_transcript_event import ChatTranscriptEvent
            from app.models.invocation_span import InvocationSpan
            from app.services.session_model_inputs import _origin_dependencies

            ref = f"session-tool-result:{calls[0].id}"
            assert (await _origin_dependencies(db, tenant_id=s["tenant_id"], agent_id=s["agent_id"], ref=ref))[1]
            event = await db.get(ChatTranscriptEvent, calls[0].result_event_id)
            pointer = event.metadata_json["v2_payload"]["metadata"]["native_empty_read_receipt"]
            evidence_id = UUID(pointer["evidence_ref"].split(":", 1)[1])
            retained = await db.get(InvocationSpan, evidence_id)
            assert retained is not None
            async with owner_sessionmaker() as writer:
                proof = await writer.get(InvocationSpan, evidence_id)
                proof.metadata_json = {"native_empty_read_evidence": {
                    **proof.metadata_json["native_empty_read_evidence"], "tool_result_hash": "0" * 64,
                }}
                await writer.commit()
            assert not (await _origin_dependencies(db, tenant_id=s["tenant_id"], agent_id=s["agent_id"], ref=ref))[1]


@pytest.mark.parametrize("tool_name", ["read_ledger", "list_files"])
async def test_nonempty_unreceipted_content_is_not_an_empty_observation(
    t2_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tool_name,
):
    import app.runtime.invoker as invoker
    from app.database import pin_rls_tenant_context
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services.agent_work_ledger import upsert_agent_work_ledger_todo
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.session_model_inputs import _origin_dependencies
    from app.services.web_chat_runtime import execute_web_chat_run

    s = t2_work
    if tool_name == "read_ledger":
        upsert_agent_work_ledger_todo(agent_id=s["agent_id"], session_id=str(s["session_id"]),
            title="UNRECEIPTED_LEGACY_LEDGER", status="pending")
        arguments = {}
    else:
        (s["agent_root"] / "workspace" / "unreceipted.txt").write_text("UNRECEIPTED_LEGACY_BODY")
        arguments = {"path": ""}
    client = ScriptedLLMClient([
        {"tool_calls": [{"id": "read-nonempty", "type": "function", "function": {
            "name": tool_name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"},
        {"content": "No transfer requested.", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, s["run_id"])
        run.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await db.commit()
    fence = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    assert len(client.requests) == 2
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s["tenant_id"])
        call = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == s["run_id"]))
        assert call.tool_name == tool_name and call.effect_state == "effect_committed"
        event = await db.get(ChatTranscriptEvent, call.result_event_id)
        payload = event.metadata_json["v2_payload"]
        assert "native_empty_read_receipt" not in (payload.get("metadata") or {})
        assert not (await _origin_dependencies(db, tenant_id=s["tenant_id"], agent_id=s["agent_id"],
            ref=f"session-tool-result:{call.id}"))[1]
        if tool_name == "read_ledger":
            assert "UNRECEIPTED_LEGACY_LEDGER" in payload["content"]
        else:
            assert "soul.md" in payload["content"] and "unreceipted.txt" not in payload["content"]
            assert (s["agent_root"] / "workspace" / "unreceipted.txt").read_text() == "UNRECEIPTED_LEGACY_BODY"
