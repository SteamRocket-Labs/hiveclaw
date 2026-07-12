from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return None


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        return _Rows(self.rows)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        return None


def _pair(now: datetime):
    task_id = uuid4()
    runtime_id = uuid4()
    tenant_id = uuid4()
    agent_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        active_runtime_task_id=runtime_id,
        status="doing",
        last_execution_status="running",
        last_error=None,
        last_result=None,
        completed_at=None,
    )
    runtime = SimpleNamespace(
        id=runtime_id,
        task_type="business_task",
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        status="running",
        result_summary=None,
        completed_at=None,
        claim_version=3,
        claimed_by="dead-worker",
        claim_expires_at=now - timedelta(seconds=1),
        metadata_json={"business_task_id": str(task_id), "phase": "invoking"},
    )
    return runtime, task


def test_stale_business_task_query_is_bounded_and_skip_locked() -> None:
    from app.services.business_task_reconciliation import stale_business_task_statement

    statement = stale_business_task_statement(now=datetime(2026, 7, 12, tzinfo=timezone.utc), limit=25)
    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "runtime_tasks.task_type" in compiled
    assert "runtime_tasks.status" in compiled
    assert "runtime_tasks.claim_expires_at" in compiled
    assert "tasks.active_runtime_task_id = runtime_tasks.id" in compiled
    assert "FOR UPDATE OF runtime_tasks SKIP LOCKED" in compiled


async def test_stale_business_task_reconciler_quarantines_without_replay() -> None:
    from app.services.business_task_reconciliation import reconcile_stale_business_tasks_in_session

    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    runtime, task = _pair(now)
    db = _Db([(runtime, task)])

    summary = await reconcile_stale_business_tasks_in_session(db, now=now, limit=10)  # type: ignore[arg-type]

    assert summary == {"checked": 1, "quarantined": 1}
    assert task.status == "needs_reconciliation"
    assert runtime.status == "needs_reconciliation"
    assert runtime.claim_version == 4
    assert db.commits == 1


def test_runtime_worker_consumes_business_task_reconciliation() -> None:
    source = (Path(__file__).resolve().parents[2] / "app" / "services" / "runtime_task_worker.py").read_text(
        encoding="utf-8"
    )

    assert "reconcile_stale_business_tasks_once" in source
    assert "business_tasks_reconciled" in source
