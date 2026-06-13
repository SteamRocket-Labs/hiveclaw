from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.audit import ChatMessage


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


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    def add(self, item):
        self.added.append(item)

    async def execute(self, _query):
        return _FakeResult(self.rows)


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

    async def execute(self, _query):
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
async def test_resolve_approval_commits_before_approved_external_action(monkeypatch) -> None:
    from app.services.approval_service import ApprovalService

    events: list[str] = []
    tenant_id = uuid4()
    approval = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        action_type="write_file",
        status="pending",
        created_at=None,
        resolved_at=None,
        resolved_by=None,
        details={"tool": "write_file", "args": {"path": "focus.md"}},
    )
    agent = SimpleNamespace(id=approval.agent_id, tenant_id=tenant_id, creator_id=uuid4(), name="Ops Agent")
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")
    db = _ResolveApprovalDb(approval, agent, events)

    async def fake_write_audit_event(*_args, **_kwargs):
        events.append("audit")

    async def fake_send_notification(*_args, **_kwargs):
        events.append("notify")

    class _Service(ApprovalService):
        async def _execute_approved_action(self, *args, **kwargs):
            events.append("execute")
            assert "commit" in db.events
            return "ok"

        async def _publish_approval_result_to_origin(self, *args, **kwargs):
            events.append("publish")
            return {"type": "approval_tool_result"}

    monkeypatch.setattr("app.core.policy.write_audit_event", fake_write_audit_event)
    monkeypatch.setattr("app.services.notification_service.send_notification", fake_send_notification)

    resolved = await _Service().resolve_approval(db, approval.id, user, "approve")  # type: ignore[arg-type]

    assert resolved.status == "approved"
    assert events.index("commit") < events.index("execute")


@pytest.mark.asyncio
async def test_approval_result_is_published_to_origin_session_and_active_run() -> None:
    from app.services.approval_service import ApprovalService

    agent_id = uuid4()
    tenant_id = uuid4()
    approver_id = uuid4()
    approval_id = uuid4()
    active_run = SimpleNamespace(
        id=uuid4(),
        task_type="web_chat_turn",
        status="running",
        metadata_json={"session_id": "session-approval", "pending_user_messages": []},
    )
    db = _FakeDb([active_run])
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid4())
    approval = SimpleNamespace(
        id=approval_id,
        agent_id=agent_id,
        action_type="workspace.write",
        status="approved",
        details={
            "tool": "write_file",
            "args": {"path": "focus.md"},
            "session_id": "session-approval",
        },
    )

    payload = await ApprovalService()._publish_approval_result_to_origin(
        db,
        approval,
        agent=agent,
        approved_by_user_id=approver_id,
        execution_result="wrote focus.md",
    )

    assert payload["type"] == "approval_tool_result"
    chat_rows = [item for item in db.added if isinstance(item, ChatMessage)]
    assert len(chat_rows) == 1
    assert chat_rows[0].role == "tool_call"
    assert chat_rows[0].conversation_id == "session-approval"
    stored = json.loads(chat_rows[0].content)
    assert stored["approval_id"] == str(approval_id)
    assert stored["result"] == "wrote focus.md"
    pending = active_run.metadata_json["pending_user_messages"]
    assert len(pending) == 1
    assert pending[0]["approval_id"] == str(approval_id)
    assert "wrote focus.md" in pending[0]["content"]


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
