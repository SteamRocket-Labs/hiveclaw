from __future__ import annotations

from uuid import uuid4

import pytest


class _FailingSession:
    def __init__(self, *, fail_on: str):
        self.fail_on = fail_on
        self.rollback_calls = 0
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, value):
        self.added.append(value)

    async def execute(self, _query):
        if self.fail_on == "execute":
            raise RuntimeError("db execute failed")
        raise AssertionError("execute should not be called in this test")

    async def commit(self):
        if self.fail_on == "commit":
            raise RuntimeError("db commit failed")
        raise AssertionError("commit should not be called in this test")

    async def rollback(self):
        self.rollback_calls += 1


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _ReconcileSession:
    def __init__(self, tasks):
        self.tasks = tasks
        self.rollback_calls = 0
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        return _ListResult(self.tasks)

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_create_runtime_task_record_rolls_back_on_commit_error(monkeypatch):
    from app.services.runtime_task_service import create_runtime_task_record

    fake_session = _FailingSession(fail_on="commit")
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    with pytest.raises(RuntimeError, match="db commit failed"):
        await create_runtime_task_record(task_id=uuid4().hex)

    assert fake_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_get_runtime_task_record_rolls_back_on_execute_error(monkeypatch):
    from app.services.runtime_task_service import get_runtime_task_record

    fake_session = _FailingSession(fail_on="execute")
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    with pytest.raises(RuntimeError, match="db execute failed"):
        await get_runtime_task_record(uuid4().hex)

    assert fake_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_list_runtime_task_records_rolls_back_on_execute_error(monkeypatch):
    from app.services.runtime_task_service import list_runtime_task_records

    fake_session = _FailingSession(fail_on="execute")
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    with pytest.raises(RuntimeError, match="db execute failed"):
        await list_runtime_task_records(parent_agent_id=uuid4())

    assert fake_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_marks_running_records_failed(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    running_task = type(
        "RuntimeTaskStub",
        (),
        {
            "status": "running",
            "result_summary": None,
            "completed_at": None,
        },
    )()
    fake_session = _ReconcileSession([running_task])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks()

    assert updated == 1
    assert running_task.status == "failed"
    assert "worker process restarted" in running_task.result_summary.lower()
    assert running_task.completed_at is not None
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_skips_excluded_ids(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    kept_id = uuid4()
    failed_id = uuid4()
    resumable_task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": kept_id,
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    orphaned_task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": failed_id,
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    fake_session = _ReconcileSession([resumable_task, orphaned_task])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks(exclude_task_ids={kept_id.hex})

    assert updated == 1
    assert resumable_task.status == "running"
    assert orphaned_task.status == "failed"
    assert fake_session.commit_calls == 1
