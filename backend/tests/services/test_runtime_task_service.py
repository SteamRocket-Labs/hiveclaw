from __future__ import annotations

from uuid import uuid4

import pytest


def _route_runtime_accessors(monkeypatch, fake_session, *, tenant_id=None):
    """Route the stage-2b RLS accessors onto the test's fake session.

    runtime_task_service migrated off bare ``async_session`` onto
    ``tenant_scoped_session`` (RLS-GUC pinned) / ``enter_rls_bypass`` (audited
    single-row) and the ``resolve_tenant_for_agent`` bypass read. Tests that
    mocked only ``async_session`` must also redirect these so the accessor sites
    use the fake session instead of opening a real connection.
    """
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)
    monkeypatch.setattr(
        "app.services.runtime_task_service.tenant_scoped_session", lambda *a, **k: fake_session
    )

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return tenant_id

    monkeypatch.setattr("app.services.runtime_task_service.resolve_tenant_for_agent", _fake_resolve_tenant)


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
        # RLS GUC statements (SET LOCAL app.current_tenant_id = ...) emitted by
        # tenant_scoped_session / enter_rls_bypass must not trip the failure path
        # nor the "should not be called" guard — they are infra, not the query.
        if "app.current_tenant_id" in str(_query):
            return None
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


class _OneTaskResult:
    def __init__(self, task):
        self._task = task

    def scalar_one_or_none(self):
        return self._task


class _UpdateSession:
    def __init__(self, task):
        self.task = task
        self.commit_calls = 0
        self.rollback_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        return _OneTaskResult(self.task)

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_create_runtime_task_record_rolls_back_on_commit_error(monkeypatch):
    from app.services.runtime_task_service import create_runtime_task_record

    fake_session = _FailingSession(fail_on="commit")
    _route_runtime_accessors(monkeypatch, fake_session)

    with pytest.raises(RuntimeError, match="db commit failed"):
        await create_runtime_task_record(task_id=uuid4().hex)

    assert fake_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_get_runtime_task_record_rolls_back_on_execute_error(monkeypatch):
    from app.services.runtime_task_service import get_runtime_task_record

    fake_session = _FailingSession(fail_on="execute")
    _route_runtime_accessors(monkeypatch, fake_session)

    with pytest.raises(RuntimeError, match="db execute failed"):
        await get_runtime_task_record(uuid4().hex)

    assert fake_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_list_runtime_task_records_rolls_back_on_execute_error(monkeypatch):
    from app.services.runtime_task_service import list_runtime_task_records

    fake_session = _FailingSession(fail_on="execute")
    _route_runtime_accessors(monkeypatch, fake_session, tenant_id=uuid4())

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
async def test_reconcile_orphaned_runtime_tasks_preserves_workflow_runs(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    workflow_task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "workflow",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    trigger_task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "trigger",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    fake_session = _ReconcileSession([workflow_task, trigger_task])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks()

    assert updated == 1
    assert workflow_task.status == "running"
    assert workflow_task.completed_at is None
    assert trigger_task.status == "failed"
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_preserves_restart_resumable_records(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    resumable_delegation = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "delegation",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {
                "resume_after_restart": True,
                "resumable_delegation": True,
            },
        },
    )()
    durable_web_chat = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "web_chat_turn",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    in_process_delegation = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "delegation",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    fake_session = _ReconcileSession([resumable_delegation, durable_web_chat, in_process_delegation])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks()

    assert updated == 1
    assert resumable_delegation.status == "running"
    assert durable_web_chat.status == "running"
    assert in_process_delegation.status == "failed"
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_preserves_resumable_subagent_records(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    resumable_subagent = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "subagent",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {
                "resume_after_restart": True,
                "resumable_subagent": True,
            },
        },
    )()
    unsafe_subagent = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "subagent",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {
                "resume_after_restart": False,
                "resumable_subagent": False,
            },
        },
    )()
    fake_session = _ReconcileSession([resumable_subagent, unsafe_subagent])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks()

    assert updated == 1
    assert resumable_subagent.status == "running"
    assert unsafe_subagent.status == "failed"
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


@pytest.mark.asyncio
async def test_update_runtime_task_record_marks_skipped_completed(monkeypatch):
    from app.services.runtime_task_service import update_runtime_task_record

    task = type(
        "RuntimeTaskStub",
        (),
        {
            "status": "running",
            "started_at": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    fake_session = _UpdateSession(task)
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await update_runtime_task_record(
        uuid4().hex,
        status="skipped",
        result_summary="Skipped: no model configured.",
        metadata_json={"skip_reason": "no_model"},
    )

    assert updated is True
    assert task.status == "skipped"
    assert task.completed_at is not None
    assert task.metadata_json["skip_reason"] == "no_model"
    assert fake_session.commit_calls == 1
