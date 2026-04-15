from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarListResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _DB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_list_users_marks_provider_backed_identity_as_feishu(monkeypatch):
    from app.api import users as users_api

    tenant_id = uuid4()
    current_user = SimpleNamespace(role="platform_admin", tenant_id=tenant_id)
    listed_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        username="tianyi",
        email="ty@example.com",
        display_name="王天怡",
        role="member",
        is_active=True,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
        feishu_open_id=None,
        feishu_user_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db = _DB(
        [
            _ScalarListResult([listed_user]),
            _ScalarResult(2),
        ]
    )

    async def _fake_get_delivery_target(*_args, **_kwargs):
        return ("u_provider_123", "user_id")

    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.get_feishu_delivery_target",
        _fake_get_delivery_target,
    )

    result = await users_api.list_users(
        tenant_id=str(tenant_id),
        current_user=current_user,
        db=db,
    )

    assert len(result) == 1
    assert result[0].source == "feishu"
    assert result[0].feishu_open_id is None


@pytest.mark.asyncio
async def test_list_users_surfaces_provider_backed_open_id(monkeypatch):
    from app.api import users as users_api

    tenant_id = uuid4()
    current_user = SimpleNamespace(role="platform_admin", tenant_id=tenant_id)
    listed_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        username="tianyi",
        email="ty@example.com",
        display_name="王天怡",
        role="member",
        is_active=True,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
        feishu_open_id=None,
        feishu_user_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db = _DB(
        [
            _ScalarListResult([listed_user]),
            _ScalarResult(1),
        ]
    )

    async def _fake_get_delivery_target(*_args, **_kwargs):
        return ("ou_provider_123", "open_id")

    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.get_feishu_delivery_target",
        _fake_get_delivery_target,
    )

    result = await users_api.list_users(
        tenant_id=str(tenant_id),
        current_user=current_user,
        db=db,
    )

    assert len(result) == 1
    assert result[0].source == "feishu"
    assert result[0].feishu_open_id == "ou_provider_123"
