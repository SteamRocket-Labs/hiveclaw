from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.dialects import postgresql


class _ScalarListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, tasks):
        self.tasks = tasks
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _ScalarListResult(self.tasks)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _BusinessTaskDB(_FakeDB):
    def __init__(self, runtime_tasks, business_task):
        super().__init__(runtime_tasks)
        self.business_task = business_task

    async def execute(self, stmt):
        self.statements.append(stmt)
        if len(self.statements) == 1:
            return _ScalarListResult(self.tasks)
        return _ScalarOneResult(self.business_task)


def test_runtime_task_claim_statement_uses_skip_locked_and_queue_order():
    from app.services.runtime_task_claim_service import build_runtime_task_claim_statement

    stmt = build_runtime_task_claim_statement(
        task_types=("web_chat_turn",),
        now=datetime(2026, 7, 2, tzinfo=timezone.utc),
        batch_size=10,
    )

    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "runtime_tasks.status IN" in compiled
    assert "runtime_tasks.priority DESC" in compiled
    assert "runtime_tasks.created_at ASC" in compiled
    assert "extract(" not in compiled.lower()
    assert "floor(" not in compiled.lower()


def test_runtime_task_aged_lane_is_sargable_oldest_first():
    from app.services.runtime_task_claim_service import build_runtime_task_claim_statement

    stmt = build_runtime_task_claim_statement(
        task_types=("workflow",),
        now=datetime(2026, 7, 13, tzinfo=timezone.utc),
        batch_size=1,
        lane="aged",
    )

    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "runtime_tasks.created_at <=" in compiled
    assert "runtime_tasks.created_at ASC" in compiled
    assert "runtime_tasks.priority DESC" not in compiled
    assert "extract(" not in compiled.lower()


