from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest


@pytest.mark.usefixtures("migrated_pg_url")
async def test_two_startup_replicas_cas_requeue_one_expired_runtime_task(owner_sessionmaker):
    from app.database import enter_rls_bypass
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_task_service import requeue_runtime_task_for_worker

    tenant_id = uuid4()
    task_id = uuid4()
    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed startup requeue race"):
        db.add(Tenant(id=tenant_id, name="Startup requeue race", slug=f"startup-requeue-{tenant_id.hex[:8]}"))
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="heartbeat",
                status="running",
                parent_agent_id=uuid4(),
                claimed_by="expired-worker",
                claim_version=4,
                claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                metadata_json={"resume_after_restart": True, "resumable_heartbeat": True},
            )
        )
        await db.commit()

    async def requeue(replica_id: str) -> bool:
        return await requeue_runtime_task_for_worker(
            task_id.hex,
            task_type="heartbeat",
            expected_status="running",
            expected_claim_version=4,
            expected_claim_worker_id="expired-worker",
            metadata_json={"startup_requeued_by": replica_id},
            session_factory=owner_sessionmaker,
        )

    results = await asyncio.gather(requeue("startup-a"), requeue("startup-b"))
    assert sorted(results) == [False, True]

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="verify startup requeue race"):
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        assert task.status == "resumable"
        assert task.claimed_by is None
        assert task.claim_expires_at is None
        assert task.claim_version == 4
        assert task.metadata_json["startup_requeued_by"] in {"startup-a", "startup-b"}
        tenant = await db.get(Tenant, tenant_id)
        await db.delete(task)
        await db.flush()
        if tenant is not None:
            await db.delete(tenant)
        await db.commit()


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
            "claimed_by": "foreground-subagent:test",
            "claim_expires_at": datetime(2026, 7, 13, tzinfo=timezone.utc),
            "claim_version": 1,
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
    assert records[1]["claimed_by"] == "foreground-subagent:test"
    assert records[1]["claim_expires_at"] == "2026-07-13T00:00:00+00:00"
    assert records[1]["claim_version"] == 1
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
async def test_terminal_runtime_task_enqueues_completion_in_same_transaction(monkeypatch):
    from app.services.runtime_notification_outbox import CompletionNotification
    from app.services.runtime_task_service import update_runtime_task_record

    tenant_id = uuid4()
    task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "task_type": "trigger",
            "status": "running",
            "metadata_json": {},
            "started_at": None,
            "completed_at": None,
            "trace_id": "trace",
            "result_summary": None,
            "claim_version": 0,
        },
    )()
    fake_session = _UpdateSession(task)
    _route_runtime_accessors(monkeypatch, fake_session, tenant_id=tenant_id)

    async def fake_resolve_runtime_task_tenant(*_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(
        "app.services.runtime_task_service.resolve_tenant_for_runtime_task",
        fake_resolve_runtime_task_tenant,
    )
    captured = {}

    async def fake_enqueue(db, notification):
        captured["db"] = db
        captured["notification"] = notification
        captured["commit_calls_at_enqueue"] = fake_session.commit_calls
        return uuid4()

    monkeypatch.setattr(
        "app.services.runtime_task_service.enqueue_completion_notification",
        fake_enqueue,
        raising=False,
    )
    notification = CompletionNotification(
        tenant_id=tenant_id,
        source_kind="trigger",
        source_run_id=str(task.id),
        parent_session_id=uuid4(),
        parent_agent_id=uuid4(),
        parent_user_id=uuid4(),
        terminal_status="completed",
        task_type="trigger",
        summary="done",
        delivery_mode="session_projection",
    )

    updated = await update_runtime_task_record(
        task.id.hex,
        status="completed",
        result_summary="done",
        completion_notification=notification,
    )

    assert updated is True
    assert captured["db"] is fake_session
    assert captured["notification"] is notification
    assert captured["commit_calls_at_enqueue"] == 0
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_update_runtime_task_record_rejects_stale_inline_claim_terminal_overwrite(monkeypatch):
    from app.services.runtime_task_service import update_runtime_task_record

    tenant_id = uuid4()
    task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "task_type": "a2a_delegation",
            "status": "needs_reconciliation",
            "metadata_json": {"needs_reconciliation": True},
            "started_at": datetime.now(timezone.utc),
            "completed_at": None,
            "trace_id": "trace-reconciled",
            "result_summary": "unknown outcome",
            "claim_version": 2,
            "claimed_by": "startup-reconciler:2",
            "claim_expires_at": None,
        },
    )()
    fake_session = _UpdateSession(task)
    _route_runtime_accessors(monkeypatch, fake_session, tenant_id=tenant_id)

    async def fake_resolve_runtime_task_tenant(*_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(
        "app.services.runtime_task_service.resolve_tenant_for_runtime_task",
        fake_resolve_runtime_task_tenant,
    )

    updated = await update_runtime_task_record(
        task.id.hex,
        expected_status="running",
        expected_claim_version=1,
        expected_claim_worker_id="a2a-inline:old",
        status="completed",
        result_summary="late success",
    )

    assert updated is False
    assert task.status == "needs_reconciliation"
    assert task.result_summary == "unknown outcome"
    assert task.metadata_json == {"needs_reconciliation": True}


@pytest.mark.asyncio
async def test_create_runtime_task_record_rolls_back_on_commit_error(monkeypatch):
    from app.services.runtime_task_service import create_runtime_task_record

    fake_session = _FailingSession(fail_on="commit")
    _route_runtime_accessors(monkeypatch, fake_session, tenant_id=uuid4())

    with pytest.raises(RuntimeError, match="db commit failed"):
        await create_runtime_task_record(task_id=uuid4().hex, parent_agent_id=uuid4())

    assert fake_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_create_runtime_task_record_persists_runtime_budget_metadata(monkeypatch):
    from app.services.runtime_task_service import create_runtime_task_record

    fake_session = _CreateSession()
    _route_runtime_accessors(monkeypatch, fake_session, tenant_id=uuid4())
    budget_run_id = uuid4()
    root_user_id = uuid4()
    root_session_id = str(uuid4())
    root_runtime_task_id = uuid4()
    parent_agent_id = uuid4()

    task_id = await create_runtime_task_record(
        task_id=uuid4().hex,
        task_type="subagent",
        parent_agent_id=parent_agent_id,
        budget_run_id=budget_run_id,
        budget_reservation_key="subagent:child-1",
        budget_admission_status="reserved",
        root_user_id=root_user_id,
        root_session_id=root_session_id,
        root_runtime_task_id=root_runtime_task_id,
        delegation_chain=[f"agent:{parent_agent_id}", "subagent:scout"],
    )

    assert task_id
    task = fake_session.added[0]
    assert task.budget_run_id == budget_run_id
    assert task.budget_reservation_key == "subagent:child-1"
    assert task.budget_admission_status == "reserved"
    assert task.root_user_id == root_user_id
    assert task.root_session_id == root_session_id
    assert task.root_runtime_task_id == root_runtime_task_id
    assert task.delegation_chain_json == [f"agent:{parent_agent_id}", "subagent:scout"]


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
            "claim_version": 3,
            "claimed_by": "delegation-worker:startup-expired",
            "claim_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
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
    assert resumable_delegation.claim_version == 3
    assert resumable_delegation.claimed_by == "delegation-worker:startup-expired"
    assert "recovery_evidence_status" not in resumable_delegation.metadata_json
    assert "recovery_tool_frames" not in resumable_delegation.metadata_json
    assert durable_web_chat.status == "running"
    assert in_process_delegation.status == "needs_reconciliation"
    assert in_process_delegation.metadata_json["needs_reconciliation"] is True
    assert in_process_delegation.metadata_json["side_effect_risk"] == "mutating"
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_quarantines_sync_a2a_unknown_outcome(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks
    from app.services.runtime_reconciliation import _canonical_recovery_evidence

    runtime_task_id = uuid4()
    a2a_task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": runtime_task_id,
            "task_type": "a2a_delegation",
            "status": "running",
            "trace_id": "trace-a2a",
            "parent_session_id": "root-session",
            "child_session_id": "pair-session",
            "result_summary": None,
            "completed_at": None,
            "claim_version": 1,
            "claimed_by": "a2a-inline:expired",
            "claim_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
            "metadata_json": {
                "interaction_type": "agent_message",
                "execution_backend": "foreground_inline",
                "resume_after_restart": False,
                "restart_resume_blocker": "custom_tool_executor_not_replayable",
                "side_effect_risk": "mutating",
                "recovery_agent_id": str(uuid4()),
                "recovery_session_id": "pair-session",
                "recovery_runtime_task_id": str(runtime_task_id),
            },
        },
    )()
    live_a2a_task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "a2a_delegation",
            "status": "running",
            "trace_id": "trace-a2a-live",
            "parent_session_id": "root-session-live",
            "child_session_id": "pair-session-live",
            "result_summary": None,
            "completed_at": None,
            "claim_version": 7,
            "claimed_by": "a2a-inline:live",
            "claim_expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "metadata_json": {
                "interaction_type": "agent_message",
                "execution_backend": "foreground_inline",
                "resume_after_restart": False,
                "restart_resume_blocker": "custom_tool_executor_not_replayable",
                "side_effect_risk": "mutating",
            },
        },
    )()
    fake_session = _ReconcileSession([a2a_task, live_a2a_task])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)
    inspection_calls = []

    def fake_inspect_recovery_manifest_checkpoint(**kwargs):
        inspection_calls.append(kwargs)
        return {
            "state": "valid",
            "receipt": {
                "ref": "runtime_artifacts/recovery_manifests/pair-session/manifest.json",
                "sha256": "a" * 64,
            },
            "expected_checkpoint_seq": 8,
            "expected_claim_version": 1,
            "expected_claim_worker_id": "a2a-inline:expired",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-send-email",
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
        fake_inspect_recovery_manifest_checkpoint,
    )

    updated = await reconcile_orphaned_runtime_tasks()

    assert updated == 1
    assert inspection_calls == [
        {
            "agent_id": a2a_task.metadata_json["recovery_agent_id"],
            "tenant_id": a2a_task.tenant_id,
            "session_id": "pair-session",
            "runtime_task_id": runtime_task_id,
        }
    ]
    assert a2a_task.status == "needs_reconciliation"
    assert a2a_task.metadata_json["needs_reconciliation"] is True
    assert a2a_task.metadata_json["side_effect_risk"] == "mutating"
    assert a2a_task.metadata_json["restart_resume_blocker"] == "custom_tool_executor_not_replayable"
    assert a2a_task.metadata_json["recovery_runtime_task_id"] == str(runtime_task_id)
    assert "may have performed external side effects" in a2a_task.result_summary
    assert a2a_task.claim_version == 2
    assert a2a_task.claimed_by.startswith("startup-reconciler:")
    assert a2a_task.claim_expires_at is None
    assert a2a_task.metadata_json["recovery_manifest_sha256"] == "a" * 64
    assert a2a_task.metadata_json["recovery_tool_frames"] == [
        {
            "runtime_task_id": str(runtime_task_id),
            "tool_call_id": "call-send-email",
            "tool_name": "send_email",
            "status": "needs_reconciliation",
            "event_type": "recovered_inline_a2a_tool_frame",
            "reason": "expired_inline_a2a_tool_outcome_unknown",
        }
    ]
    evidence = _canonical_recovery_evidence(a2a_task)
    assert evidence["evidence_complete"] is True
    assert evidence["frames"][0]["tool_call_id"] == "call-send-email"
    assert live_a2a_task.status == "running"
    assert live_a2a_task.claim_version == 7


