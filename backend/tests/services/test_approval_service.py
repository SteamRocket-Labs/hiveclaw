from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalarRows(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    def add(self, item):
        self.added.append(item)

    async def execute(self, _query):
        return _FakeResult(self.rows)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_request_approval_persists_immutable_execution_envelope() -> None:
    from app.models.audit import ApprovalRequest
    from app.services.approval_service import ApprovalService
    from app.services.approval_ticket import (
        build_approval_execution_envelope,
        hash_approval_execution_envelope,
    )
    from app.tools.runtime import ToolExecutionContext

    tenant_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, name="Approval Agent")
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requester_id,
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/approval-service-workspace"),
        session_id="channel-session:approval-service",
        origin_channel="web",
    )
    envelope = build_approval_execution_envelope(
        context=context,
        tool_call_id="approval-service-tool-call",
        emit_runtime_hooks=True,
    )
    db = _FakeDb([])

    class _Service(ApprovalService):
        async def _notify_pending_approval(self, *_args, **_kwargs):
            return None

    outcome = await _Service().request_approval(
        db,  # type: ignore[arg-type]
        agent,  # type: ignore[arg-type]
        action_type="workspace.file.write",
        details={
            "tool": "write_file",
            "args": {"path": "workspace/notes.md", "content": "approved"},
            "requested_by": str(requester_id),
            "execution_envelope": envelope,
            "policy_snapshot": {"schema": "test-policy"},
        },
    )

    approval = next(item for item in db.added if isinstance(item, ApprovalRequest))
    assert outcome["approval_id"] == str(approval.id)
    assert approval.execution_envelope == envelope
    assert approval.execution_envelope_hash == hash_approval_execution_envelope(envelope)
    assert approval.requested_by == requester_id


@pytest.mark.asyncio
async def test_bound_external_approval_persists_principal_in_ticket_and_audit() -> None:
    from app.core.execution_context import ExecutionIdentity
    from app.models.audit import ApprovalRequest, AuditLog
    from app.services.approval_service import ApprovalService
    from app.services.approval_ticket import build_approval_execution_envelope
    from app.tools.runtime import ToolExecutionContext

    tenant_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    external_principal_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, name="External Approval Agent")
    principal = SimpleNamespace(
        id=external_principal_id,
        tenant_id=tenant_id,
        linked_user_id=requester_id,
        status="active",
    )
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requester_id,
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/external-approval-workspace"),
        execution_identity=ExecutionIdentity(
            identity_type="external_principal_bound",
            identity_id=external_principal_id,
            label="Slack guest via slack",
        ),
        session_id="slack:approval",
        origin_channel="slack",
    )
    envelope = build_approval_execution_envelope(
        context=context,
        tool_call_id="external-approval-call",
        emit_runtime_hooks=True,
    )
    db = _FakeDb([principal])

    class _Service(ApprovalService):
        async def _notify_pending_approval(self, *_args, **_kwargs):
            return None

    await _Service().request_approval(
        db,  # type: ignore[arg-type]
        agent,  # type: ignore[arg-type]
        action_type="workspace.file.write",
        details={
            "tool": "write_file",
            "args": {"path": "workspace/external.md", "content": "approved"},
            "requested_by": str(requester_id),
            "execution_envelope": envelope,
            "policy_snapshot": {"schema": "test-policy"},
        },
    )

    approval = next(item for item in db.added if isinstance(item, ApprovalRequest))
    audit = next(item for item in db.added if isinstance(item, AuditLog))
    assert approval.requested_by_external_principal_id == external_principal_id
    assert audit.external_principal_id == external_principal_id


@pytest.mark.asyncio
async def test_bound_external_approval_rejects_stale_or_cross_tenant_binding() -> None:
    from app.core.execution_context import ExecutionIdentity
    from app.services.approval_service import ApprovalService
    from app.services.approval_ticket import ApprovalTicketError, build_approval_execution_envelope
    from app.tools.runtime import ToolExecutionContext

    tenant_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    external_principal_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=requester_id,
        tenant_id=str(tenant_id),
        workspace=Path("/tmp/external-approval-stale"),
        execution_identity=ExecutionIdentity(
            identity_type="external_principal_bound",
            identity_id=external_principal_id,
            label="stale binding",
        ),
    )
    envelope = build_approval_execution_envelope(
        context=context,
        tool_call_id="stale-external-approval",
        emit_runtime_hooks=True,
    )
    # The tenant + linked-user predicate returns no row for a stale/cross-tenant binding.
    db = _FakeDb([])

    class _Service(ApprovalService):
        async def _notify_pending_approval(self, *_args, **_kwargs):
            return None

    with pytest.raises(ApprovalTicketError, match="external principal binding mismatch"):
        await _Service().request_approval(
            db,  # type: ignore[arg-type]
            SimpleNamespace(id=agent_id, tenant_id=tenant_id, name="Stale Agent"),  # type: ignore[arg-type]
            action_type="workspace.file.write",
            details={
                "tool": "write_file",
                "args": {"path": "workspace/stale.md", "content": "blocked"},
                "requested_by": str(requester_id),
                "execution_envelope": envelope,
                "policy_snapshot": {"schema": "test-policy"},
            },
        )


