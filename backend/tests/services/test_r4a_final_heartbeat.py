"""Committed model output survives contention with its own terminal writer."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.services.test_session_terminal_outcome_v2 import _prepare, _seed

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _committed_answer(factory):
    from app.models.audit import ChatMessage
    from app.models.chat_artifact import ChatArtifact
    from app.models.runtime_task import RuntimeTask
    from app.services.session_model_round import commit_model_response

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(factory)
    content = "Report ready: [report.md](workspace/report.md)"
    async with factory() as db:
        task = await db.get(RuntimeTask, run_id)
        task.claimed_by, task.claim_version = "r4a-worker", 1
        task.claim_expires_at = datetime.now(UTC) + timedelta(minutes=3)
        message = ChatMessage(tenant_id=tenant_id, agent_id=agent_id, user_id=task.root_user_id,
            conversation_id=str(session_id), role="tool_call", content="saved report")
        db.add(message)
        await db.flush()
        artifact = ChatArtifact(tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
            runtime_task_id=run_id, message_id=message.id, owner_user_id=task.root_user_id,
            root_session_id=session_id, authority_state="owned", path="workspace/report.md", name="report.md",
            source="workspace_write", snapshot_hash=hashlib.sha256(b"saved report").hexdigest(),
            snapshot_json={"content": "saved report"}, size=12)
        db.add(artifact)
        request_id, _ = await _prepare(db, tenant_id=tenant_id, agent_id=agent_id,
            session_id=session_id, run_id=run_id, turn_id=turn_id)
        receipt = await commit_model_response(db, tenant_id=tenant_id, agent_id=agent_id,
            session_id=session_id, run_id=run_id, turn_id=turn_id, round_index=1,
            provider_request_id=request_id,
            response={"content": content, "tool_calls": [], "finish_reason": "stop", "usage": {}})
        await db.commit()
        return SimpleNamespace(tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
            run_id=run_id, turn_id=turn_id, receipt=receipt, content=content, artifact_id=artifact.id)


async def _assert_delivered(factory, seed):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome

    async with factory() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        assert task.status == "completed"
        outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.run_id == seed.run_id))
        assert outcome is not None and outcome.state == "terminal_committed"
        assert outcome.terminal_result_id == uuid.UUID(seed.receipt["result_id"])
        finals = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == seed.run_id, ChatTranscriptEvent.item_kind == "assistant_final")))
        assert len(finals) == 1
        payload = finals[0].metadata_json["v2_payload"]
        assert payload["terminal_result_id"] == seed.receipt["result_id"]
        assert payload["source_blocks"] == outcome.seal_json["source_blocks"]
        assert payload["artifact_refs"] == [str(seed.artifact_id)]
        assert task.metadata_json["artifact_ids"] == [str(seed.artifact_id)]
        results = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed.run_id)))
        assert [row.id for row in results] == [uuid.UUID(seed.receipt["result_id"])]
        assert results[0].seal_json["semantic_content"] == seed.content


async def test_finalization_survives_own_row_lock_past_heartbeat_statement_timeout(
    owner_sessionmaker, migrated_pg_url, monkeypatch,
):
    from app import database
    from app.services import chat_artifact_delivery as artifacts, runtime_task_fence as fence
    from app.services import web_chat_runtime as runtime
    from app.services.web_chat_run_orchestrator import _commit_canonical_terminal_outcome
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _patch_runtime_seams

    seed = await _committed_answer(owner_sessionmaker)
    _patch_runtime_seams(monkeypatch, {**vars(seed), "data_root": "/unused"}, owner_sessionmaker)
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool,
        connect_args={"server_settings": {"statement_timeout": "50"}})
    renewal_started = asyncio.Event()

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE runtime_tasks SET claim_expires_at"):
            renewal_started.set()

    monkeypatch.setattr(database, "async_session", async_sessionmaker(engine, expire_on_commit=False))
    original = artifacts.materialize_declared_read_artifacts

    async def slow_selection(db, **kwargs):
        await asyncio.wait_for(renewal_started.wait(), timeout=5)
        async with owner_sessionmaker() as observer:
            rows = (await observer.execute(text("SELECT pid, wait_event_type, pg_blocking_pids(pid) "
                "FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'"))).all()
            print("R4a lock wait snapshot:", rows)
        await asyncio.sleep(0.15)
        return await original(db, **kwargs)

    monkeypatch.setattr(artifacts, "materialize_declared_read_artifacts", slow_selection)
    state = SimpleNamespace(run_uuid=seed.run_id, agent=SimpleNamespace(id=seed.agent_id, tenant_id=seed.tenant_id),
        session_id=str(seed.session_id), metadata={"turn_id": seed.turn_id}, runtime_session_context=None,
        ports=SimpleNamespace(runtime=runtime._web_chat_run_ports().runtime,
            artifacts=SimpleNamespace(artifact_paths=lambda *_: ["workspace/report.md"],
                declared_paths=lambda *_: ["workspace/report.md"], rejected_paths=lambda *_: [])))
    try:
        await fence.run_claimed_runtime_task(_commit_canonical_terminal_outcome(state, seed.receipt, seed.content),
            task_id=seed.run_id, claim_version=1, worker_id="r4a-worker", lease_seconds=0.15)
    finally:
        await engine.dispose()
    await _assert_delivered(owner_sessionmaker, seed)


async def test_reclaimed_committed_final_finishes_before_reexecution_or_quarantine(owner_sessionmaker, monkeypatch):
    from app import database
    from app.models.runtime_task import RuntimeTask
    from app.services import runtime_task_fence as fence, web_chat_runtime as runtime

    seed = await _committed_answer(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        task.claim_version = 4
        task.attempt_count = 4
        task.metadata_json = {**task.metadata_json, "reclaimed_expired_claim": True, "max_execution_attempts": 3}
        await db.commit()
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)

    async def no_reexecution(**kwargs):
        pytest.fail("a committed final must be recovered without entering the model/tool lifecycle")

    monkeypatch.setattr("app.services.web_chat_run_orchestrator.run_web_chat_task", no_reexecution)
    await fence.run_claimed_runtime_task(runtime.execute_web_chat_run(seed.run_id), task_id=seed.run_id,
        claim_version=4, worker_id="r4a-worker", lease_seconds=180)
    await _assert_delivered(owner_sessionmaker, seed)


async def test_expired_final_recovery_retains_already_committed_write_artifact(owner_sessionmaker):
    from app.models.runtime_task import RuntimeTask
    from app.services.session_terminal_outcome import recover_terminal_candidates_once

    seed = await _committed_answer(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        task.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        recovered = await recover_terminal_candidates_once(db, worker_id="r4a-recovery", run_id=seed.run_id)
    assert recovered["terminal_committed"] == 1
    await _assert_delivered(owner_sessionmaker, seed)


async def test_artifact_materialization_releases_task_lock(owner_sessionmaker, migrated_pg_url, monkeypatch):
    from app.services import chat_artifact_delivery as artifacts, web_chat_runtime as runtime
    from app.services.web_chat_run_orchestrator import _record_canonical_terminal_artifact_selection
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _patch_runtime_seams

    seed = await _committed_answer(owner_sessionmaker)
    _patch_runtime_seams(monkeypatch, {**vars(seed), "data_root": "/unused"}, owner_sessionmaker)
    original = artifacts.materialize_declared_read_artifacts

    async def observe_unlocked(db, **kwargs):
        async with owner_sessionmaker() as other:
            await other.execute(text("SET LOCAL statement_timeout = '50ms'"))
            await other.execute(text("SELECT id FROM runtime_tasks WHERE id=:id FOR UPDATE"), {"id": seed.run_id})
        return await original(db, **kwargs)

    monkeypatch.setattr(artifacts, "materialize_declared_read_artifacts", observe_unlocked)
    state = SimpleNamespace(run_uuid=seed.run_id, agent=SimpleNamespace(id=seed.agent_id, tenant_id=seed.tenant_id),
        session_id=str(seed.session_id), runtime_session_context=None,
        ports=SimpleNamespace(runtime=runtime._web_chat_run_ports().runtime,
            artifacts=SimpleNamespace(artifact_paths=lambda *_: ["workspace/report.md"],
                declared_paths=lambda *_: ["workspace/report.md"], rejected_paths=lambda *_: [])))
    await _record_canonical_terminal_artifact_selection(state, seed.content,
        terminal_result_id=uuid.UUID(seed.receipt["result_id"]))


async def test_foreign_task_lock_past_expiry_never_renews_or_executes_tool(owner_sessionmaker, monkeypatch):
    from app import database
    from app.models.runtime_task import RuntimeTask
    from app.services import runtime_task_fence as fence
    from app.tools.service import _renew_runtime_task_lease_before_execution

    seed = await _committed_answer(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        task.claim_expires_at = datetime.now(UTC) + timedelta(seconds=0.15)
        await db.commit()
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    renewed, effects = [], []
    original = fence.renew_current_runtime_task_lease

    async def observe_renewal(**kwargs):
        result = await original(**kwargs)
        renewed.append(result)
        return result

    monkeypatch.setattr(fence, "renew_current_runtime_task_lease", observe_renewal)

    async def work():
        await asyncio.sleep(0.25)
        await _renew_runtime_task_lease_before_execution()
        effects.append("tool effect")

    async with owner_sessionmaker() as locker:
        await locker.scalar(select(RuntimeTask).where(RuntimeTask.id == seed.run_id).with_for_update())

        async def release_lock():
            await asyncio.sleep(0.3)
            await locker.rollback()

        release = asyncio.create_task(release_lock())
        try:
            with pytest.raises(fence.StaleRuntimeTaskFenceError):
                await fence.run_claimed_runtime_task(work(), task_id=seed.run_id,
                    claim_version=1, worker_id="r4a-worker", lease_seconds=0.15)
        finally:
            await release
    assert renewed == [], "a locked row must not be reported as renewed"
    assert effects == []


async def test_artifact_selection_rechecks_claim_after_materialization(owner_sessionmaker, monkeypatch):
    from app.models.runtime_task import RuntimeTask
    from app.services import chat_artifact_delivery as artifacts, runtime_task_fence as fence, web_chat_runtime as runtime
    from app.services.web_chat_run_orchestrator import _record_canonical_terminal_artifact_selection
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _patch_runtime_seams

    seed = await _committed_answer(owner_sessionmaker)
    _patch_runtime_seams(monkeypatch, {**vars(seed), "data_root": "/unused"}, owner_sessionmaker)

    async def reclaim_during_selection(db, **kwargs):
        async with owner_sessionmaker() as other:
            await other.execute(text("SET LOCAL statement_timeout = '50ms'"))
            await other.execute(text("UPDATE runtime_tasks SET claim_version=2, claimed_by='new-worker' WHERE id=:id"),
                {"id": seed.run_id})
            await other.commit()
        return []

    monkeypatch.setattr(artifacts, "materialize_declared_read_artifacts", reclaim_during_selection)
    state = SimpleNamespace(run_uuid=seed.run_id, agent=SimpleNamespace(id=seed.agent_id, tenant_id=seed.tenant_id),
        session_id=str(seed.session_id), runtime_session_context=None,
        ports=SimpleNamespace(runtime=runtime._web_chat_run_ports().runtime,
            artifacts=SimpleNamespace(artifact_paths=lambda *_: ["workspace/report.md"],
                declared_paths=lambda *_: ["workspace/report.md"], rejected_paths=lambda *_: [])))
    token = fence.set_runtime_task_fence(task_id=seed.run_id, claim_version=1, worker_id="r4a-worker")
    try:
        with pytest.raises(fence.StaleRuntimeTaskFenceError):
            await _record_canonical_terminal_artifact_selection(state, seed.content,
                terminal_result_id=uuid.UUID(seed.receipt["result_id"]))
    finally:
        fence.reset_runtime_task_fence(token)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        assert task.claim_version == 2 and task.claimed_by == "new-worker"
        assert "artifact_selection_result_id" not in task.metadata_json
