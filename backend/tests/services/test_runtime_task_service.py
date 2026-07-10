from __future__ import annotations

from contextlib import asynccontextmanager
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
    monkeypatch.setattr("app.services.runtime_task_service.tenant_scoped_session", lambda *a, **k: fake_session)

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return tenant_id

    monkeypatch.setattr("app.services.runtime_task_service.resolve_tenant_for_agent", _fake_resolve_tenant)

    async def _fake_admit_tenant(agent_id, *, source, **_kwargs):
        from app.runtime.tenant_admission import RuntimeTenantAdmission, blocked_runtime_tenant_admission

        if tenant_id is None:
            return blocked_runtime_tenant_admission(
                reason_code="agent_tenant_missing",
                message=f"{source} runtime is blocked because agent {agent_id} has no tenant.",
                source=source,
                agent_id=agent_id,
            )
        return RuntimeTenantAdmission(
            ok=True,
            tenant_id=tenant_id,
            status="allowed",
            reason_code="tenant_resolved",
            message=f"{source} runtime tenant resolved.",
            agent_id=agent_id,
            source=source,
        )

    monkeypatch.setattr("app.services.runtime_task_service.admit_agent_runtime_tenant", _fake_admit_tenant)


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


class _CreateSession(_FailingSession):
    def __init__(self):
        super().__init__(fail_on="never")
        self.commit_calls = 0

    async def commit(self):
        self.commit_calls += 1


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _RowResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _LocatorSession:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []
        self.rollback_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query):
        query_text = str(query)
        self.queries.append(query_text)
        if "app.current_tenant_id" in query_text:
            return _RowResult([])
        return _RowResult(self.rows)

    async def rollback(self):
        self.rollback_calls += 1


class _ReconcileSession:
    def __init__(self, tasks):
        self.tasks = tasks
        self.tenant_id = uuid4()
        for task in self.tasks:
            if not hasattr(task, "id"):
                task.id = uuid4()
            if not hasattr(task, "tenant_id"):
                task.tenant_id = self.tenant_id
        self.rollback_calls = 0
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        query_text = str(_query)
        if "app.current_tenant_id" in query_text:
            return _RowResult([])
        if query_text.lstrip().startswith("SELECT runtime_tasks.id, runtime_tasks.tenant_id"):
            return _RowResult([(task.id, task.tenant_id) for task in self.tasks])
        return _ListResult(self.tasks)

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_list_active_runtime_tasks_uses_locator_then_tenant_scoped_reads(monkeypatch):
    from app.services.runtime_task_service import list_active_runtime_task_records

    tenant_a = uuid4()
    tenant_b = uuid4()
    task_a = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "subagent",
            "status": "running",
            "tenant_id": tenant_a,
            "parent_agent_id": None,
            "child_agent_id": None,
            "child_agent_name": None,
            "prompt": "a",
            "result_summary": None,
            "trace_id": None,
            "parent_session_id": None,
            "child_session_id": None,
            "depth": 1,
            "budget_run_id": None,
            "budget_reservation_key": None,
            "budget_admission_status": None,
            "budget_terminal_reason": None,
            "claim_version": 0,
            "root_idempotency_key": "subagent:a",
            "config_snapshot_hash": "config-a",
            "policy_snapshot_hash": "policy-a",
            "metadata_json": {},
            "created_at": None,
            "started_at": None,
            "completed_at": None,
        },
    )()
    task_b = type(
        "RuntimeTaskStub",
        (),
        {
            **{name: getattr(task_a, name) for name in vars(type(task_a)) if not name.startswith("__")},
            "id": uuid4(),
            "tenant_id": tenant_b,
            "prompt": "b",
            "root_idempotency_key": "subagent:b",
        },
    )()
    locator = _LocatorSession([(task_b.id, tenant_b), (task_a.id, tenant_a)])
    scoped_sessions = {
        tenant_a: _ReconcileSession([task_a]),
        tenant_b: _ReconcileSession([task_b]),
    }
    opened_tenants = []

    @asynccontextmanager
    async def _tenant_scope(tenant_id, **_kwargs):
        opened_tenants.append(tenant_id)
        yield scoped_sessions[tenant_id]

    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: locator)
    monkeypatch.setattr("app.services.runtime_task_service.tenant_scoped_session", _tenant_scope)

    records = await list_active_runtime_task_records()

    assert [record["task_id"] for record in records] == [task_b.id.hex, task_a.id.hex]
    assert opened_tenants == [tenant_b, tenant_a]
    locator_selects = [query for query in locator.queries if query.lstrip().startswith("SELECT")]
    assert len(locator_selects) == 1
    assert "runtime_tasks.id" in locator_selects[0]
    assert "runtime_tasks.tenant_id" in locator_selects[0]
    assert "runtime_tasks.prompt" not in locator_selects[0]


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
        query_text = str(_query)
        if "app.current_tenant_id" in query_text:
            return _OneTaskResult(None)
        if query_text.lstrip().startswith("SELECT runtime_tasks.tenant_id"):
            return _OneTaskResult(self.task.tenant_id)
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
async def test_create_runtime_task_record_persists_runtime_budget_metadata(monkeypatch):
    from app.services.runtime_task_service import create_runtime_task_record

    fake_session = _CreateSession()
    _route_runtime_accessors(monkeypatch, fake_session, tenant_id=uuid4())
    budget_run_id = uuid4()

    task_id = await create_runtime_task_record(
        task_id=uuid4().hex,
        task_type="subagent",
        budget_run_id=budget_run_id,
        budget_reservation_key="subagent:child-1",
        budget_admission_status="reserved",
    )

    assert task_id
    task = fake_session.added[0]
    assert task.budget_run_id == budget_run_id
    assert task.budget_reservation_key == "subagent:child-1"
    assert task.budget_admission_status == "reserved"


