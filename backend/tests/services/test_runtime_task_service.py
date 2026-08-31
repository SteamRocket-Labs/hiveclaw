from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select


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
        if "session_writer_epochs" in str(_query):
            return _OneTaskResult(
                type(
                    "SessionWriterEpochStub",
                    (),
                    {
                        "state": "legacy_open",
                        "new_run_generation": 1,
                        "allowed_existing_generations_json": [1, 2],
                        "enforcement_mode": "observe",
                        "version": 1,
                        "release_id": None,
                    },
                )()
            )
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
            for name, value in {
                "metadata_json": {},
                "claim_version": 0,
                "root_runtime_task_id": None,
                "parent_session_id": None,
                "parent_agent_id": None,
                "terminal_boundary_generation": None,
            }.items():
                if not hasattr(task, name):
                    setattr(task, name, value)
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

    async def flush(self):
        return None

    async def rollback(self):
        self.rollback_calls += 1


def test_runtime_task_projection_includes_claim_owner_and_expiry_for_startup_recovery() -> None:
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_service import _task_to_dict

    claim_expires_at = datetime.now(timezone.utc)
    task = RuntimeTask(
        id=uuid4(),
        tenant_id=uuid4(),
        task_type="delegation",
        status="running",
        claimed_by="runtime-worker-a",
        claim_expires_at=claim_expires_at,
        claim_version=4,
        metadata_json={"resumable_delegation": True},
    )

    projected = _task_to_dict(task)

    assert projected["claimed_by"] == "runtime-worker-a"
    assert projected["claim_expires_at"] == claim_expires_at.isoformat()


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
        for name, value in {
            "id": uuid4(),
            "claim_version": 0,
            "root_runtime_task_id": None,
            "parent_session_id": None,
            "parent_agent_id": None,
            "terminal_boundary_generation": None,
        }.items():
            if not hasattr(task, name):
                setattr(task, name, value)
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

    async def flush(self):
        return None


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

    async def fake_terminal_settlement(db, received_task, **kwargs):
        captured["terminal_db"] = db
        captured["terminal_task"] = received_task
        captured["terminal_kwargs"] = kwargs
        captured["commit_calls_at_terminal_enqueue"] = fake_session.commit_calls
        return "runtime-task-terminal:test"

    monkeypatch.setattr(
        "app.services.runtime_task_service.enqueue_completion_notification",
        fake_enqueue,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.runtime_terminal_settlement.settle_and_enqueue_runtime_task_terminal",
        fake_terminal_settlement,
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
    assert captured["terminal_db"] is fake_session
    assert captured["terminal_task"] is task
    assert captured["terminal_kwargs"]["terminal_source"] == "runtime_task_service.update"
    assert captured["commit_calls_at_terminal_enqueue"] == 0
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "metadata", "expected_disposition"),
    [
        ("skipped", {}, "intentional_no_delivery"),
        ("completed", {"delivery": "workflow"}, "workflow_child_owned"),
        ("failed", {"delivery": "workflow"}, "workflow_child_owned"),
        ("needs_reconciliation", {"delivery": "workflow"}, "workflow_child_owned"),
    ],
)
async def test_trigger_without_parent_delivery_is_durably_settled_but_keeps_terminal_boundary(
    monkeypatch,
    status,
    metadata,
    expected_disposition,
):
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
            "metadata_json": metadata,
            "started_at": None,
            "completed_at": None,
            "trace_id": "trace",
            "result_summary": None,
            "claim_version": 0,
            "root_runtime_task_id": None,
            "completion_outbox_generation": 1,
            "completion_outbox_settled_at": None,
            "completion_outbox_attempted_at": None,
            "completion_outbox_attempt_count": 0,
            "completion_outbox_last_error": None,
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
    terminal_enqueues = []

    async def fake_terminal_settlement(db, received_task, **_kwargs):
        terminal_enqueues.append((db, received_task, fake_session.commit_calls))
        return "runtime-task-terminal:test"

    monkeypatch.setattr(
        "app.services.runtime_terminal_settlement.settle_and_enqueue_runtime_task_terminal",
        fake_terminal_settlement,
    )

    updated = await update_runtime_task_record(
        task.id.hex,
        status=status,
        result_summary="Trigger conditions did not require a run.",
    )

    assert updated is True
    assert task.completion_outbox_settled_at is not None
    assert task.completion_outbox_last_error is None
    assert task.metadata_json["completion_delivery_disposition"] == expected_disposition
    assert terminal_enqueues == [(fake_session, task, 0)]
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_postgres_trigger_settlement_rolls_back_with_terminal_outbox_and_replays_once(
    monkeypatch,
    owner_sessionmaker,
):
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.audit import AuditLog
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.trigger import AgentTrigger
    from app.models.user import User
    from app.services import direct_invocation_terminal_boundary_processor as terminal_processor
    from app.services import runtime_task_service

    tenant_id, user_id, agent_id, trigger_id, task_id = (uuid4() for _ in range(5))
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Atomic trigger settlement",
                slug=f"atomic-trigger-{tenant_id.hex[:10]}",
            )
        )
        db.add(
            User(
                id=user_id,
                username=f"atomic-{user_id.hex[:10]}",
                email=f"atomic-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Atomic Trigger Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Atomic trigger agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            AgentTrigger(
                id=trigger_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                name="Atomic once",
                type="once",
                config={
                    "at": "2026-08-31T00:00:00+00:00",
                    "_fire_inflight": {
                        "event_key": "once:atomic",
                        "runtime_task_id": str(task_id),
                        "started_at": "2026-08-31T00:00:00+00:00",
                    },
                },
                reason="Prove atomic settlement",
                is_enabled=True,
                fire_count=0,
                cooldown_seconds=0,
            )
        )
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="trigger",
                status="running",
                parent_agent_id=agent_id,
                metadata_json={
                    "delivery": "workflow",
                    "trigger_ids": [str(trigger_id)],
                    "trigger_names": ["Atomic once"],
                    "trigger_types": ["once"],
                },
            )
        )

    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)
    original_enqueue = terminal_processor.enqueue_direct_terminal_boundary_for_task
    expected_audit_id = uuid5(task_id, "trigger-settlement-audit")

    async def fail_after_settlement(*_args, **_kwargs):
        raise RuntimeError("terminal outbox unavailable")

    monkeypatch.setattr(terminal_processor, "enqueue_direct_terminal_boundary_for_task", fail_after_settlement)
    with pytest.raises(RuntimeError, match="terminal outbox unavailable"):
        await runtime_task_service.update_runtime_task_record(
            task_id.hex,
            status="completed",
            result_summary="Workflow launched.",
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        rolled_back_task = await db.get(RuntimeTask, task_id)
        rolled_back_trigger = await db.get(AgentTrigger, trigger_id)
        assert rolled_back_task.status == "running"
        assert "trigger_settlement" not in (rolled_back_task.metadata_json or {})
        assert rolled_back_trigger.fire_count == 0
        assert rolled_back_trigger.is_enabled is True
        assert rolled_back_trigger.config["_fire_inflight"]["runtime_task_id"] == str(task_id)
        assert await db.get(AuditLog, expected_audit_id) is None

    monkeypatch.setattr(terminal_processor, "enqueue_direct_terminal_boundary_for_task", original_enqueue)
    assert await runtime_task_service.update_runtime_task_record(
        task_id.hex,
        status="completed",
        result_summary="Workflow launched.",
    )
    assert await runtime_task_service.update_runtime_task_record(task_id.hex, status="completed")
    assert not await runtime_task_service.update_runtime_task_record(
        task_id.hex,
        status="completed",
        result_summary="Late replacement must not cross the terminal seal.",
    )
    assert await runtime_task_service.update_runtime_task_record(
        task_id.hex,
        budget_admission_status="settled",
    )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        settled_task = await db.get(RuntimeTask, task_id)
        settled_trigger = await db.get(AgentTrigger, trigger_id)
        assert settled_task.status == "completed"
        assert settled_task.result_summary == "Workflow launched."
        assert settled_task.budget_admission_status == "settled"
        assert settled_task.terminal_boundary_enqueued_at is not None
        assert settled_task.metadata_json["terminal_committed_status"] == "completed"
        assert settled_task.metadata_json["terminal_commit_source"] == "runtime_task_service.update"
        assert settled_task.metadata_json["terminal_execution_fence_ref"].startswith("runtime-task-terminal:")
        assert settled_task.metadata_json["trigger_settlement"]["trigger_outcomes"] == {str(trigger_id): "success"}
        assert settled_trigger.fire_count == 1
        assert settled_trigger.is_enabled is False
        assert "_fire_inflight" not in settled_trigger.config
        audit_rows = list((await db.execute(select(AuditLog).where(AuditLog.id == expected_audit_id))).scalars())
        assert len(audit_rows) == 1
        assert audit_rows[0].id == UUID(settled_task.metadata_json["trigger_settlement"]["audit_log_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_outcome"),
    [("failed", "failure"), ("needs_reconciliation", "hold"), ("skipped", "release")],
)
async def test_trigger_terminal_outcomes_preserve_failure_hold_and_release_contract(status, expected_outcome):
    from datetime import datetime, timedelta, timezone

    from app.services.runtime_task_service import _settle_trigger_runtime_task
    from app.services.trigger_daemon import _inflight_fire_is_active

    task_id, tenant_id, agent_id, trigger_id = (uuid4() for _ in range(4))
    trigger = SimpleNamespace(
        id=trigger_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        name="terminal outcome",
        type="once",
        config={
            "_fire_inflight": {
                "runtime_task_id": str(task_id),
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        },
        fire_count=0,
        max_fires=None,
        is_enabled=True,
        last_fired_at=None,
    )
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        metadata_json={
            "trigger_ids": [str(trigger_id)],
            "reconciliation_reason": "mutating effect outcome unknown",
        },
        result_summary="failed",
    )

    class Session:
        async def execute(self, _query):
            return _ListResult([trigger])

        def add(self, _value):
            raise AssertionError("non-success settlement must not emit trigger_fired audit")

    receipt = await _settle_trigger_runtime_task(Session(), task, status=status)

    assert receipt["trigger_outcomes"] == {str(trigger_id): expected_outcome}
    if status == "failed":
        assert "_fire_inflight" not in trigger.config
        assert trigger.config["failure_count"] == 1
    elif status == "needs_reconciliation":
        assert trigger.config["_fire_inflight"]["hold"] is True
        assert _inflight_fire_is_active(
            trigger.config,
            datetime.now(timezone.utc) + timedelta(days=365),
        )
    else:
        assert "_fire_inflight" not in trigger.config
        assert trigger.fire_count == 0


@pytest.mark.asyncio
async def test_late_completion_cannot_overwrite_killed_runtime_task(monkeypatch):
    from app.services.runtime_task_service import update_runtime_task_record

    tenant_id = uuid4()
    killed_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "task_type": "delegation",
            "status": "killed",
            "metadata_json": {"terminal_reason": "cancelled_by_user"},
            "started_at": killed_at,
            "completed_at": killed_at,
            "trace_id": "trace",
            "result_summary": "cancelled",
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

    updated = await update_runtime_task_record(
        task.id.hex,
        status="completed",
        result_summary="late success",
    )

    assert updated is False
    assert task.status == "killed"
    assert task.result_summary == "cancelled"
    assert task.metadata_json["late_terminal_attempt"]["requested_status"] == "completed"
    assert fake_session.commit_calls == 1


@pytest.mark.asyncio
async def test_resumable_runtime_task_maps_to_supported_root_suspended_state(monkeypatch):
    from app.services.runtime_task_service import update_runtime_task_record

    tenant_id = uuid4()
    task = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "task_type": "subagent",
            "status": "running",
            "metadata_json": {},
            "started_at": None,
            "completed_at": None,
            "trace_id": "trace",
            "result_summary": None,
            "claim_version": 0,
            "root_runtime_task_id": uuid4(),
        },
    )()
    fake_session = _UpdateSession(task)
    _route_runtime_accessors(monkeypatch, fake_session, tenant_id=tenant_id)

    async def fake_resolve_runtime_task_tenant(*_args, **_kwargs):
        return tenant_id

    transitions = []

    async def fake_transition(_db, **kwargs):
        transitions.append(kwargs)
        return None, None

    monkeypatch.setattr(
        "app.services.runtime_task_service.resolve_tenant_for_runtime_task",
        fake_resolve_runtime_task_tenant,
    )
    monkeypatch.setattr(
        "app.services.runtime_root_ledger.transition_runtime_root_item_by_task",
        fake_transition,
    )

    updated = await update_runtime_task_record(task.id.hex, status="resumable")

    assert updated is True
    assert task.status == "resumable"
    assert transitions[0]["requested_state"] == "suspended"


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
async def test_create_runtime_task_record_rejects_malformed_root_runtime_task_id(monkeypatch):
    from app.services.runtime_task_service import create_runtime_task_record

    fake_session = _CreateSession()
    _route_runtime_accessors(monkeypatch, fake_session, tenant_id=uuid4())

    with pytest.raises(ValueError, match="Invalid root runtime task id"):
        await create_runtime_task_record(
            task_id=uuid4().hex,
            task_type="subagent",
            parent_agent_id=uuid4(),
            root_runtime_task_id="not-a-uuid",
            root_item_intent_key="subagent:scout",
        )

    assert fake_session.added == []
    assert fake_session.commit_calls == 0


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
    assert running_task.metadata_json["terminal_committed_status"] == "failed"
    assert running_task.metadata_json["terminal_commit_source"] == (
        "runtime_task_service.startup_orphan_reconciliation"
    )
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
async def test_reconcile_orphaned_runtime_tasks_preserves_worker_reclaimable_delegation_only(monkeypatch):
    from app.services.runtime_task_service import reconcile_orphaned_runtime_tasks

    resumable_delegation_id = uuid4()
    resumable_delegation = type(
        "RuntimeTaskStub",
        (),
        {
            "id": resumable_delegation_id,
            "task_type": "delegation",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {
                "resume_after_restart": True,
                "resumable_delegation": True,
                "restart_replay_contract": {
                    "schema": "runtime_restart_replay_contract.v1",
                    "idempotency_key": f"delegation:{resumable_delegation_id.hex}:restart",
                    "task_type": "delegation",
                    "task_id": resumable_delegation_id.hex,
                },
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
    missing_resume_flag = type(
        "RuntimeTaskStub",
        (),
        {
            "id": uuid4(),
            "task_type": "delegation",
            "status": "running",
            "result_summary": None,
            "completed_at": None,
            "metadata_json": {"resumable_delegation": True},
        },
    )()
    cross_type_flag = type(
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
                "resumable_subagent": True,
            },
        },
    )()
    missing_contract = type(
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
    fake_session = _ReconcileSession(
        [
            resumable_delegation,
            durable_web_chat,
            in_process_delegation,
            missing_resume_flag,
            cross_type_flag,
            missing_contract,
        ]
    )
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks()

    assert updated == 4
    assert resumable_delegation.status == "running"
    assert durable_web_chat.status == "running"
    assert in_process_delegation.status == "needs_reconciliation"
    assert in_process_delegation.metadata_json["needs_reconciliation"] is True
    assert in_process_delegation.metadata_json["side_effect_risk"] == "mutating"
    assert missing_resume_flag.status == "needs_reconciliation"
    assert cross_type_flag.status == "needs_reconciliation"
    assert missing_contract.status == "needs_reconciliation"
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
async def test_reconcile_orphaned_runtime_tasks_preserves_reclaimable_triggers_beyond_startup_limit(monkeypatch):
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
    resumable_triggers = [resumable_trigger]
    for _ in range(50):
        extra_id = uuid4()
        resumable_triggers.append(
            type(
                "RuntimeTaskStub",
                (),
                {
                    "id": extra_id,
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
                            "task_id": extra_id.hex,
                            "idempotency_key": f"trigger:{extra_id.hex}:restart",
                        },
                    },
                },
            )()
        )
    fake_session = _ReconcileSession([*resumable_triggers, resumable_heartbeat])
    monkeypatch.setattr("app.services.runtime_task_service.async_session", lambda: fake_session)

    updated = await reconcile_orphaned_runtime_tasks()

    assert updated == 1
    assert all(task.status == "running" for task in resumable_triggers)
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
            "task_type": "workflow",
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
    terminal_enqueues = []

    async def fake_terminal_settlement(db, received_task, **_kwargs):
        terminal_enqueues.append((db, received_task, fake_session.commit_calls))
        return "runtime-task-terminal:test"

    monkeypatch.setattr(
        "app.services.runtime_terminal_settlement.settle_and_enqueue_runtime_task_terminal",
        fake_terminal_settlement,
    )

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
    assert terminal_enqueues == [(fake_session, task, 0)]
    assert fake_session.commit_calls == 2  # locator audit + tenant-scoped update