def test_runtime_task_claim_statement_reclaims_only_expired_active_rows():
    from app.services.runtime_task_claim_service import build_runtime_task_claim_statement, runtime_task_claim_snapshot

    stmt = build_runtime_task_claim_statement(
        task_types=("web_chat_turn",),
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),
        batch_size=10,
    )

    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "runtime_tasks.status = " in compiled
    assert "runtime_tasks.claim_expires_at IS NULL" in compiled
    assert "runtime_tasks.claim_expires_at <=" in compiled
    assert "FOR UPDATE SKIP LOCKED" in compiled
    snapshot = runtime_task_claim_snapshot()
    assert snapshot["lease_reclaimable_task_types"] == [
        "web_chat_turn",
        "goal_continuation",
        "team_member",
        "advanced_plan",
        "approval_execution",
        "hr_provisioning",
        "dream",
        "system_plan_run",
        "subagent",
        "delegation",
        "trigger",
        "heartbeat",
    ]
    assert snapshot["fence_contract"] == "claim_version+worker_id+lease"


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize("task_type", ["delegation", "trigger", "heartbeat"])
@pytest.mark.parametrize("initial_status", ["pending", "expired_running"])
async def test_two_pg_workers_claim_one_runtime_authority_for_live_and_recovery_rows(
    owner_sessionmaker,
    task_type,
    initial_status,
):
    """The shared queue is the only execution authority, including restart reclaim."""

    from app.database import enter_rls_bypass
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    tenant_id = uuid4()
    task_id = uuid4()
    expired = initial_status == "expired_running"
    trigger_id = uuid4()
    metadata = {
        "test_fixture": "shared_worker_claim_race",
        "side_effect_risk": {
            "delegation": "read_only",
            "trigger": "mutating",
            "heartbeat": "internal_governed",
        }[task_type],
        "restart_replay_contract": {
            "schema": "runtime_restart_replay_contract.v1",
            "idempotency_key": f"{task_type}:{task_id.hex}:restart",
            "task_type": task_type,
            "task_id": task_id.hex,
            "mode": "durable_restart_replay",
            "requires_completion_journal": True,
        },
    }
    if task_type == "trigger":
        metadata.update(
            {
                "trigger_ids": [str(trigger_id)],
                "workflow_batch_protocol": {
                    "mode": "deterministic_workflow_ref",
                    "trigger_ids": [str(trigger_id)],
                },
            }
        )
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed durable runtime claim race"):
        db.add(Tenant(id=tenant_id, name="Runtime claim race", slug=f"claim-race-{tenant_id.hex[:10]}"))
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type=task_type,
                status="running" if expired else "pending",
                parent_agent_id=uuid4(),
                priority=2_000_000_000,
                claimed_by="dead-worker" if expired else None,
                claim_version=7 if expired else 0,
                attempt_count=2 if expired else 0,
                claim_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)) if expired else None,
                metadata_json=metadata,
            )
        )
        await db.commit()

    async def claim(worker_id: str):
        async with owner_sessionmaker() as db, enter_rls_bypass(db, reason=f"claim race {worker_id}"):
            return await RuntimeTaskClaimService(
                db=db,
                worker_id=worker_id,
                task_types=(task_type,),
                lease_seconds=60,
            ).claim_available(batch_size=100)

    claimed_a, claimed_b = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    winners = [task for batch in (claimed_a, claimed_b) for task in batch]
    assert [task.id for task in winners].count(task_id) == 1

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="verify durable runtime claim race"):
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.claimed_by in {"worker-a", "worker-b"}
        assert task.claim_version == (8 if expired else 1)
        assert task.attempt_count == (3 if expired else 1)
        tenant = await db.get(Tenant, tenant_id)
        await db.delete(task)
        await db.flush()
        if tenant is not None:
            await db.delete(tenant)
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize("task_type", ["subagent", "delegation"])
async def test_expired_child_session_runtime_is_reconciled_instead_of_replayed(
    owner_sessionmaker,
    task_type,
):
    from app.database import enter_rls_bypass
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    tenant_id = uuid4()
    task_id = uuid4()
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed expired child-session runtime"):
        db.add(Tenant(id=tenant_id, name="Child session replay fence", slug=f"child-fence-{tenant_id.hex[:10]}"))
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type=task_type,
                status="running",
                parent_agent_id=uuid4(),
                child_session_id=str(uuid4()),
                claimed_by="dead-session-worker",
                claim_version=3,
                attempt_count=1,
                claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                metadata_json={
                    "side_effect_risk": "read_only",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"{task_type}:{task_id.hex}:restart",
                        "task_type": task_type,
                        "task_id": task_id.hex,
                        "mode": "durable_restart_replay",
                        "requires_completion_journal": True,
                    },
                },
            )
        )
        await db.commit()

    async def claim(worker_id: str):
        async with owner_sessionmaker() as db, enter_rls_bypass(db, reason=f"child-session fence {worker_id}"):
            return await RuntimeTaskClaimService(
                db=db,
                worker_id=worker_id,
                task_types=(task_type,),
                lease_seconds=60,
            ).claim_available(batch_size=1)

    claimed_a, claimed_b = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert claimed_a == []
    assert claimed_b == []

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="verify child-session replay fence"):
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "needs_reconciliation"
        assert task.claim_version == 4
        assert task.claimed_by is None
        assert task.metadata_json["reconciliation_reason"] == "expired_session_bound_or_mutating_runtime"
        tenant = await db.get(Tenant, tenant_id)
        await db.delete(task)
        await db.flush()
        if tenant is not None:
            await db.delete(tenant)
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_expired_mutating_trigger_claim_atomically_reconciles_and_enqueues_outbox(owner_sessionmaker):
    from sqlalchemy import select

    from app.database import enter_rls_bypass
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    tenant_id, user_id, agent_id, session_id, task_id, trigger_id = (uuid4() for _ in range(6))
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed unsafe expired trigger claim"):
        db.add(Tenant(id=tenant_id, name="Unsafe trigger claim", slug=f"unsafe-trigger-{tenant_id.hex[:10]}"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"unsafe-trigger-{user_id.hex[:8]}",
                email=f"unsafe-trigger-{user_id.hex[:8]}@example.test",
                password_hash="x",
                display_name="Unsafe Trigger Owner",
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id, tenant_id=tenant_id, creator_id=user_id, owner_user_id=user_id, name="Unsafe Trigger Agent"
            )
        )
        await db.flush()
        db.add(ChatSession(id=session_id, tenant_id=tenant_id, agent_id=agent_id, user_id=user_id))
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="trigger",
                status="running",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                root_user_id=user_id,
                claimed_by="crashed-trigger-worker",
                claim_version=11,
                attempt_count=1,
                claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                metadata_json={
                    "side_effect_risk": "mutating",
                    "trigger_ids": [str(trigger_id)],
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"trigger:{task_id.hex}:restart",
                        "task_type": "trigger",
                        "task_id": task_id.hex,
                        "mode": "durable_restart_replay",
                        "requires_completion_journal": True,
                    },
                },
            )
        )
        await db.commit()

    async def claim(worker_id: str):
        async with owner_sessionmaker() as db, enter_rls_bypass(db, reason=f"unsafe trigger claim {worker_id}"):
            return await RuntimeTaskClaimService(
                db=db,
                worker_id=worker_id,
                task_types=("trigger",),
                lease_seconds=60,
            ).claim_available(batch_size=1)

    claimed_a, claimed_b = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert claimed_a == []
    assert claimed_b == []

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="verify unsafe trigger reconciliation"):
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "needs_reconciliation"
        assert task.claim_version == 12
        assert task.claimed_by is None
        assert task.metadata_json["reconciliation_reason"] == "expired_session_bound_or_mutating_runtime"
        outboxes = (
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
                )
            )
            .scalars()
            .all()
        )
        assert len(outboxes) == 1
        assert outboxes[0].terminal_status == "needs_reconciliation"
        assert outboxes[0].payload_rank == 150

        await db.execute(
            delete(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
        )
        await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
        await db.execute(delete(RuntimeTask).where(RuntimeTask.id == task_id))
        await db.execute(delete(Agent).where(Agent.id == agent_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_priority_aging_claims_old_low_priority_work_under_continuous_high_priority_flow(owner_sessionmaker):
    from app.database import enter_rls_bypass
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    tenant_id = uuid4()
    low_id = uuid4()
    now = datetime.now(timezone.utc)
    high_ids: list = []
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed runtime priority aging"):
        db.add(Tenant(id=tenant_id, name="Runtime priority aging", slug=f"claim-aging-{tenant_id.hex[:10]}"))
        await db.flush()
        db.add(
            RuntimeTask(
                id=low_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="pending",
                parent_agent_id=uuid4(),
                priority=0,
                created_at=now - timedelta(minutes=30),
                metadata_json={"test_fixture": "old_low_priority"},
            )
        )
        await db.commit()

    claimed_ids: list = []
    for round_index in range(5):
        high_id = uuid4()
        high_ids.append(high_id)
        async with owner_sessionmaker() as db, enter_rls_bypass(db, reason=f"inject high priority {round_index}"):
            db.add(
                RuntimeTask(
                    id=high_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="pending",
                    parent_agent_id=uuid4(),
                    priority=2_000_000_000,
                    created_at=datetime.now(timezone.utc),
                    metadata_json={"test_fixture": "continuously_injected_high_priority"},
                )
            )
            await db.commit()
        async with owner_sessionmaker() as db, enter_rls_bypass(db, reason=f"priority aging round {round_index}"):
            claimed = await RuntimeTaskClaimService(
                db=db,
                worker_id=f"aging-worker-{round_index}",
                task_types=("workflow",),
                lease_seconds=60,
            ).claim_available(batch_size=1)
            claimed_ids.extend(task.id for task in claimed)
            for task in claimed:
                task.status = "completed"
            await db.commit()
        if low_id in claimed_ids:
            break

    assert low_id in claimed_ids

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="cleanup runtime priority aging"):
        for task_id in (low_id, *high_ids):
            task = await db.get(RuntimeTask, task_id)
            if task is not None:
                await db.delete(task)
        await db.flush()
        tenant = await db.get(Tenant, tenant_id)
        if tenant is not None:
            await db.delete(tenant)
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_two_lane_claim_handles_future_timestamps_and_extreme_priority(owner_sessionmaker):
    from app.database import enter_rls_bypass
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    tenant_id = uuid4()
    old_id, current_id, future_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed future timestamp claim lanes"):
        db.add(Tenant(id=tenant_id, name="Future claim lanes", slug=f"future-lanes-{tenant_id.hex[:10]}"))
        await db.flush()
        db.add_all(
            [
                RuntimeTask(
                    id=old_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="pending",
                    priority=0,
                    created_at=now - timedelta(minutes=30),
                    metadata_json={"lane": "aged"},
                ),
                RuntimeTask(
                    id=current_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="pending",
                    priority=1_000_000,
                    created_at=now,
                    metadata_json={"lane": "normal"},
                ),
                RuntimeTask(
                    id=future_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="pending",
                    priority=2_000_000_000,
                    created_at=now + timedelta(days=1),
                    metadata_json={"lane": "future_clock_skew"},
                ),
            ]
        )
        await db.commit()

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="claim future timestamp lanes"):
        claimed = await RuntimeTaskClaimService(
            db=db,
            worker_id="future-lane-worker",
            task_types=("workflow",),
            lease_seconds=60,
        ).claim_available(batch_size=2)
        assert [task.id for task in claimed] == [old_id, future_id]

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="cleanup future timestamp lanes"):
        await db.execute(delete(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id))
        tenant = await db.get(Tenant, tenant_id)
        if tenant is not None:
            await db.delete(tenant)
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_claim_lane_queries_use_partial_indexes_on_large_queue(owner_sessionmaker):
    from app.database import enter_rls_bypass
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_claim_service import build_runtime_task_claim_statement

    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed claim lane explain queue"):
        db.add(Tenant(id=tenant_id, name="Claim lane explain", slug=f"claim-explain-{tenant_id.hex[:10]}"))
        await db.flush()
        db.add_all(
            [
                RuntimeTask(
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="pending",
                    priority=index % 20,
                    created_at=now - timedelta(seconds=index),
                    metadata_json={"test_fixture": "claim_lane_explain"},
                )
                for index in range(750)
            ]
        )
        await db.commit()

    plans: dict[str, str] = {}
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="explain claim lane indexes"):
        await db.execute(text("ANALYZE runtime_tasks"))
        await db.execute(text("SET LOCAL enable_seqscan = off"))
        for lane in ("normal", "aged"):
            stmt = build_runtime_task_claim_statement(
                task_types=("workflow",),
                now=now,
                batch_size=10,
                lane=lane,
            )
            sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            rows = (await db.execute(text(f"EXPLAIN {sql}"))).scalars().all()
            plans[lane] = "\n".join(str(row) for row in rows)

    assert "ix_runtime_tasks_claim_normal_lane" in plans["normal"]
    assert "ix_runtime_tasks_claim_aged_lane" in plans["aged"]

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="cleanup claim lane explain queue"):
        await db.execute(delete(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id))
        tenant = await db.get(Tenant, tenant_id)
        if tenant is not None:
            await db.delete(tenant)
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_pg_trigger_intent_claim_fence_executes_and_terminals(monkeypatch, owner_sessionmaker):
    from app.database import enter_rls_bypass
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services import trigger_daemon
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task

    monkeypatch.setattr("app.services.runtime_task_service.async_session", owner_sessionmaker)

    tenant_id = uuid4()
    agent_id = uuid4()
    trigger_id = uuid4()
    event_keys = {str(trigger_id): "once:pg-fenced-event"}
    task_id = trigger_daemon._trigger_runtime_task_id_for_event(agent_id, event_keys)
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed fenced trigger integration"):
        db.add(Tenant(id=tenant_id, name="Fenced trigger", slug=f"fenced-trigger-{tenant_id.hex[:8]}"))
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="trigger",
                status="pending",
                parent_agent_id=agent_id,
                priority=2_000_000_000,
                metadata_json={
                    "agent_id": str(agent_id),
                    "trigger_ids": [str(trigger_id)],
                    "fire_event_keys": event_keys,
                },
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="claim fenced trigger integration"):
        claimed = await RuntimeTaskClaimService(
            db=db,
            worker_id="pg-trigger-worker",
            task_types=("trigger",),
            lease_seconds=30,
        ).claim_available(batch_size=100)
    claim = next(task for task in claimed if task.id == task_id)

    trigger = SimpleNamespace(
        id=trigger_id,
        agent_id=agent_id,
        name="fenced-once",
        type="once",
        config={},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
    )

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return list(self.values)

        def scalar_one_or_none(self):
            return self.values[0] if self.values else None

    class Session:
        def __init__(self, values):
            self.values = values

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _query):
            return Result(self.values)

        async def commit(self):
            return None

    sessions = [Session([trigger]), Session([trigger]), Session([trigger])]

    async def fake_resolve_tenant(*_args, **_kwargs):
        return tenant_id

    async def fake_invoke(invoked_agent_id, triggers, *, runtime_task_id=None):
        assert invoked_agent_id == agent_id
        assert triggers == [trigger]
        assert trigger.config["_fire_inflight"]["runtime_task_id"] == task_id.hex
        await trigger_daemon._record_trigger_success_state(agent_id, [trigger_id])
        await trigger_daemon.update_runtime_task_record(
            runtime_task_id,
            status="completed",
            result_summary="fenced trigger completed",
        )

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: sessions.pop(0))
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", fake_invoke)

    assert await run_claimed_runtime_task(
        trigger_daemon.execute_claimed_trigger_runtime_task(task_id),
        task_id=task_id,
        claim_version=claim.claim_version,
        worker_id="pg-trigger-worker",
        lease_seconds=30,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="verify fenced trigger integration"):
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "completed", task.result_summary
        tenant = await db.get(Tenant, tenant_id)
        await db.delete(task)
        await db.flush()
        if tenant is not None:
            await db.delete(tenant)
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_pg_heartbeat_intent_claim_fence_terminals_and_releases(monkeypatch, owner_sessionmaker):
    from app.database import enter_rls_bypass
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services import heartbeat
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task

    monkeypatch.setattr("app.services.runtime_task_service.async_session", owner_sessionmaker)

    tenant_id = uuid4()
    agent_id = uuid4()
    task_id = heartbeat._heartbeat_runtime_task_id_for_event(agent_id, "cadence:pg-fenced")
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed fenced heartbeat integration"):
        db.add(Tenant(id=tenant_id, name="Fenced heartbeat", slug=f"fenced-heartbeat-{tenant_id.hex[:8]}"))
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="heartbeat",
                status="pending",
                parent_agent_id=agent_id,
                priority=2_000_000_000,
                metadata_json={"agent_id": str(agent_id), "tenant_id": str(tenant_id)},
            )
        )
        await db.commit()
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="claim fenced heartbeat integration"):
        claimed = await RuntimeTaskClaimService(
            db=db,
            worker_id="pg-heartbeat-worker",
            task_types=("heartbeat",),
            lease_seconds=30,
        ).claim_available(batch_size=100)
    claim = next(task for task in claimed if task.id == task_id)
    released: list = []

    class EmptyResult:
        def scalar_one_or_none(self):
            return None

    class EmptySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _query):
            return EmptyResult()

    async def fake_acquire(_agent_id, *, now=None):
        return True

    async def fake_release(_agent_id):
        released.append(_agent_id)

    monkeypatch.setattr(heartbeat, "tenant_scoped_session", lambda *a, **k: EmptySession())
    monkeypatch.setattr(heartbeat, "_try_acquire_heartbeat_lease_async", fake_acquire)
    monkeypatch.setattr(heartbeat, "_release_heartbeat_lease_async", fake_release)

    assert await run_claimed_runtime_task(
        heartbeat.execute_claimed_heartbeat_runtime_task(task_id),
        task_id=task_id,
        claim_version=claim.claim_version,
        worker_id="pg-heartbeat-worker",
        lease_seconds=30,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="verify fenced heartbeat integration"):
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "skipped"
        assert released == [agent_id]
        tenant = await db.get(Tenant, tenant_id)
        await db.delete(task)
        await db.flush()
        if tenant is not None:
            await db.delete(tenant)
        await db.commit()


