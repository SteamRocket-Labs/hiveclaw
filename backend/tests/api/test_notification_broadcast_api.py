from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.committed = False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_broadcast_notification_sends_to_current_tenant_and_counts_agents():
    import app.api.notification as notification_api

    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    member = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, is_active=True)
    db = _FakeDB([
        _ListResult([current_user, member]),
        _ScalarResult(2),
    ])

    async def fake_send_notification(*, db, user_id, type, title, body="", link=None, ref_id=None):
        db.add(SimpleNamespace(user_id=user_id, type=type, title=title, body=body, link=link, ref_id=ref_id))
        return SimpleNamespace(user_id=user_id)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(notification_api, "send_notification", fake_send_notification)

    try:
        result = await notification_api.broadcast_notifications(
            data=notification_api.BroadcastNotificationIn(title="Maintenance", body="Tonight at 11"),
            current_user=current_user,
            db=db,
        )
    finally:
        monkeypatch.undo()

    assert result == {"users_notified": 2, "agents_notified": 2}
    assert len(db.added) == 2
    assert all(item.type == "broadcast" for item in db.added)
    assert db.committed is True