@pytest.mark.asyncio
async def test_periodic_orphan_reconcile_can_scope_to_expired_inline_a2a(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    def running_task(task_type: str):
        return type(
            "RuntimeTaskStub",
            (),
            {
                "id": uuid4(),
                "task_type": task_type,
                "status": "running",
                "trace_id": f"trace-{task_type}",
                "parent_session_id": f"parent-{task_type}",
                "child_session_id": f"child-{task_type}",
                "result_summary": None,
                "completed_at": None,
                "claim_version": 1,
                "claimed_by": f"worker-{task_type}",
                "claim_expires_at": expired_at,
                "metadata_json": {"side_effect_risk": "mutating"},
            },
        )()

    a2a_task = running_task("a2a_delegation")
    delegation_task = running_task("delegation")
    fake_session = _ReconcileSession([a2a_task, delegation_task])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks(task_types={"a2a_delegation"})

    assert updated == 1
    assert a2a_task.status == "needs_reconciliation"
    assert delegation_task.status == "running"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_startup_lane_scope_filters_before_limit_and_empty_orphan_scope_is_noop(
    owner_sessionmaker,
    monkeypatch,
):
    from sqlalchemy import delete

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.runtime_task_service import (
        list_active_runtime_task_records,
        reconcile_orphaned_runtime_tasks,
    )
    import app.services.runtime_task_service as runtime_task_service

    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)

    tenant_id, user_id, agent_id = uuid4(), uuid4(), uuid4()
    oldest = datetime.now(timezone.utc) - timedelta(hours=2)
    lane_ids = {
        "delegation": uuid4(),
        "trigger": uuid4(),
        "heartbeat": uuid4(),
    }
    empty_scope_target_id = uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="startup-lane-scope", slug=f"sls-{tenant_id.hex[:10]}"))
        await db.flush()
        db.add(
            User(
                id=user_id,
                username=f"sls-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Startup Scope Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="startup-scope-agent",
                role_description="scope tests",
                creator_id=user_id,
            )
        )
        await db.flush()
        db.add_all(
            [
                RuntimeTask(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="pending",
                    parent_agent_id=agent_id,
                    root_user_id=user_id,
                    created_at=oldest + timedelta(seconds=index),
                    metadata_json={"resume_after_restart": True},
                )
                for index in range(50)
            ]
        )
        for offset, (task_type, task_id) in enumerate(lane_ids.items(), start=60):
            db.add(
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type=task_type,
                    status="pending",
                    parent_agent_id=agent_id,
                    root_user_id=user_id,
                    created_at=oldest + timedelta(seconds=offset),
                    metadata_json={"resume_after_restart": True, f"resumable_{task_type}": True},
                )
            )
        db.add(
            RuntimeTask(
                id=empty_scope_target_id,
                tenant_id=tenant_id,
                task_type="a2a_delegation",
                status="running",
                parent_agent_id=agent_id,
                root_user_id=user_id,
                claimed_by="expired-a2a-worker",
                claim_version=1,
                claim_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                created_at=oldest + timedelta(minutes=5),
                metadata_json={"side_effect_risk": "mutating"},
            )
        )

    try:
        global_records = await list_active_runtime_task_records(
            task_types=None,
            limit=None,
            session_factory=owner_sessionmaker,
        )
        own_global_ids = {record["task_id"] for record in global_records if record.get("tenant_id") == str(tenant_id)}
        assert own_global_ids == {
            *(task_id.hex for task_id in lane_ids.values()),
            empty_scope_target_id.hex,
            *(
                record["task_id"]
                for record in global_records
                if record.get("tenant_id") == str(tenant_id) and record.get("task_type") == "workflow"
            ),
        }
        assert len(own_global_ids) == 54
        assert (
            await list_active_runtime_task_records(
                task_types=(),
                limit=None,
                session_factory=owner_sessionmaker,
            )
            == []
        )

        for task_type, task_id in lane_ids.items():
            records = await list_active_runtime_task_records(
                task_types=(task_type,),
                limit=1,
                session_factory=owner_sessionmaker,
            )
            assert [record["task_id"] for record in records] == [task_id.hex]

        assert await reconcile_orphaned_runtime_tasks(task_types=()) == 0
        async with owner_sessionmaker() as db:
            target = await db.get(RuntimeTask, empty_scope_target_id)
        assert target is not None
        assert target.status == "running"
    finally:
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            await db.execute(delete(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id))