@pytest.mark.asyncio
async def test_create_runtime_task_record_blocks_parent_agent_without_tenant(monkeypatch):
    from app.runtime.tenant_admission import RuntimeTenantPreconditionError
    from app.services.runtime_task_service import create_runtime_task_record

    fake_session = _CreateSession()
    _route_runtime_accessors(monkeypatch, fake_session, tenant_id=None)

    with pytest.raises(RuntimeTenantPreconditionError) as exc:
        await create_runtime_task_record(
            task_id=uuid4().hex,
            task_type="trigger",
            parent_agent_id=uuid4(),
            metadata_json={"source": "trigger"},
        )

    assert exc.value.status == "blocked_precondition"
    assert exc.value.reason_code == "agent_tenant_missing"
    assert fake_session.added == []
    assert fake_session.commit_calls == 0


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
    assert trigger_task.status == "needs_reconciliation"
    assert trigger_task.metadata_json["needs_reconciliation"] is True
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_preserves_cc_session_runtime_tasks(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    team_member = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "team_member",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    goal_continuation = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "goal_continuation",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    advanced_plan = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "advanced_plan",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {},
        },
    )()
    fake_session = _ReconcileSession([team_member, goal_continuation, advanced_plan])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks()

    assert updated == 0
    assert team_member.status == "running"
    assert goal_continuation.status == "running"
    assert advanced_plan.status == "running"
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_reconciles_unconfirmed_restart_resumable_records(monkeypatch):
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

    assert updated == 2
    assert resumable_delegation.status == "needs_reconciliation"
    assert resumable_delegation.metadata_json["restart_resume_blocker"] == "restart_resume_not_confirmed"
    assert durable_web_chat.status == "running"
    assert in_process_delegation.status == "needs_reconciliation"
    assert in_process_delegation.metadata_json["needs_reconciliation"] is True
    assert in_process_delegation.metadata_json["side_effect_risk"] == "mutating"
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_reconciles_unconfirmed_resumable_subagent_records(monkeypatch):
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

    assert updated == 2
    assert resumable_subagent.status == "needs_reconciliation"
    assert resumable_subagent.metadata_json["restart_resume_blocker"] == "restart_resume_not_confirmed"
    assert unsafe_subagent.status == "needs_reconciliation"
    assert unsafe_subagent.metadata_json["needs_reconciliation"] is True
    assert unsafe_subagent.metadata_json["side_effect_risk"] == "mutating"
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_reconciles_unconfirmed_resumable_trigger_and_heartbeat(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    trigger_id = uuid4()
    heartbeat_id = uuid4()
    resumable_trigger = type(
        "RuntimeTaskStub",
        (),
        {
            "id": trigger_id,
            "task_type": "trigger",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {
                "resume_after_restart": True,
                "resumable_trigger": True,
                "restart_replay_contract": {
                    "schema": "runtime_restart_replay_contract.v1",
                    "task_type": "trigger",
                    "task_id": trigger_id.hex,
                    "idempotency_key": f"trigger:{trigger_id.hex}:restart",
                },
            },
        },
    )()
    resumable_heartbeat = type(
        "RuntimeTaskStub",
        (),
        {
            "id": heartbeat_id,
            "task_type": "heartbeat",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {
                "resume_after_restart": True,
                "resumable_heartbeat": True,
                "restart_replay_contract": {
                    "schema": "runtime_restart_replay_contract.v1",
                    "task_type": "heartbeat",
                    "task_id": heartbeat_id.hex,
                    "idempotency_key": f"heartbeat:{heartbeat_id.hex}:restart",
                },
            },
        },
    )()
    fake_session = _ReconcileSession([resumable_trigger, resumable_heartbeat])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks()

    assert updated == 2
    assert resumable_trigger.status == "needs_reconciliation"
    assert resumable_trigger.metadata_json["restart_resume_blocker"] == "restart_resume_not_confirmed"
    assert resumable_heartbeat.status == "needs_reconciliation"
    assert resumable_heartbeat.metadata_json["restart_resume_blocker"] == "restart_resume_not_confirmed"
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
            "tenant_id": uuid4(),
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
    assert fake_session.commit_calls == 2  # locator audit + tenant-scoped update


@pytest.mark.asyncio
async def test_update_runtime_task_record_marks_needs_reconciliation_completed(monkeypatch):
    from app.services.runtime_task_service import update_runtime_task_record

    task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "trigger",
            "status": "running",
            "started_at": None,
            "completed_at": None,
            "metadata_json": {"side_effect_risk": "mutating"},
            "trace_id": "trigger-trace",
            "child_session_id": None,
            "parent_session_id": None,
            "result_summary": None,
            "tenant_id": uuid4(),
        },
    )()
    fake_session = _UpdateSession(task)
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await update_runtime_task_record(
        uuid4().hex,
        status="needs_reconciliation",
        result_summary="Needs reconciliation",
        metadata_json={"needs_reconciliation": True},
    )

    assert updated is True
    assert task.status == "needs_reconciliation"
    assert task.completed_at is not None
    assert task.metadata_json["needs_reconciliation"] is True
    assert task.metadata_json["completion_journal"][0]["status"] == "needs_reconciliation"
    assert fake_session.commit_calls == 2  # locator audit + tenant-scoped update