@pytest.mark.usefixtures("migrated_pg_url")
async def test_pg_delegation_intent_claim_fence_executes_child_and_terminals(monkeypatch, owner_sessionmaker):
    from app.agents import orchestrator
    from app.database import enter_rls_bypass
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task

    monkeypatch.setattr("app.services.runtime_task_service.async_session", owner_sessionmaker)
    tenant_id = uuid4()
    agent_id = uuid4()
    target_id = uuid4()
    owner_id = uuid4()
    task_id = uuid4()
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed fenced delegation integration"):
        db.add(Tenant(id=tenant_id, name="Fenced delegation", slug=f"fenced-delegation-{tenant_id.hex[:8]}"))
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="delegation",
                status="pending",
                parent_agent_id=agent_id,
                priority=2_000_000_000,
                child_agent_id=target_id,
                child_session_id="fenced-child-session",
                trace_id="fenced-delegation-trace",
                metadata_json={
                    "tenant_id": str(tenant_id),
                    "owner_id": str(owner_id),
                    "target_agent_id": str(target_id),
                    "conversation_messages": [{"role": "user", "content": "perform fenced child work"}],
                    "tool_profile": "review_readonly",
                },
            )
        )
        await db.commit()
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="claim fenced delegation integration"):
        claimed = await RuntimeTaskClaimService(
            db=db,
            worker_id="pg-delegation-worker",
            task_types=("delegation",),
            lease_seconds=30,
        ).claim_available(batch_size=100)
    claim = next(task for task in claimed if task.id == task_id)

    target = SimpleNamespace(id=target_id, name="Fenced worker", role_description="helper", tenant_id=tenant_id)
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)

    async def fake_resolve(_target_id):
        return target, model

    async def fake_invoke(_invocation):
        return SimpleNamespace(content="fenced child completed")

    async def fake_terminal_evidence(**_kwargs):
        return {"status": "completed", "result_refs": [f"runtime-task://{task_id.hex}"]}

    async def fake_plan_gate(_request):
        return True, "focused_pg_integration"

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orchestrator, "_resolve_resumable_target_runtime", fake_resolve)
    monkeypatch.setattr(orchestrator, "invoke_agent", fake_invoke)
    monkeypatch.setattr(orchestrator, "_delegation_plan_gate_allows", fake_plan_gate)
    monkeypatch.setattr(orchestrator, "_persist_delegation_terminal_evidence", fake_terminal_evidence)
    monkeypatch.setattr(orchestrator, "_settle_delegation_budget", noop_async)
    monkeypatch.setattr(orchestrator, "_project_delegation_completion_to_parent", noop_async)
    monkeypatch.setattr(orchestrator, "persist_invocation_span", noop_async)

    assert await run_claimed_runtime_task(
        orchestrator.dispatch_persisted_async_delegation(task_id.hex),
        task_id=task_id,
        claim_version=claim.claim_version,
        worker_id="pg-delegation-worker",
        lease_seconds=30,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="verify fenced delegation integration"):
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "completed", task.result_summary
        assert task.result_summary == "fenced child completed"
        tenant = await db.get(Tenant, tenant_id)
        await db.delete(task)
        await db.flush()
        if tenant is not None:
            await db.delete(tenant)
        await db.commit()
    orchestrator._async_tasks.clear()


