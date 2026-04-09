from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_resolve_feishu_user_prefers_provider_user_id(monkeypatch):
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    expected_user = SimpleNamespace(id=uuid4(), email="a@test.com")
    calls: list[str] = []

    async def by_user_id(db, tenant_id, provider_user_id):
        calls.append(f"user_id:{provider_user_id}")
        return expected_user

    async def by_open_id(db, tenant_id, provider_open_id):
        calls.append(f"open_id:{provider_open_id}")
        return None

    async def by_email(db, tenant_id, email):
        calls.append(f"email:{email}")
        return None

    monkeypatch.setattr(service, "_find_by_provider_user_id", by_user_id)
    monkeypatch.setattr(service, "_find_by_open_id", by_open_id)
    monkeypatch.setattr(service, "_find_by_email", by_email)

    user = await service.resolve_feishu_user(
        db=object(),
        tenant_id=tenant_id,
        provider_user_id="ouser_123",
        provider_open_id="ou_open_456",
        email="fallback@test.com",
    )

    assert user is expected_user
    assert calls == ["user_id:ouser_123"]


@pytest.mark.asyncio
async def test_resolve_feishu_user_falls_back_open_id_then_email(monkeypatch):
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    expected_user = SimpleNamespace(id=uuid4(), email="fallback@test.com")
    calls: list[str] = []

    async def by_user_id(db, tenant_id, provider_user_id):
        calls.append(f"user_id:{provider_user_id}")
        return None

    async def by_open_id(db, tenant_id, provider_open_id):
        calls.append(f"open_id:{provider_open_id}")
        return None

    async def by_email(db, tenant_id, email):
        calls.append(f"email:{email}")
        return expected_user

    monkeypatch.setattr(service, "_find_by_provider_user_id", by_user_id)
    monkeypatch.setattr(service, "_find_by_open_id", by_open_id)
    monkeypatch.setattr(service, "_find_by_email", by_email)

    user = await service.resolve_feishu_user(
        db=object(),
        tenant_id=tenant_id,
        provider_user_id="ouser_123",
        provider_open_id="ou_open_456",
        email="fallback@test.com",
    )

    assert user is expected_user
    assert calls == [
        "user_id:ouser_123",
        "open_id:ou_open_456",
        "email:fallback@test.com",
    ]

