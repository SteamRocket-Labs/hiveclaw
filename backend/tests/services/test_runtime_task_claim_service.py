from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql


class _ScalarListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


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
    assert db.commits == 1
    assert db.rollbacks == 0
