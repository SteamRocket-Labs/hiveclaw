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