def test_runtime_task_claim_statement_excludes_stopped_budget_runs():
    from app.services.runtime_task_claim_service import build_runtime_task_claim_statement

    stmt = build_runtime_task_claim_statement(
        task_types=("subagent", "delegation"),
        now=datetime(2026, 7, 4, tzinfo=timezone.utc),
        batch_size=10,
    )

    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "runtime_tasks.budget_run_id IS NULL" in compiled
    assert "runtime_budget_runs.status = " in compiled
    assert "EXISTS" in compiled


@pytest.mark.usefixtures("migrated_pg_url")
async def test_runtime_task_claim_rejects_cross_tenant_budget_authority(owner_sessionmaker):
    from app.database import enter_rls_bypass
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    task_tenant_id = uuid4()
    budget_tenant_id = uuid4()
    budget_run_id = uuid4()
    task_id = uuid4()
    async with (
        owner_sessionmaker() as db,
        enter_rls_bypass(
            db,
            reason="test cross-tenant runtime budget claim authority",
        ) as bypass_db,
    ):
        bypass_db.add_all(
            [
                Tenant(id=task_tenant_id, name="Task tenant", slug=f"task-{task_tenant_id.hex[:10]}"),
                Tenant(id=budget_tenant_id, name="Budget tenant", slug=f"budget-{budget_tenant_id.hex[:10]}"),
            ]
        )
        await bypass_db.flush()
        bypass_db.add(
            RuntimeBudgetRun(
                id=budget_run_id,
                tenant_id=budget_tenant_id,
                root_run_kind="subagent",
                root_run_key=f"cross-tenant-{task_id}",
                status="active",
            )
        )
        bypass_db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=task_tenant_id,
                task_type="subagent",
                status="pending",
                parent_agent_id=uuid4(),
                budget_run_id=budget_run_id,
                metadata_json={"test_fixture": "cross_tenant_budget_claim"},
            )
        )
        await bypass_db.commit()

    async with (
        owner_sessionmaker() as db,
        enter_rls_bypass(
            db,
            reason="test cross-tenant runtime budget claim scan",
        ),
    ):
        claimed = await RuntimeTaskClaimService(
            db=db,
            worker_id="cross-tenant-claim-worker",
            task_types=("subagent",),
            lease_seconds=60,
        ).claim_available(batch_size=10)

    assert task_id not in {task.id for task in claimed}
    async with (
        owner_sessionmaker() as db,
        enter_rls_bypass(
            db,
            reason="test cross-tenant runtime budget claim verification",
        ) as bypass_db,
    ):
        task = await bypass_db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "pending"
        budget_run = await bypass_db.get(RuntimeBudgetRun, budget_run_id)
        task_tenant = await bypass_db.get(Tenant, task_tenant_id)
        budget_tenant = await bypass_db.get(Tenant, budget_tenant_id)
        await bypass_db.delete(task)
        await bypass_db.flush()
        if budget_run is not None:
            await bypass_db.delete(budget_run)
        await bypass_db.flush()
        if task_tenant is not None:
            await bypass_db.delete(task_tenant)
        if budget_tenant is not None:
            await bypass_db.delete(budget_tenant)
        await bypass_db.commit()