class _FakeOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ResolveApprovalDb:
    def __init__(self, approval, agent, events):
        self._values = [approval, agent]
        self.events = []
        self._events = events
        self.added = []
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        return _FakeOneResult(self._values.pop(0))

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.events.append("flush")
        self._events.append("flush")

    async def commit(self):
        self.events.append("commit")
        self._events.append("commit")


@pytest.mark.asyncio
async def test_session_tool_approval_cannot_be_resolved_through_enterprise_service() -> None:
    from app.services.approval_service import ApprovalService

    tenant_id = uuid4()
    approval = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        tenant_id=tenant_id,
        action_type="external.web.search",
        status="pending",
        created_at=None,
        resolved_at=None,
        resolved_by=None,
        details={
            "tool": "web_search",
            "args": {"query": "github trending"},
            "origin": {"type": "agent_session", "session_id": "session-1"},
        },
    )
    agent = SimpleNamespace(id=approval.agent_id, tenant_id=tenant_id, creator_id=uuid4(), name="Ops Agent")
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")
    db = _ResolveApprovalDb(approval, agent, [])

    with pytest.raises(ValueError, match="inside the session"):
        await ApprovalService().resolve_approval(db, approval.id, user, "approve")  # type: ignore[arg-type]

    assert approval.status == "pending"
    assert approval.resolved_at is None


@pytest.mark.asyncio
async def test_resolve_approval_atomically_enqueues_durable_execution_job(monkeypatch) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.services.approval_service import ApprovalService

    events: list[str] = []
    tenant_id = uuid4()
    approval = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        tenant_id=tenant_id,
        action_type="write_file",
        tool_name="write_file",
        execution_status="pending",
        execution_task_id=None,
        execution_receipt=None,
        execution_envelope_hash="envelope-hash",
        policy_snapshot_hash="policy-hash",
        requested_by=uuid4(),
        expires_at=None,
        status="pending",
        created_at=None,
        resolved_at=None,
        resolved_by=None,
        details={"tool": "write_file", "args": {"path": "workspace/notes.md"}},
    )
    agent = SimpleNamespace(id=approval.agent_id, tenant_id=tenant_id, creator_id=uuid4(), name="Ops Agent")
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")
    db = _ResolveApprovalDb(approval, agent, events)

    async def fake_write_audit_event(*_args, **_kwargs):
        events.append("audit")

    async def fake_send_notification(*_args, **_kwargs):
        events.append("notify")

    async def fake_notify_worker(*, reason, runtime_task_id):
        assert reason == "approval_execution_queued"
        assert runtime_task_id == approval.execution_task_id
        events.append("wake")

    async def assign_writer_generation(_db, task):
        task.writer_generation = 1
        return 1

    class _Service(ApprovalService):
        async def _execute_approved_action(self, *args, **kwargs):
            raise AssertionError("approved actions must not execute in the resolving HTTP request")

    monkeypatch.setattr("app.core.policy.write_audit_event", fake_write_audit_event)
    monkeypatch.setattr("app.services.notification_service.send_notification", fake_send_notification)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_notify_worker)
    monkeypatch.setattr(
        "app.services.session_writer_epoch.assign_runtime_task_writer_generation",
        assign_writer_generation,
    )

    resolved = await _Service().resolve_approval(db, approval.id, user, "approve")  # type: ignore[arg-type]

    assert resolved.status == "approved"
    assert resolved.execution_status == "queued"
    execution_jobs = [item for item in db.added if isinstance(item, RuntimeTask)]
    assert len(execution_jobs) == 1
    execution_job = execution_jobs[0]
    assert execution_job.task_type == "approval_execution"
    assert execution_job.status == "pending"
    assert execution_job.root_idempotency_key == f"approval-execution:{approval.id}"
    assert execution_job.metadata_json["approval_id"] == str(approval.id)
    assert approval.execution_task_id == execution_job.id
    assert events.index("commit") < events.index("wake")
    assert "execute" not in events
    assert "FOR UPDATE" in str(db.queries[0]).upper()


def test_org_admin_can_resolve_same_tenant_agent_approval() -> None:
    from app.services.approval_service import _can_resolve_agent_approval

    tenant_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")

    assert _can_resolve_agent_approval(agent, user) is True


def test_org_admin_cannot_resolve_other_tenant_agent_approval() -> None:
    from app.services.approval_service import _can_resolve_agent_approval

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), creator_id=uuid4())
    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), role="org_admin")

    assert _can_resolve_agent_approval(agent, user) is False


def test_only_current_owner_not_creator_or_sponsor_can_resolve_approval() -> None:
    from app.services.approval_service import _can_resolve_agent_approval

    tenant_id = uuid4()
    owner_id = uuid4()
    sponsor_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid4(),
        owner_user_id=owner_id,
        sponsor_user_id=sponsor_id,
    )

    assert _can_resolve_agent_approval(
        agent,
        SimpleNamespace(id=owner_id, tenant_id=tenant_id, role="member"),
    )
    assert not _can_resolve_agent_approval(
        agent,
        SimpleNamespace(id=sponsor_id, tenant_id=tenant_id, role="member"),
    )
    assert not _can_resolve_agent_approval(
        agent,
        SimpleNamespace(id=agent.creator_id, tenant_id=tenant_id, role="member"),
    )
