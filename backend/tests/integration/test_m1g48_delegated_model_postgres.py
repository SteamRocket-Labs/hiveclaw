"""Persist the actual delegated kernel/model/tool flow without inventing input authority."""

import json
import platform

import pytest
from sqlalchemy import select

from tests.integration.test_m1g46_native_skill_receipt_postgres import _run_native_skill_task

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS process gate; Linux delivery is deferred.")]


@pytest.mark.parametrize("compact_session_ids", [False, True])
async def test_actual_delegated_kernel_persists_model_and_tool_receipts(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, compact_session_ids,
):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionCommand, SessionModelResult, SessionToolInvocation

    seed, client, _bindings, _imported = await _run_native_skill_task(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, delegated=True, compact_session_ids=compact_session_ids)
    result = seed["delegation_result"]
    async with owner_sessionmaker() as db:
        rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])
            .order_by(SessionModelResult.round_id)))
        snapshots = [row.model_request_snapshot_json for row in rows]
        (tmp_path / "delegation-model-inputs.json").write_text(json.dumps(snapshots, indent=2, default=str))
        assert len(rows) == len(client.requests) == 3, (result, snapshots)
        assert all(row.state == "round_committed" and row.seal_json for row in rows)
        assert "NATIVE_SOUL_CONTEXT_PRESENT" in json.dumps(snapshots)
        assert all(snapshot["input_bindings"]["status"] == "incomplete" for snapshot in snapshots)
        # The delegated instruction has no captured parent-model lineage yet.
        assert not list(await db.scalars(select(SessionCommand).where(SessionCommand.session_id == seed["session_id"])))
        tools = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"])))
        assert {row.tool_name for row in tools} == {"load_skill", "run_skill_tool"}
        assert all(row.result_event_id and row.provider_request_id in {model.provider_request_id for model in rows}
            for row in tools)
        receipts = list(await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.carrier_ref == f"{seed['agent_id']}\nworkspace/native-report.md",
            ContentInputReceipt.carrier_kind == "workspace_artifact")))
        assert not receipts, "Unbound parent instructions must never become a complete artifact input receipt."
    assert result.transcript_committed and not result.failed, result

    from app.agents import orchestrator
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    marker = seed["agent_root"] / "workspace/native-report.md"
    marker_stat = marker.stat()
    token = set_runtime_task_fence(task_id=seed["run_id"], worker_id="delegation-native-worker", claim_version=1)
    try:
        recovered = await orchestrator._delegate_after_cycle_check(seed["delegation_request"],
            trace_id=seed["delegation_request"].trace_id, child_session_id=seed["delegation_request"].session_id,
            tool_profile=orchestrator._resolve_delegation_tool_profile("worker_safe"), is_delegation=True)
    finally:
        reset_runtime_task_fence(token)
    assert recovered.content == result.content and recovered.transcript_committed and not recovered.failed, recovered
    assert len(client.requests) == 3, "A committed final must replay its seal without resending the provider."
    assert marker.stat().st_mtime_ns == marker_stat.st_mtime_ns, "Settled tools must not execute again."
    async with owner_sessionmaker() as db:
        replayed = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])
            .order_by(SessionModelResult.round_id)))
        assert [row.model_request_snapshot_json for row in replayed] == snapshots
        events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == seed["run_id"])))
        instructions = [event for event in events if event.event_type == "user_message"]
        assert len(instructions) == 1 and instructions[0].actor_type == "agent"
        assert not [event for event in events if event.event_type == "assistant_message"], "Do not duplicate canonical finals."

    from uuid import uuid4
    from sqlalchemy.exc import IntegrityError
    from app.database import pin_rls_tenant_context
    from app.models.chat_session import ChatSession

    # Current child binding is independent of a row's otherwise valid tenant/run IDs.
    for field, value in [("peer_agent_id", seed["agent_id"]), ("runtime_task_id", None)]:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            child = await db.get(ChatSession, seed["session_id"])
            setattr(child, field, value)
            await db.flush()
            db.add(SessionModelResult(id=uuid4(), tenant_id=seed["tenant_id"], session_id=seed["session_id"],
                run_id=seed["run_id"], turn_id=f"turn-{seed['run_id'].hex}", round_id=f"{seed['run_id']}:round:42",
                provider_request_id=f"scope-control:{uuid4()}", state="prepared", model_request_hash="a" * 64,
                model_request_snapshot_json={}, bound_input_ids_json=[]))
            with pytest.raises(IntegrityError, match="session_v2_authority_binding_mismatch"):
                await db.flush()
            await db.rollback()


async def test_ambiguous_delegated_provider_send_keeps_typed_reconciliation(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.llm_client import LLMError

    seed, client, _bindings, _imported = await _run_native_skill_task(owner_sessionmaker,
        app_user_sessionmaker, monkeypatch, tmp_path, delegated=True,
        client_script=[LLMError("Transport disconnected after dispatch", delivery_state="unknown")])
    result = seed["delegation_result"]
    assert len(client.requests) == 1 and result.failed
    assert result.terminal_reason == "delegation_session_reconciliation_required", result
    assert result.parts[0]["status"] == "unknown" and not result.parts[0]["retryable"]
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.status == "needs_reconciliation"
        assert task.metadata_json["session_v2_reconciliation"]["delivery_state"] == "unknown"
        rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
        assert len(rows) == 1 and rows[0].state == "needs_reconciliation" and rows[0].seal_json
    assert not (seed["agent_root"] / "workspace/native-report.md").exists()


async def test_delegation_recovers_a_sealed_round_before_registry_commit(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from sqlalchemy.exc import SQLAlchemyError
    from app.agents import orchestrator
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services import session_model_round
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    original = session_model_round.commit_sealed_model_round
    async def registry_unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("Controlled storage interruption after durable seal")
    monkeypatch.setattr(session_model_round, "commit_sealed_model_round", registry_unavailable)
    seed, client, _bindings, _imported = await _run_native_skill_task(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, delegated=True)
    result = seed["delegation_result"]
    assert len(client.requests) == 1 and result.terminal_reason == "delegation_session_reconciliation_required", result
    async with owner_sessionmaker() as db:
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
        assert row.state == "sealed" and row.seal_json
        original_seal, original_request = row.seal_json, row.model_request_snapshot_json
        assert not list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"])))
    monkeypatch.setattr(session_model_round, "commit_sealed_model_round", original)
    token = set_runtime_task_fence(task_id=seed["run_id"], worker_id="delegation-native-worker", claim_version=1)
    try:
        recovered = await orchestrator._delegate_after_cycle_check(seed["delegation_request"],
            trace_id=seed["delegation_request"].trace_id, child_session_id=seed["delegation_request"].session_id,
            tool_profile=orchestrator._resolve_delegation_tool_profile("worker_safe"), is_delegation=True)
    finally:
        reset_runtime_task_fence(token)
    assert not recovered.failed and recovered.transcript_committed, recovered
    assert len(client.requests) == 3, "Resume the first sealed generation instead of asking the model to replace it."
    assert (seed["agent_root"] / "workspace/native-report.md").read_text() == "NATIVE_SKILL_REPORT"
    async with owner_sessionmaker() as db:
        rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])
            .order_by(SessionModelResult.round_id)))
        assert len(rows) == 3 and all(row.state == "round_committed" for row in rows)
        assert rows[0].seal_json == original_seal and rows[0].model_request_snapshot_json == original_request
