"""G1 round 4 — second-review fixes: the dashboard keeps ordinary work, HR
abandon has one path for members and admins, the kernel refusal keeps the
fail-closed sentinel, and the websocket refusal maps by type.

Each test fails on ``2b8c5a80`` and pins the reviewed contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _Result:
    def __init__(self, value=None, values=()):
        self._value = value
        self._values = list(values)

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value if self._value is not None else 1

    def all(self):
        return self._values

    def __iter__(self):
        return iter(self._values)


class _FakeDB:
    def __init__(self, results=()):
        self._results = list(results)
        self.statements: list[str] = []
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return self._results.pop(0) if self._results else _Result()

    async def scalar(self, stmt):
        self.statements.append(str(stmt))
        return self._results.pop(0).scalar_one_or_none() if self._results else None

    async def scalars(self, stmt):
        self.statements.append(str(stmt))
        return self._results.pop(0) if self._results else _Result()

    async def get(self, _model, _pk, **_kwargs):
        return None

    async def flush(self):
        return None

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, agent):
        committed_agent = self._results.pop(0).scalar_one_or_none()
        agent.deactivated_at = committed_agent.deactivated_at if committed_agent is not None else None


def _employee_row(*, deactivated: bool):
    return SimpleNamespace(
        id=uuid4(), tenant_id=uuid4(), name="被放弃创建的员工",
        deleted_at=None, deactivated_at=datetime.now(timezone.utc) if deactivated else None,
    )


# ─── item 2: the abandon refuses, it never deactivates ──────────────────


@pytest.mark.asyncio
async def test_abandon_of_a_live_created_employee_refuses_without_touching_it(monkeypatch) -> None:
    from app.services import hr_creation_recovery

    draft = SimpleNamespace(
        id=uuid4(), tenant_id=uuid4(), created_agent_id=uuid4(),
        status="failed", claim_token=None, provisioning_task_id=None,
        provisioning_json={},
    )
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=draft.tenant_id, is_active=True)
    employee = _employee_row(deactivated=False)
    db = _FakeDB(results=[_Result(employee)])

    def fake_admin(user, *, resource_tenant_id):
        return user is admin

    monkeypatch.setattr("app.core.permissions.is_scoped_business_admin", fake_admin)

    with pytest.raises(HTTPException) as exc_info:
        await hr_creation_recovery.abandon_hr_creation(db, draft, actor=admin, task=None)

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail["code"] == "agent_retirement_administrator_required", detail
    assert "digital-employees page" in detail["message"], detail
    # Nothing was mutated: no fence, no supersede, no audit.
    assert db.commits == 0 and db.added == [] and db.rollbacks == 0
    assert draft.status == "failed"

    # A member gets the plain ask-the-admin sentence.
    member = SimpleNamespace(id=uuid4(), role="member", tenant_id=draft.tenant_id, is_active=True)
    db2 = _FakeDB(results=[_Result(_employee_row(deactivated=False))])
    with pytest.raises(HTTPException) as member_refusal:
        await hr_creation_recovery.abandon_hr_creation(db2, draft, actor=member, task=None)
    assert "Ask a company admin" in member_refusal.value.detail["message"]


@pytest.mark.asyncio
async def test_abandon_supersedes_once_the_created_employee_is_deactivated(monkeypatch) -> None:
    from app.services import hr_creation_recovery

    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=uuid4(), is_active=True)
    draft = SimpleNamespace(
        id=uuid4(), tenant_id=admin.tenant_id, created_agent_id=uuid4(),
        status="failed", claim_token=None, provisioning_task_id=None,
        provisioning_json={},
    )
    employee = _employee_row(deactivated=True)
    db = _FakeDB(results=[
        _Result(employee),   # the guard's row lock read
        _Result(draft),      # the re-lock of the draft
    ])

    async def fake_audit(_db, **_kwargs):
        return None

    monkeypatch.setattr("app.core.policy.write_audit_event", fake_audit)

    await hr_creation_recovery.abandon_hr_creation(db, draft, actor=admin, task=None)

    assert draft.status == "superseded" and draft.failure_code == "abandoned_by_requester"
    assert "deactivates it" in draft.failure_message, draft.failure_message
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_deactivated_prepare_outcome_still_commits_bookkeeping_then_settles(monkeypatch) -> None:
    """Every committed-deactivation outcome of prepare gets projection, audit
    and sync bump before the typed pending error surfaces."""
    import app.api.agents as agents_api
    from app.services import agent_retirement

    tenant_id = uuid4()
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    agent = SimpleNamespace(
        id=uuid4(), tenant_id=tenant_id, name="待停用", status="idle",
        deleted_at=None, deactivated_at=None, deactivation_reason=None,
    )
    order: list[str] = []

    async def pending_prepare(_db, *, agent, actor):
        agent.deactivated_at = datetime.now(timezone.utc)
        agent.deactivation_reason = agent_retirement.PENDING_REASON
        agent.status = "stopped"
        order.append("prepare-committed")
        raise HTTPException(409, detail={"code": "agent_retirement_pending", "retryable": True})

    async def fake_register(_db, _projection, **_kwargs):
        order.append("projection")

    async def fake_audit(_db, **_kwargs):
        order.append("audit")

    async def fake_settle(_db, _agent_in, **_kwargs):
        order.append("terminate")

    async def fake_history_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr("app.core.permissions.check_agent_history_access", fake_history_access)
    monkeypatch.setattr(agent_retirement, "prepare", pending_prepare)
    monkeypatch.setattr("app.services.ai_assets.register_projection", fake_register)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_audit)
    monkeypatch.setattr("app.services.agent_identity_lifecycle.terminate_agent_runtime_tasks", fake_settle)
    db = _FakeDB(results=[_Result(agent), _Result(True), _Result(agent)])
    # queue: pending re-read of deactivated_at, sync bump, terminate re-select

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.deactivate_agent(agent_id=agent.id, current_user=admin, db=db)

    assert exc_info.value.detail["code"] == "agent_retirement_pending"
    assert order == ["prepare-committed", "projection", "audit", "terminate"], order
    assert db.commits >= 2, "bookkeeping and settlement commit separately"


@pytest.mark.asyncio
async def test_uncommitted_prepare_error_never_reaches_bookkeeping(monkeypatch) -> None:
    """A prepare error whose deactivation did not commit re-raises before any
    bookkeeping or settlement happens."""
    import app.api.agents as agents_api
    from app.services import agent_retirement

    tenant_id = uuid4()
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    agent = SimpleNamespace(
        id=uuid4(), tenant_id=tenant_id, name="未被停用", status="idle",
        deleted_at=None, deactivated_at=None, deactivation_reason=None,
    )
    async def failing_prepare(_db, *, agent, actor):
        raise HTTPException(403, detail={"code": "agent_retirement_administrator_required"})

    async def fail_if_called(_db, *_args, **_kwargs):
        raise AssertionError("no bookkeeping may run for an uncommitted outcome")

    async def fake_history_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr("app.core.permissions.check_agent_history_access", fake_history_access)
    monkeypatch.setattr(agent_retirement, "prepare", failing_prepare)
    monkeypatch.setattr("app.services.ai_assets.register_projection", fail_if_called)
    monkeypatch.setattr("app.services.agent_identity_lifecycle.terminate_agent_runtime_tasks", fail_if_called)
    db = _FakeDB(results=[_Result(None)])

    with pytest.raises(HTTPException) as exc_info:
        await agents_api.deactivate_agent(agent_id=agent.id, current_user=admin, db=db)

    assert exc_info.value.status_code == 403
    assert db.commits == 0
