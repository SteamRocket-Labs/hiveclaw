from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDB:
    def __init__(self, values):
        self.values = list(values)
        self.flushes = 0

    async def execute(self, _statement):
        return _ScalarResult(self.values.pop(0))

    async def flush(self):
        self.flushes += 1

    def add(self, _value):
        raise AssertionError("inactive user login must not create rows")


@pytest.mark.asyncio
async def test_feishu_auth_rejects_existing_inactive_user_before_identity_write(monkeypatch) -> None:
    from app.services.auth_provider import FeishuAuthProvider

    provider = FeishuAuthProvider()
    inactive_user = SimpleNamespace(id=uuid4(), is_active=False)
    identity_provider = SimpleNamespace(id=uuid4(), config={})
    monkeypatch.setattr(provider, "_ensure_provider", AsyncMock(return_value=identity_provider))
    monkeypatch.setattr(provider, "_exchange_code_for_user", AsyncMock(return_value={"open_id": "ou-1"}))
    monkeypatch.setattr(provider, "_find_user_by_external_identity", AsyncMock(return_value=inactive_user))

    with pytest.raises(ValueError, match="inactive"):
        await provider.authenticate_with_code(_FakeDB([]), code="code", tenant_id=uuid4())


@pytest.mark.asyncio
async def test_oidc_login_rejects_existing_inactive_user_before_issuing_token() -> None:
    from app.services.oidc_service import login_or_register

    tenant_id = uuid4()
    inactive_user = SimpleNamespace(
        id=uuid4(),
        is_active=False,
        tenant_id=tenant_id,
        role="member",
        avatar_url=None,
        oidc_issuer=None,
    )
    db = _FakeDB([inactive_user])

    with pytest.raises(ValueError, match="inactive"):
        await login_or_register(
            db,
            {"sub": "subject-1", "issuer": "https://issuer.example", "email": "user@example.com"},
            tenant_id,
        )

    assert db.flushes == 0