@pytest.mark.asyncio
async def test_claim_available_marks_tasks_running_with_lease():
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    task = RuntimeTask(
        id=uuid4(),
        task_type="web_chat_turn",
        status="pending",
        parent_agent_id=uuid4(),
        tenant_id=uuid4(),
        parent_session_id=str(uuid4()),
        attempt_count=0,
    )
    db = _FakeDB([task])

    service = RuntimeTaskClaimService(
        db=db,
        worker_id="worker-a",
        task_types=("web_chat_turn",),
        lease_seconds=60,
    )
    claimed = await service.claim_available(batch_size=5)

    assert claimed == [task]
    assert task.status == "running"
    assert task.claimed_by == "worker-a"
    assert task.claim_expires_at is not None
    assert task.claim_expires_at > datetime.now(timezone.utc)
    assert task.started_at is not None
    assert task.attempt_count == 1
    assert task.claim_version == 1
    assert task.metadata_json["claim_version"] == 1
    assert task.metadata_json["claim_fence"] == f"{task.id.hex}:1"
    assert db.commits == 1
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_claim_available_reclaims_expired_running_task_with_new_fence():
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    old_expiry = datetime.now(timezone.utc) - timedelta(seconds=5)
    task = RuntimeTask(
        id=uuid4(),
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=uuid4(),
        tenant_id=uuid4(),
        parent_session_id=str(uuid4()),
        attempt_count=2,
        claim_version=4,
        claimed_by="dead-worker",
        claim_expires_at=old_expiry,
        metadata_json={"claim_version": 4, "claim_fence": "old:4"},
    )
    db = _FakeDB([task])

    claimed = await RuntimeTaskClaimService(
        db=db,
        worker_id="recovery-worker",
        task_types=("web_chat_turn",),
        lease_seconds=60,
    ).claim_available(batch_size=1)

    assert claimed == [task]
    assert task.status == "running"
    assert task.claimed_by == "recovery-worker"
    assert task.claim_version == 5
    assert task.attempt_count == 3
    assert task.metadata_json["reclaimed_expired_claim"] is True
    assert task.metadata_json["previous_claim"]["worker_id"] == "dead-worker"
    assert task.metadata_json["previous_claim"]["claim_version"] == 4
    assert task.metadata_json["claim_fence"] == f"{task.id.hex}:5"


