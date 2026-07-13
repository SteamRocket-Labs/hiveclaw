"""Stage-2b: migrated INSERT paths set tenant_id against real PostgreSQL.

The stage-2b RLS policy on ``runtime_tasks`` is ``USING``-only (no
``WITH CHECK``): an INSERT that forgets ``tenant_id`` writes a NULL row that is
globally visible after the stage-3 role flip — an isolation hole. This proves
the accessor that creates runtime-task rows
(:func:`create_runtime_task_record`) derives ``tenant_id`` from the parent
agent and writes it, and leaves a parent-less row NULL (orphan surfaced, not
invented) — the mirror of the backfill's behaviour, but on the live write path.

A mock session can observe none of this (no RLS, no real INSERT), so this lives
in the Testcontainers integration suite.
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.database import tenant_scoped_session as real_tenant_scoped_session
from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services import runtime_task_service
from app.services.tenant_resolver import resolve_tenant_for_agent as real_resolve_tenant_for_agent


async def _mk_tenant(db) -> uuid.UUID:
    t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
    db.add(t)
    await db.flush()
    return t.id


async def _mk_user(db, tenant_id) -> uuid.UUID:
    u = User(
        username=f"u-{uuid.uuid4().hex[:10]}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        password_hash="x",
        display_name="U",
        tenant_id=tenant_id,
    )
    db.add(u)
    await db.flush()
    return u.id


async def _mk_agent(db, *, creator_id, tenant_id) -> uuid.UUID:
    a = Agent(name="A", creator_id=creator_id, tenant_id=tenant_id)
    db.add(a)
    await db.flush()
    return a.id


def _bind_accessors_to(monkeypatch, sessionmaker) -> None:
    """Point ``create_runtime_task_record``'s stage-2b accessors at the test
    engine: the real ``tenant_scoped_session`` / ``resolve_tenant_for_agent``,
    but threaded onto the Testcontainers sessionmaker instead of the app engine.
    """

    def _scoped(tenant_id=None, **_kwargs):
        return real_tenant_scoped_session(tenant_id, session_factory=sessionmaker)

    async def _resolve(agent_id, **_kwargs):
        return await real_resolve_tenant_for_agent(agent_id, session_factory=sessionmaker)

    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", _scoped)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", _resolve)
    monkeypatch.setattr(runtime_task_service, "async_session", sessionmaker)


@pytest.fixture
async def runtime_task_ids_to_cleanup(owner_sessionmaker):
    task_ids: set[uuid.UUID] = set()
    try:
        yield task_ids
    finally:
        if task_ids:
            async with owner_sessionmaker() as db:
                await db.execute(delete(RuntimeTask).where(RuntimeTask.id.in_(task_ids)))
                await db.commit()


async def test_create_runtime_task_sets_tenant_from_parent_agent(owner_sessionmaker, monkeypatch):
    async with owner_sessionmaker() as db:
        tid = await _mk_tenant(db)
        uid = await _mk_user(db, tid)
        aid = await _mk_agent(db, creator_id=uid, tenant_id=tid)
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)

    task_id = await runtime_task_service.create_runtime_task_record(
        task_id=uuid.uuid4().hex,
        task_type="trigger",
        status="running",
        parent_agent_id=aid,
    )

    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTask, uuid.UUID(task_id))
        assert row is not None
        # Derived from the parent agent's tenant — not NULL, not invented.
        assert row.tenant_id == tid


async def test_create_runtime_task_without_parent_fails_closed(owner_sessionmaker, monkeypatch):
    from app.runtime.tenant_admission import RuntimeTenantPreconditionError

    _bind_accessors_to(monkeypatch, owner_sessionmaker)

    task_uuid = uuid.uuid4()
    with pytest.raises(RuntimeTenantPreconditionError) as exc:
        await runtime_task_service.create_runtime_task_record(
            task_id=task_uuid.hex,
            task_type="delegation",
            status="pending",
        )

    assert exc.value.reason_code == "tenant_required"
    async with owner_sessionmaker() as db:
        assert await db.get(RuntimeTask, task_uuid) is None


async def test_sync_a2a_commits_and_completes_exact_child_runtime_task(owner_sessionmaker, monkeypatch):
    from app.agents import orchestrator
    from app.agents.orchestrator import AgentDelegationResult, OrchestrationPolicy, delegate_to_agent

    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        source_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        target_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    captured = {}

    async def fake_delegate(request):
        captured["request"] = request
        async with owner_sessionmaker() as db:
            row = await db.get(RuntimeTask, uuid.UUID(request.runtime_task_id))
            assert row is not None
            assert row.status == "running"
            assert row.tenant_id == tenant_id
        return AgentDelegationResult(
            content="durable reply",
            child_session_id=request.session_id,
            trace_id=request.trace_id or "trace-a2a-real-pg",
            depth=request.depth,
        )

    async def fake_persist_invocation_span(**_kwargs):
        return None

    monkeypatch.setattr(orchestrator, "_delegate", fake_delegate)
    monkeypatch.setattr(orchestrator, "persist_invocation_span", fake_persist_invocation_span)

    reply = await delegate_to_agent(
        target=type(
            "Target",
            (),
            {"id": target_agent_id, "name": "Target", "role_description": "Peer"},
        )(),
        target_model=object(),
        conversation_messages=[{"role": "user", "content": "do governed work"}],
        owner_id=owner_id,
        session_id="pair-session-real-pg",
        parent_agent_id=source_agent_id,
        parent_session_id="root-session-real-pg",
        trace_id="trace-a2a-real-pg",
        interaction_type="agent_message",
        tool_executor=object(),
        policy=OrchestrationPolicy(tool_profile="agent_message"),
        tenant_id=tenant_id,
    )

    assert reply == "durable reply"
    runtime_task_id = uuid.UUID(captured["request"].runtime_task_id)
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTask, runtime_task_id)
        assert row is not None
        assert row.task_type == "delegation"
        assert row.status == "completed"
        assert row.parent_agent_id == source_agent_id
        assert row.child_agent_id == target_agent_id
        assert row.root_user_id == owner_id
        assert row.child_session_id == "pair-session-real-pg"
        assert row.metadata_json["execution_backend"] == "foreground_inline"
        assert row.metadata_json["recovery_runtime_task_id"] == runtime_task_id.hex
        assert row.metadata_json["execution_receipt"]["status"] == "completed"


async def test_sync_a2a_stale_terminal_cannot_overwrite_reconciliation_fence(owner_sessionmaker, monkeypatch):
    from app.agents import orchestrator
    from app.agents.orchestrator import AgentDelegationResult, OrchestrationPolicy, delegate_to_agent

    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        source_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        target_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    captured = {}

    async def reconcile_while_inline_worker_is_finishing(request):
        captured["task_id"] = uuid.UUID(request.runtime_task_id)
        async with real_tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            row = await db.get(RuntimeTask, captured["task_id"], with_for_update=True)
            assert row is not None
            assert row.status == "running"
            assert row.claim_version == 1
            row.status = "needs_reconciliation"
            row.claim_version = 2
            row.claimed_by = "startup-reconciler:test"
            row.claim_expires_at = None
            row.result_summary = "unknown inline outcome"
            row.metadata_json = {**dict(row.metadata_json or {}), "needs_reconciliation": True}
        return AgentDelegationResult(
            content="late reply",
            child_session_id=request.session_id,
            trace_id=request.trace_id or "trace-a2a-race",
            depth=request.depth,
        )

    async def fake_persist_invocation_span(**_kwargs):
        return None

    monkeypatch.setattr(orchestrator, "_delegate", reconcile_while_inline_worker_is_finishing)
    monkeypatch.setattr(orchestrator, "persist_invocation_span", fake_persist_invocation_span)

    with pytest.raises(RuntimeError, match="disappeared before terminal commit"):
        await delegate_to_agent(
            target=type(
                "Target",
                (),
                {"id": target_agent_id, "name": "Target", "role_description": "Peer"},
            )(),
            target_model=object(),
            conversation_messages=[{"role": "user", "content": "do governed work"}],
            owner_id=owner_id,
            session_id="pair-session-race-pg",
            parent_agent_id=source_agent_id,
            parent_session_id="root-session-race-pg",
            trace_id="trace-a2a-race",
            interaction_type="agent_message",
            tool_executor=object(),
            policy=OrchestrationPolicy(tool_profile="agent_message"),
            tenant_id=tenant_id,
        )

    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTask, captured["task_id"])
        assert row is not None
        assert row.status == "needs_reconciliation"
        assert row.claim_version == 2
        assert row.claimed_by == "startup-reconciler:test"
        assert row.result_summary == "unknown inline outcome"
        assert row.metadata_json["needs_reconciliation"] is True


async def test_periodic_a2a_reconcile_quarantines_expired_lease_but_preserves_live_claim(
    owner_sessionmaker,
    monkeypatch,
):
    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        source_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        target_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    now = datetime.now(timezone.utc)
    expired_id = uuid.uuid4()
    live_id = uuid.uuid4()
    common = {
        "task_type": "a2a_delegation",
        "status": "running",
        "parent_agent_id": source_agent_id,
        "child_agent_id": target_agent_id,
        "root_user_id": owner_id,
        "claim_version": 1,
        "attempt_count": 1,
        "metadata_json": {
            "execution_backend": "foreground_inline",
            "side_effect_risk": "mutating",
            "restart_resume_blocker": "custom_tool_executor_not_replayable",
        },
    }
    await runtime_task_service.create_runtime_task_record(
        task_id=expired_id.hex,
        claimed_by="a2a-inline:expired-real-pg",
        claim_expires_at=now - timedelta(seconds=1),
        **common,
    )
    await runtime_task_service.create_runtime_task_record(
        task_id=live_id.hex,
        claimed_by="a2a-inline:live-real-pg",
        claim_expires_at=now + timedelta(minutes=5),
        **common,
    )

    reconciled = await runtime_task_service.reconcile_orphaned_runtime_tasks(task_types={"a2a_delegation"})

    assert reconciled == 1
    async with owner_sessionmaker() as db:
        expired = await db.get(RuntimeTask, expired_id)
        live = await db.get(RuntimeTask, live_id)
        assert expired is not None
        assert expired.status == "needs_reconciliation"
        assert expired.claim_version == 2
        assert expired.claimed_by.startswith("startup-reconciler:")
        assert expired.claim_expires_at is None
        assert live is not None
        assert live.status == "running"
        assert live.claim_version == 1
        assert live.claimed_by == "a2a-inline:live-real-pg"


async def test_periodic_inline_a2a_sweep_projects_foreground_delegation_operator_evidence(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
    runtime_task_ids_to_cleanup,
):
    from app.config import get_settings
    from app.services import runtime_task_worker
    from app.services.runtime_reconciliation import get_runtime_reconciliation_task

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        source_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        target_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    now = datetime.now(timezone.utc)
    expired_id = uuid.uuid4()
    live_id = uuid.uuid4()
    async_id = uuid.uuid4()
    runtime_task_ids_to_cleanup.update({expired_id, live_id, async_id})

    def metadata_for(task_id: uuid.UUID, session_id: str, worker_id: str) -> dict:
        return {
            "execution_backend": "foreground_inline",
            "side_effect_risk": "mutating",
            "restart_resume_blocker": "custom_tool_executor_not_replayable",
            "recovery_agent_id": str(target_agent_id),
            "recovery_session_id": session_id,
            "recovery_runtime_task_id": task_id.hex,
            "recovery_resolution_targets": [
                {
                    "agent_id": str(target_agent_id),
                    "session_id": session_id,
                    "runtime_task_id": task_id.hex,
                    "source": "current_run",
                    "expected_claim_version": 1,
                    "expected_claim_worker_id": worker_id,
                }
            ],
        }

    expired_session_id = "pair-session-expired-delegation"
    expired_worker_id = "a2a-inline:expired-delegation"
    await runtime_task_service.create_runtime_task_record(
        task_id=expired_id.hex,
        task_type="delegation",
        status="running",
        parent_agent_id=source_agent_id,
        child_agent_id=target_agent_id,
        child_session_id=expired_session_id,
        root_user_id=owner_id,
        claimed_by=expired_worker_id,
        claim_expires_at=now - timedelta(seconds=1),
        claim_version=1,
        attempt_count=1,
        metadata_json=metadata_for(expired_id, expired_session_id, expired_worker_id),
    )
    live_session_id = "pair-session-live-delegation"
    live_worker_id = "a2a-inline:live-delegation"
    await runtime_task_service.create_runtime_task_record(
        task_id=live_id.hex,
        task_type="delegation",
        status="running",
        parent_agent_id=source_agent_id,
        child_agent_id=target_agent_id,
        child_session_id=live_session_id,
        root_user_id=owner_id,
        claimed_by=live_worker_id,
        claim_expires_at=now + timedelta(minutes=5),
        claim_version=1,
        attempt_count=1,
        metadata_json=metadata_for(live_id, live_session_id, live_worker_id),
    )
    async_worker_id = "delegation-worker:expired-async"
    await runtime_task_service.create_runtime_task_record(
        task_id=async_id.hex,
        task_type="delegation",
        status="running",
        parent_agent_id=source_agent_id,
        child_agent_id=target_agent_id,
        child_session_id="async-delegation-session",
        root_user_id=owner_id,
        claimed_by=async_worker_id,
        claim_expires_at=now - timedelta(seconds=1),
        claim_version=1,
        attempt_count=1,
        metadata_json={
            "resumable_delegation": True,
            "resume_after_restart": True,
            "side_effect_risk": "mutating",
        },
    )

    reconciled = await runtime_task_worker.reconcile_expired_inline_a2a_once()

    assert reconciled == 1
    async with real_tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        expired = await db.get(RuntimeTask, expired_id)
        live = await db.get(RuntimeTask, live_id)
        assert expired is not None
        assert expired.status == "needs_reconciliation"
        assert expired.claim_version == 2
        assert expired.claimed_by.startswith("startup-reconciler:")
        assert expired.claim_expires_at is None
        assert expired.metadata_json["recovery_evidence_status"] == "ready"
        [target] = expired.metadata_json["recovery_resolution_targets"]
        assert target == {
            "agent_id": str(target_agent_id),
            "session_id": expired_session_id,
            "runtime_task_id": str(expired_id),
            "source": "current_run",
            "expected_manifest_state": "missing",
            "expected_manifest_ref": None,
            "expected_sha256": None,
        }
        [frame] = expired.metadata_json["recovery_tool_frames"]
        assert frame["runtime_task_id"] == str(expired_id)
        assert frame["tool_name"] == "a2a_agent_message"
        assert frame["reason"] == "expired_inline_a2a_manifest_missing"
        view = await get_runtime_reconciliation_task(db, task_id=expired_id, tenant_id=tenant_id)
        assert view is not None
        assert view["recovery_evidence"]["evidence_complete"] is True
        projected_metadata = dict(expired.metadata_json)

        assert live is not None
        assert live.status == "running"
        assert live.claim_version == 1
        assert live.claimed_by == live_worker_id
        assert live.claim_expires_at == now + timedelta(minutes=5)

        async_delegation = await db.get(RuntimeTask, async_id)
        assert async_delegation is not None
        assert async_delegation.status == "running"
        assert async_delegation.claim_version == 1
        assert async_delegation.claimed_by == async_worker_id
        assert async_delegation.claim_expires_at == now - timedelta(seconds=1)
        assert "recovery_evidence_status" not in async_delegation.metadata_json
        assert "recovery_tool_frames" not in async_delegation.metadata_json

    assert (
        await runtime_task_service.refresh_inline_a2a_reconciliation_evidence(
            task_ids={expired_id},
            limit=None,
        )
        == 0
    )
    async with owner_sessionmaker() as db:
        refreshed = await db.get(RuntimeTask, expired_id)
        assert refreshed is not None
        assert refreshed.claim_version == 2
        assert refreshed.metadata_json == projected_metadata


async def test_a2a_reaper_commits_fence_before_evidence_and_periodic_refresh_recovers_projection_crash(
    owner_sessionmaker,
    monkeypatch,
):
    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        source_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        target_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    task_id = uuid.uuid4()
    session_id = "pair-session-evidence-saga"
    await runtime_task_service.create_runtime_task_record(
        task_id=task_id.hex,
        task_type="a2a_delegation",
        status="running",
        parent_agent_id=source_agent_id,
        child_agent_id=target_agent_id,
        child_session_id=session_id,
        root_user_id=owner_id,
        claimed_by="a2a-inline:expired-evidence-saga",
        claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        claim_version=1,
        attempt_count=1,
        metadata_json={
            "execution_backend": "foreground_inline",
            "side_effect_risk": "mutating",
            "restart_resume_blocker": "custom_tool_executor_not_replayable",
            "recovery_agent_id": str(target_agent_id),
            "recovery_session_id": session_id,
            "recovery_runtime_task_id": task_id.hex,
        },
    )

    inspection_attempts = 0

    def crash_first_projection(**_kwargs):
        nonlocal inspection_attempts
        inspection_attempts += 1
        if inspection_attempts == 1:
            raise OSError("simulated crash after DB fence commit")
        return {
            "state": "valid",
            "receipt": {
                "ref": f"runtime_artifacts/recovery_manifests/{session_id}/{task_id.hex}.json",
                "sha256": "b" * 64,
            },
            "expected_checkpoint_seq": 9,
            "expected_claim_version": 1,
            "expected_claim_worker_id": "a2a-inline:expired-evidence-saga",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-after-fence",
                    "tool_name": "send_email",
                    "status": "running",
                }
            ],
            "recent_tool_outcomes": [],
            "recent_writes": [],
            "current_turn_writes": [],
        }

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.inspect_recovery_manifest_checkpoint",
        crash_first_projection,
    )

    reconciled = await runtime_task_service.reconcile_orphaned_runtime_tasks(task_types={"a2a_delegation"})

    assert reconciled == 1
    async with owner_sessionmaker() as db:
        fenced = await db.get(RuntimeTask, task_id)
        assert fenced is not None
        assert fenced.status == "needs_reconciliation"
        assert fenced.claim_version == 2
        assert fenced.claimed_by.startswith("startup-reconciler:")
        assert fenced.metadata_json["recovery_evidence_status"] == "pending"
        assert "recovery_manifest_sha256" not in fenced.metadata_json

    refreshed = await runtime_task_service.refresh_inline_a2a_reconciliation_evidence(
        task_ids={task_id},
        limit=10,
    )

    assert refreshed == 1
    assert inspection_attempts == 2
    async with owner_sessionmaker() as db:
        projected = await db.get(RuntimeTask, task_id)
        assert projected is not None
        assert projected.status == "needs_reconciliation"
        assert projected.claim_version == 2
        assert projected.metadata_json["recovery_evidence_status"] == "ready"
        assert projected.metadata_json["recovery_manifest_sha256"] == "b" * 64
        assert projected.metadata_json["recovery_resolution_targets"][0]["expected_sha256"] == "b" * 64
        assert projected.metadata_json["recovery_tool_frames"][0]["tool_call_id"] == "call-after-fence"


async def test_a2a_evidence_refresh_empty_task_scope_is_a_strict_noop(
    owner_sessionmaker,
    monkeypatch,
):
    async with owner_sessionmaker() as db:
        await db.execute(
            delete(RuntimeTask).where(
                RuntimeTask.task_type == "a2a_delegation",
                RuntimeTask.status == "needs_reconciliation",
            )
        )
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        source_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        target_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        task_id = uuid.uuid4()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="a2a_delegation",
                status="needs_reconciliation",
                parent_agent_id=source_agent_id,
                child_agent_id=target_agent_id,
                child_session_id="pair-session-empty-scope",
                root_user_id=owner_id,
                claim_version=2,
                claimed_by="startup-reconciler:empty-scope",
                metadata_json={
                    "execution_backend": "foreground_inline",
                    "recovery_agent_id": str(target_agent_id),
                    "recovery_session_id": "pair-session-empty-scope",
                    "recovery_runtime_task_id": str(task_id),
                    "recovery_evidence_status": "pending",
                },
            )
        )
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    assert await runtime_task_service.refresh_inline_a2a_reconciliation_evidence(task_ids=set()) == 0

    async with owner_sessionmaker() as db:
        untouched = await db.get(RuntimeTask, task_id)
        assert untouched is not None
        assert untouched.metadata_json["recovery_evidence_status"] == "pending"
        assert "recovery_resolution_targets" not in untouched.metadata_json


async def test_global_a2a_evidence_refresh_filters_pending_before_limit(
    owner_sessionmaker,
    monkeypatch,
):
    async with owner_sessionmaker() as db:
        await db.execute(
            delete(RuntimeTask).where(
                RuntimeTask.task_type == "a2a_delegation",
                RuntimeTask.status == "needs_reconciliation",
            )
        )
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        source_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        target_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        old_ready_id = uuid.uuid4()
        new_pending_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        db.add_all(
            [
                RuntimeTask(
                    id=old_ready_id,
                    tenant_id=tenant_id,
                    task_type="a2a_delegation",
                    status="needs_reconciliation",
                    parent_agent_id=source_agent_id,
                    child_agent_id=target_agent_id,
                    child_session_id="pair-session-old-ready",
                    root_user_id=owner_id,
                    claim_version=2,
                    claimed_by="startup-reconciler:old-ready",
                    created_at=now - timedelta(days=1),
                    completed_at=now - timedelta(days=1),
                    metadata_json={
                        "execution_backend": "foreground_inline",
                        "recovery_agent_id": str(target_agent_id),
                        "recovery_session_id": "pair-session-old-ready",
                        "recovery_runtime_task_id": str(old_ready_id),
                        "recovery_evidence_status": "ready",
                        "reviewed_marker": "must-not-be-selected",
                    },
                ),
                RuntimeTask(
                    id=new_pending_id,
                    tenant_id=tenant_id,
                    task_type="a2a_delegation",
                    status="needs_reconciliation",
                    parent_agent_id=source_agent_id,
                    child_agent_id=target_agent_id,
                    child_session_id="pair-session-new-pending",
                    root_user_id=owner_id,
                    claim_version=3,
                    claimed_by="startup-reconciler:new-pending",
                    created_at=now,
                    completed_at=now,
                    metadata_json={
                        "execution_backend": "foreground_inline",
                        "recovery_agent_id": str(target_agent_id),
                        "recovery_session_id": "pair-session-new-pending",
                        "recovery_runtime_task_id": str(new_pending_id),
                        "recovery_evidence_status": "pending",
                    },
                ),
            ]
        )
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    assert await runtime_task_service.refresh_inline_a2a_reconciliation_evidence(limit=1) == 1

    async with owner_sessionmaker() as db:
        old_ready = await db.get(RuntimeTask, old_ready_id)
        new_pending = await db.get(RuntimeTask, new_pending_id)
        assert old_ready is not None and new_pending is not None
        assert old_ready.metadata_json == {
            "execution_backend": "foreground_inline",
            "recovery_agent_id": str(target_agent_id),
            "recovery_session_id": "pair-session-old-ready",
            "recovery_runtime_task_id": str(old_ready_id),
            "recovery_evidence_status": "ready",
            "reviewed_marker": "must-not-be-selected",
        }
        assert new_pending.metadata_json["recovery_evidence_status"] == "ready"
        assert new_pending.metadata_json["recovery_resolution_targets"][0]["expected_manifest_state"] == "missing"


@pytest.mark.parametrize("operation_status", ["prepared", "failed"])
async def test_a2a_evidence_refresh_preserves_operator_review_operation_snapshot(
    owner_sessionmaker,
    monkeypatch,
    operation_status,
):
    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        source_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        target_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        task_id = uuid.uuid4()
        reviewed_target = {
            "agent_id": str(target_agent_id),
            "session_id": "pair-session-operator-review",
            "runtime_task_id": str(task_id),
            "source": "current_run",
            "expected_manifest_state": "missing",
        }
        reviewed_frame = {
            "runtime_task_id": str(task_id),
            "tool_call_id": "call-reviewed",
            "tool_name": "send_email",
            "status": "needs_reconciliation",
        }
        reviewed_metadata = {
            "execution_backend": "foreground_inline",
            "recovery_agent_id": str(target_agent_id),
            "recovery_session_id": "pair-session-operator-review",
            "recovery_runtime_task_id": str(task_id),
            "recovery_evidence_status": "ready",
            "recovery_resolution_targets": [reviewed_target],
            "recovery_tool_frames": [reviewed_frame],
            "reconciliation_operation": {
                "operation_id": f"operator-{operation_status}",
                "status": operation_status,
                "evidence_digest": "d" * 64,
            },
        }
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="a2a_delegation",
                status="needs_reconciliation",
                parent_agent_id=source_agent_id,
                child_agent_id=target_agent_id,
                child_session_id="pair-session-operator-review",
                root_user_id=owner_id,
                claim_version=4,
                claimed_by="startup-reconciler:operator-review",
                metadata_json=reviewed_metadata,
            )
        )
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    assert (
        await runtime_task_service.refresh_inline_a2a_reconciliation_evidence(
            task_ids={task_id},
            limit=1,
        )
        == 0
    )

    async with owner_sessionmaker() as db:
        stable = await db.get(RuntimeTask, task_id)
        assert stable is not None
        assert stable.metadata_json == reviewed_metadata


async def test_a2a_reaper_fence_prevents_old_kernel_from_clearing_manifest_after_side_effect(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.config import get_settings
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.recovery_manifest import inspect_recovery_manifest_checkpoint
    from app.runtime.session import SessionContext
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.tools.service import _renew_runtime_task_lease_before_execution

    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        source_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        target_agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    monkeypatch.setattr("app.database.async_session", owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    task_id = uuid.uuid4()
    session_id = "pair-session-old-kernel-fence"
    old_worker = "a2a-inline:old-kernel-real-pg"
    await runtime_task_service.create_runtime_task_record(
        task_id=task_id.hex,
        task_type="a2a_delegation",
        status="running",
        parent_agent_id=source_agent_id,
        child_agent_id=target_agent_id,
        child_session_id=session_id,
        root_user_id=owner_id,
        claimed_by=old_worker,
        claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        claim_version=1,
        attempt_count=1,
        metadata_json={
            "execution_backend": "foreground_inline",
            "side_effect_risk": "mutating",
            "restart_resume_blocker": "custom_tool_executor_not_replayable",
            "recovery_agent_id": str(target_agent_id),
            "recovery_session_id": session_id,
            "recovery_runtime_task_id": task_id.hex,
        },
    )
    session = SessionContext(
        session_id=session_id,
        source="agent",
        channel="agent",
        metadata={
            "tenant_id": str(tenant_id),
            "agent_id": str(target_agent_id),
            "runtime_task_id": task_id.hex,
            "claim_version": 1,
            "claim_worker_id": old_worker,
        },
    )
    request = InvocationRequest(
        model=type("Model", (), {"provider": "openai", "model": "gpt-4.1"})(),
        messages=[{"role": "user", "content": "send governed message"}],
        agent_name="Target",
        role_description="Peer",
        agent_id=target_agent_id,
        user_id=owner_id,
        session_context=session,
        memory_session_id=session_id,
    )

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    async def side_effect_then_reaper(*_args, **_kwargs):
        async with real_tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            row = await db.get(RuntimeTask, task_id, with_for_update=True)
            assert row is not None
            row.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        reconciled = await asyncio.create_task(
            runtime_task_service.reconcile_orphaned_runtime_tasks(task_types={"a2a_delegation"}),
            context=contextvars.Context(),
        )
        assert reconciled == 1
        return "sent exactly once"

    async def emit_event(_event):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    token = set_runtime_task_fence(
        task_id=task_id,
        claim_version=1,
        worker_id=old_worker,
    )
    try:
        result, _args, executed = await _execute_tool_with_hooks(
            execute_tool=side_effect_then_reaper,
            request=request,
            runtime_config=RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
            tool_name="send_email",
            tool_args={"to": "user@example.com"},
            tool_call_id="call-old-kernel-fence",
            emit_event=emit_event,
            renew_runtime_lease=_renew_runtime_task_lease_before_execution,
        )
    finally:
        reset_runtime_task_fence(token)

    assert result == "sent exactly once"
    assert executed is True
    inspection = inspect_recovery_manifest_checkpoint(
        agent_id=target_agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        runtime_task_id=task_id,
    )
    assert inspection is not None
    assert inspection["state"] == "valid"
    assert inspection["pending_tool_frames"][0]["tool_call_id"] == "call-old-kernel-fence"
    async with owner_sessionmaker() as db:
        fenced = await db.get(RuntimeTask, task_id)
        assert fenced is not None
        assert fenced.status == "needs_reconciliation"
        assert fenced.claim_version == 2
        assert fenced.claimed_by.startswith("startup-reconciler:")
        assert fenced.metadata_json["recovery_evidence_status"] == "ready"
        assert fenced.metadata_json["recovery_manifest_sha256"] == inspection["receipt"]["sha256"]


async def test_operator_claim_invalidation_blocks_old_post_event_compaction_manifest_write(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.config import get_settings
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _persist_recovery_manifest_checkpoint_with_fence
    from app.runtime.recovery_manifest import (
        inspect_recovery_manifest_checkpoint,
        persist_recovery_manifest_checkpoint,
        resolve_recovery_manifest_reconciliations,
    )
    from app.runtime.session import SessionContext
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.tools.service import _renew_runtime_task_lease_before_execution

    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        owner_id = await _mk_user(db, tenant_id)
        agent_id = await _mk_agent(db, creator_id=owner_id, tenant_id=tenant_id)
        await db.commit()

    _bind_accessors_to(monkeypatch, owner_sessionmaker)
    monkeypatch.setattr("app.database.async_session", owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    task_id = uuid.uuid4()
    session_id = "system-plan-post-event-compaction"
    old_worker = "system-plan:old-worker-real-pg"
    await runtime_task_service.create_runtime_task_record(
        task_id=task_id.hex,
        task_type="system_plan_run",
        status="running",
        parent_agent_id=agent_id,
        child_agent_id=agent_id,
        child_session_id=session_id,
        root_user_id=owner_id,
        claimed_by=old_worker,
        claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        claim_version=1,
        attempt_count=1,
        metadata_json={"side_effect_risk": "mutating"},
    )
    session = SessionContext(
        session_id=session_id,
        source="system_plan",
        channel="internal",
        metadata={
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "runtime_task_id": task_id.hex,
            "claim_version": 1,
            "claim_worker_id": old_worker,
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-system-plan-unknown",
                    "tool_name": "write_file",
                    "status": "needs_reconciliation",
                }
            ],
            "pending_tool_frame": {
                "tool_call_id": "call-system-plan-unknown",
                "tool_name": "write_file",
                "status": "needs_reconciliation",
            },
            "recovery_reconciliation_blocked": True,
        },
    )
    [initial_receipt] = persist_recovery_manifest_checkpoint(agent_id, session, data_root=tmp_path)
    initial = inspect_recovery_manifest_checkpoint(
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        runtime_task_id=task_id,
        data_root=tmp_path,
    )
    assert initial is not None and initial["state"] == "valid"
    assert initial["receipt"]["sha256"] == initial_receipt["sha256"]

    async with real_tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        row = await db.get(RuntimeTask, task_id, with_for_update=True)
        assert row is not None
        row.status = "needs_reconciliation"
        row.claim_version = 2
        row.claimed_by = "operator-reconciler"
        row.claim_expires_at = None

    request = InvocationRequest(
        model=type("Model", (), {"provider": "openai", "model": "gpt-4.1"})(),
        messages=[],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        user_id=owner_id,
        session_context=session,
        memory_session_id=session_id,
    )
    token = set_runtime_task_fence(task_id=task_id, claim_version=1, worker_id=old_worker)
    try:
        stale_receipt = await _persist_recovery_manifest_checkpoint_with_fence(
            request,
            renew_runtime_lease=_renew_runtime_task_lease_before_execution,
            delete_if_empty=True,
        )
    finally:
        reset_runtime_task_fence(token)

    assert stale_receipt is None
    unchanged = inspect_recovery_manifest_checkpoint(
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        runtime_task_id=task_id,
        data_root=tmp_path,
    )
    assert unchanged is not None
    assert unchanged["receipt"]["sha256"] == initial_receipt["sha256"]
    receipts = resolve_recovery_manifest_reconciliations(
        targets=[
            {
                "agent_id": str(agent_id),
                "session_id": session_id,
                "runtime_task_id": str(task_id),
                "source": "current_run",
                "expected_manifest_state": "present",
                "expected_manifest_ref": initial_receipt["ref"],
                "expected_sha256": initial_receipt["sha256"],
                "expected_checkpoint_seq": initial["expected_checkpoint_seq"],
                "expected_claim_version": initial["expected_claim_version"],
                "expected_claim_worker_id": initial["expected_claim_worker_id"],
            }
        ],
        tenant_id=tenant_id,
        action="mark_resolved",
        reason="operator reviewed exact post-event manifest",
        actor_user_id=owner_id,
        operation_id=uuid.uuid4().hex,
        data_root=tmp_path,
    )
    assert len(receipts) == 1
    assert receipts[0]["source_state"] == "present"
