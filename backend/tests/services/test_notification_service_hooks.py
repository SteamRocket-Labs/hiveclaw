from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeDB:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self.added = []
        self.flushes = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.tenant_id)


@pytest.mark.asyncio
async def test_notification_service_emits_notification_hook_with_tenant_evidence(monkeypatch):
    from app.services import notification_service

    tenant_id = uuid4()
    user_id = uuid4()
    captured = []

    async def fake_emit(event, **kwargs):
        captured.append((event.value, kwargs))
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit)
    db = _FakeDB(tenant_id)
    notification = await notification_service.send_notification(
        db=db,
        user_id=user_id,
        type="approval_pending",
        title="Approval required",
        body="Review the action",
        link="/approvals/1",
    )

    assert notification.title == "Approval required"
    assert captured == [
        (
            "notification",
            {
                "evidence_db": db,
                "source": "notification_service",
                "metadata": {
                    "tenant_id": str(tenant_id),
                    "user_id": str(user_id),
                    "notification_id": str(notification.id),
                    "notification_type": "approval_pending",
                    "title": "Approval required",
                    "link": "/approvals/1",
                },
            },
        )
    ]