@pytest.mark.asyncio
async def test_missing_a2a_manifest_still_projects_explicit_unknown_outcome_frame(monkeypatch):
    from app.services.runtime_reconciliation import _canonical_recovery_evidence
    from app.services.runtime_task_service import _project_expired_inline_a2a_evidence

    task_id = uuid4()
    tenant_id = uuid4()
    target_agent_id = uuid4()
    task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": task_id,
            "tenant_id": tenant_id,
            "child_agent_id": target_agent_id,
            "child_session_id": "pair-session-missing",
            "metadata_json": {},
        },
    )()

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.inspect_recovery_manifest_checkpoint",
        lambda **_kwargs: None,
    )

    metadata = await _project_expired_inline_a2a_evidence(
        task,
        {
            "recovery_agent_id": str(target_agent_id),
            "recovery_session_id": "pair-session-missing",
            "recovery_runtime_task_id": str(task_id),
        },
    )
    task.metadata_json = metadata

    assert metadata["recovery_manifest_state"] == "missing"
    assert metadata["recovery_tool_frames"][0]["tool_name"] == "a2a_agent_message"
    assert metadata["recovery_tool_frames"][0]["reason"] == "expired_inline_a2a_manifest_missing"
    evidence = _canonical_recovery_evidence(task)
    assert evidence["evidence_complete"] is True
    assert evidence["incomplete_reasons"] == []