@pytest.mark.asyncio
async def test_workflow_claim_consumes_completed_operator_retry_before_execution():
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    operation_id = uuid4().hex
    task = RuntimeTask(
        id=uuid4(),
        task_type="workflow",
        status="pending",
        parent_agent_id=uuid4(),
        tenant_id=uuid4(),
        parent_session_id=str(uuid4()),
        attempt_count=1,
        claim_version=5,
        claimed_by="operator-reconciler",
        metadata_json={
            "claim_version": 5,
            "needs_reconciliation": False,
            "reconciliation_status": "retry_requested",
            "reconciliation_operation": {
                "schema": "runtime_reconciliation_operation.v2",
                "operation_id": operation_id,
                "status": "completed",
                "action": "retry",
            },
        },
    )

    claimed = await RuntimeTaskClaimService(
        db=_FakeDB([task]),
        worker_id="workflow-retry-worker",
        task_types=("workflow",),
        lease_seconds=60,
    ).claim_available(batch_size=1)

    assert claimed == [task]
    assert task.status == "running"
    assert task.claim_version == 6
    assert "reconciliation_operation" not in task.metadata_json
    assert task.metadata_json["reconciliation_status"] == "retry_in_progress"
    assert task.metadata_json["consumed_reconciliation_operations"][-1]["operation_id"] == operation_id
    assert task.metadata_json["consumed_reconciliation_operations"][-1]["consumed_claim_version"] == 6


