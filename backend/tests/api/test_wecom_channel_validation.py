from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Minimal DB double returning a fixed scalar, plus an exact Tenant
    liveness scalar for the production ``select(Tenant.is_active)`` probe."""

    def __init__(self, value=None):
        self._value = value
        self.sync_session = SimpleNamespace(info={})
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        if "tenants.is_active" in str(stmt):
            # Retired-company negatives are proven by the real-PostgreSQL
            # inactive-tenant gates; the fake always reports a live company.
            return _ScalarResult(True)
        return _ScalarResult(self._value)


@pytest.mark.asyncio
async def test_wecom_webhook_mode_requires_wecom_agent_id(monkeypatch):
    import app.api.wecom as wecom_api

    async def fake_require_manage(db, current_user, agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=current_user.tenant_id)

    monkeypatch.setattr(wecom_api, "require_agent_manage_access", fake_require_manage)

    with pytest.raises(HTTPException) as exc:
        await wecom_api.configure_wecom_channel(
            agent_id=uuid4(),
            data={
                "corp_id": "corp-id",
                "secret": "secret",
                "token": "token",
                "encoding_aes_key": "encoding-key",
            },
            current_user=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
            db=_FakeDB(),
        )

    assert exc.value.status_code == 422
    assert "wecom_agent_id" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_wecom_verify_webhook_pins_agent_tenant_before_signature_check():
    from app import database
    from app.api.wecom import wecom_verify_webhook

    tenant_id = uuid4()
    config = SimpleNamespace(
        agent_id=uuid4(),
        tenant_id=tenant_id,
        channel_type="wecom",
        verification_token="token",
        encrypt_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
    )
    db = _FakeDB(config)

    response = await wecom_verify_webhook(
        agent_id=config.agent_id,
        msg_signature="wrong-signature",
        timestamp="1",
        nonce="n",
        echostr="encrypted",
        db=db,
    )

    assert response.status_code == 403
    assert db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)