@pytest.mark.asyncio
async def test_missing_a2a_manifest_refresh_clears_stale_sha_claim_cas_and_frames():
    from app.services.runtime_task_service import _project_expired_inline_a2a_evidence

    task_id = uuid4()
    tenant_id = uuid4()
    target_agent_id = uuid4()
    task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": task_id,
            "tenant_id": tenant_id,
            "child_agent_id": target_agent_id,
            "child_session_id": "pair-session-deleted",
        },
    )()
    metadata = {
        "recovery_agent_id": str(target_agent_id),
        "recovery_session_id": "pair-session-deleted",
        "recovery_runtime_task_id": str(task_id),
        "recovery_manifest_ref": "runtime_artifacts/recovery_manifests/old.json",
        "recovery_manifest_sha256": "a" * 64,
        "recovery_resolution_targets": [
            {
                "agent_id": str(target_agent_id),
                "session_id": "pair-session-deleted",
                "runtime_task_id": str(task_id),
                "source": "current_run",
                "expected_manifest_ref": "runtime_artifacts/recovery_manifests/old.json",
                "expected_sha256": "a" * 64,
                "expected_checkpoint_seq": 4,
                "expected_claim_version": 1,
                "expected_claim_worker_id": "a2a-inline:old",
            }
        ],
        "recovery_tool_frames": [
            {
                "runtime_task_id": str(task_id),
                "tool_call_id": "call-old",
                "tool_name": "send_email",
                "status": "needs_reconciliation",
            }
        ],
    }

    projected = await _project_expired_inline_a2a_evidence(
        task,
        metadata,
        inspection=None,
    )

    target = projected["recovery_resolution_targets"][0]
    assert projected["recovery_manifest_state"] == "missing"
    assert "recovery_manifest_ref" not in projected
    assert "recovery_manifest_sha256" not in projected
    assert target["expected_manifest_ref"] is None
    assert target["expected_sha256"] is None
    assert "expected_checkpoint_seq" not in target
    assert "expected_claim_version" not in target
    assert "expected_claim_worker_id" not in target
    assert [frame["tool_call_id"] for frame in projected["recovery_tool_frames"]] != ["call-old"]
    assert projected["recovery_tool_frames"][0]["reason"] == "expired_inline_a2a_manifest_missing"


@pytest.mark.asyncio
async def test_reconcile_orphaned_runtime_tasks_reconciles_unconfirmed_resumable_subagent_records(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    terminalized = []

    async def fake_apply_locked_subagent_terminal_protocol(
        _db,
        task,
        *,
        status,
        summary,
        blocker,
        metadata_json,
    ):
        terminalized.append((task.id, status, blocker))
        task.status = status
        task.result_summary = summary
        task.metadata_json = metadata_json
        task.completed_at = datetime.now(timezone.utc)

    monkeypatch.setattr(
        "app.services.subagent_run_service.apply_locked_subagent_terminal_protocol",
        fake_apply_locked_subagent_terminal_protocol,
    )

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
    assert terminalized == [
        (resumable_subagent.id, "needs_reconciliation", "restart_resume_not_confirmed"),
        (unsafe_subagent.id, "needs_reconciliation", "non_idempotent_restart_orphan"),
    ]
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