@pytest.mark.asyncio
async def test_claim_available_backfills_legacy_running_task_without_lease():
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    task = RuntimeTask(
        id=uuid4(),
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=uuid4(),
        tenant_id=uuid4(),
        parent_session_id=str(uuid4()),
        attempt_count=0,
        claim_version=0,
        claimed_by=None,
        claim_expires_at=None,
    )
    claimed = await RuntimeTaskClaimService(
        db=_FakeDB([task]),
        worker_id="migration-recovery-worker",
        task_types=("web_chat_turn",),
        lease_seconds=60,
    ).claim_available(batch_size=1)

    assert claimed == [task]
    assert task.claim_version == 1
    assert task.metadata_json["legacy_claim_backfilled"] is True
    assert task.metadata_json["claim_fence"] == f"{task.id.hex}:1"


@pytest.mark.asyncio
async def test_business_task_claim_updates_both_state_projections_in_one_commit():
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    task_id = uuid4()
    runtime_task = RuntimeTask(
        id=uuid4(),
        task_type="business_task",
        status="pending",
        parent_agent_id=uuid4(),
        tenant_id=uuid4(),
        metadata_json={"business_task_id": str(task_id), "phase": "queued"},
        attempt_count=0,
    )
    business_task = SimpleNamespace(
        id=task_id,
        agent_id=runtime_task.parent_agent_id,
        tenant_id=runtime_task.tenant_id,
        active_runtime_task_id=runtime_task.id,
        status="pending",
        last_execution_status="queued",
        completed_at=None,
    )
    db = _BusinessTaskDB([runtime_task], business_task)

    claimed = await RuntimeTaskClaimService(
        db=db,
        worker_id="worker-a",
        task_types=("business_task",),
        lease_seconds=60,
    ).claim_available(batch_size=1)

    assert claimed == [runtime_task]
    assert runtime_task.status == "running"
    assert business_task.status == "doing"
    assert business_task.last_execution_status == "running"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_business_task_claim_quarantines_an_invalid_projection_link():
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    runtime_task = RuntimeTask(
        id=uuid4(),
        task_type="business_task",
        status="pending",
        parent_agent_id=uuid4(),
        tenant_id=uuid4(),
        metadata_json={"business_task_id": str(uuid4()), "phase": "queued"},
        attempt_count=0,
    )
    db = _BusinessTaskDB([runtime_task], None)

    claimed = await RuntimeTaskClaimService(
        db=db,
        worker_id="worker-a",
        task_types=("business_task",),
        lease_seconds=60,
    ).claim_available(batch_size=1)

    assert claimed == []
    assert runtime_task.status == "needs_reconciliation"
    assert runtime_task.metadata_json["phase"] == "terminal"
    assert "link" in runtime_task.result_summary
    assert db.commits == 1
