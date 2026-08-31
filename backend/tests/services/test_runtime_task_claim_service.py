from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
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
        # Executable-chat A2A continuation runs must be lease-reclaimable after
        # a worker crash exactly like every other executable-chat type.
        "a2a_continuation",
        "approval_execution",
        "hr_provisioning",
        "dream",
        # Reclaimed delegation execution is fenced again in the worker and
        # mutating profiles are quarantined before hydration or replay.
        "delegation",
        # Added after 2,107 leaseless ``running`` trigger rows accumulated over
        # 38 days with no runtime path able to reach them.
        "trigger",
    ]
    assert snapshot["fence_contract"] == "claim_version+worker_id+lease"


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
async def test_fresh_pending_delegation_claim_clears_old_reclaim_marker():
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    task = RuntimeTask(
        id=uuid4(),
        task_type="delegation",
        status="pending",
        parent_agent_id=uuid4(),
        tenant_id=uuid4(),
        parent_session_id=str(uuid4()),
        metadata_json={"reclaimed_expired_claim": True, "tool_profile": "worker_safe"},
    )

    claimed = await RuntimeTaskClaimService(
        db=_FakeDB([task]),
        worker_id="worker-a",
        task_types=("delegation",),
        lease_seconds=60,
    ).claim_available(batch_size=1)

    assert claimed == [task]
    assert "reclaimed_expired_claim" not in task.metadata_json


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
    assert task.metadata_json["lease_reclaim_count"] == 1
    assert task.metadata_json["previous_claim"]["worker_id"] == "dead-worker"
    assert task.metadata_json["previous_claim"]["claim_version"] == 4
    assert task.metadata_json["claim_fence"] == f"{task.id.hex}:5"


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
async def test_business_task_claim_quarantines_an_invalid_projection_link(monkeypatch):
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
        terminal_boundary_generation=1,
    )
    db = _BusinessTaskDB([runtime_task], None)
    enqueued = []

    async def fake_settle(_db, task, **_kwargs):
        enqueued.append((task.id, task.status))
        task.terminal_boundary_enqueued_at = datetime.now(timezone.utc)

    monkeypatch.setattr(
        "app.services.runtime_terminal_settlement.settle_and_enqueue_runtime_task_terminal",
        fake_settle,
    )

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
    assert enqueued == [(runtime_task.id, "needs_reconciliation")]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_invalid_business_task_claim_commits_terminal_outbox_atomically(owner_sessionmaker):
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    tenant_id, user_id, agent_id, runtime_task_id = (uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Claim quarantine", slug=f"claim-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                username=f"claim-{user_id.hex[:12]}",
                email=f"claim-{user_id.hex[:12]}@test.local",
                password_hash="x",
                display_name="Claim Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Claim Agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            RuntimeTask(
                id=runtime_task_id,
                tenant_id=tenant_id,
                task_type="business_task",
                parent_agent_id=agent_id,
                status="pending",
                prompt="missing business task projection",
                metadata_json={"business_task_id": str(uuid4()), "phase": "queued"},
            )
        )
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        claimed = await RuntimeTaskClaimService(
            db=db,
            worker_id="quarantine-worker",
            task_types=("business_task",),
        ).claim_available(batch_size=1)
    assert claimed == []

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, runtime_task_id)
        outbox = await db.scalar(
            select(RuntimeTerminalBoundaryOutbox).where(
                RuntimeTerminalBoundaryOutbox.runtime_task_id == runtime_task_id
            )
        )
    assert task is not None and task.status == "needs_reconciliation"
    assert task.terminal_boundary_enqueued_at is not None
    assert outbox is not None
    assert outbox.event_kind == "runtime_terminal"
    assert outbox.status == "pending"
