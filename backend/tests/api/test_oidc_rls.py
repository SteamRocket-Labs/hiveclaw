from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.sync_session = SimpleNamespace(info={})
        self.statements = []
        self.committed = False
        self.rollback_called = False

    async def execute(self, stmt):
        self.statements.append(stmt)
        sql = getattr(stmt, "text", None) or str(stmt)
        if sql.lstrip().upper().startswith("SET LOCAL"):
            return _ScalarResult(None)
        if not self._results:
            raise AssertionError("Unexpected execute call")
        return _ScalarResult(self._results.pop(0))

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rollback_called = True


@pytest.mark.asyncio
async def test_oidc_public_config_pins_resolved_tenant_before_setting_read():
    from app import database
    from app.api import oidc as oidc_api

    tenant_id = uuid4()
    tenant = SimpleNamespace(id=tenant_id, slug="acme")
    setting = SimpleNamespace(
        value={
            "issuer_url": "https://issuer.example.com",
            "client_id": "client-id",
            "scopes": "openid email",
            "display_name": "Acme SSO",
        }
    )
    db = _FakeDB([tenant, setting])

    with patch("app.services.oidc_service.discover_oidc", new_callable=AsyncMock) as discover:
        discover.return_value = {"authorization_endpoint": "https://issuer.example.com/auth"}
        result = await oidc_api.get_oidc_config(tenant_slug="acme", db=db)

    assert result["configured"] is True
    assert result["tenant_id"] == str(tenant_id)
    assert db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)
    assert any(f"SET LOCAL app.current_tenant_id = '{tenant_id}'" in str(stmt) for stmt in db.statements)


@pytest.mark.asyncio
async def test_oidc_callback_pins_explicit_tenant_before_login_or_register():
    from app import database
    from app.api import oidc as oidc_api

    tenant_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        username="oidc-user",
        email="oidc@example.com",
        display_name="OIDC User",
        avatar_url=None,
        role="member",
        tenant_id=tenant_id,
        department_id=None,
        title=None,
        feishu_open_id=None,
        oidc_sub="sub-1",
        is_active=True,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
        created_at=datetime.now(timezone.utc),
    )
    setting = SimpleNamespace(
        value={
            "issuer_url": "https://issuer.example.com",
            "client_id": "client-id",
            "client_secret": "secret",
            "auto_provision": True,
        }
    )
    db = _FakeDB([setting])

    with (
        patch("app.services.oidc_service.exchange_code", new_callable=AsyncMock) as exchange_code,
        patch("app.services.oidc_service.login_or_register", new_callable=AsyncMock) as login_or_register,
        patch("app.core.policy.write_audit_event", new_callable=AsyncMock),
    ):
        exchange_code.return_value = {
            "sub": "sub-1",
            "email": "oidc@example.com",
            "issuer": "https://issuer.example.com",
        }
        login_or_register.return_value = (user, "jwt-token")
        result = await oidc_api.oidc_callback(
            oidc_api.OIDCCallbackRequest(
                code="code",
                redirect_uri="https://app.example.com/callback",
                tenant_id=str(tenant_id),
            ),
            db=db,
        )

    assert result.access_token == "jwt-token"
    assert db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)
    assert any(f"SET LOCAL app.current_tenant_id = '{tenant_id}'" in str(stmt) for stmt in db.statements)
    assert db.committed is True


@pytest.mark.asyncio
async def test_oidc_callback_fails_closed_when_security_audit_is_unavailable():
    from app.api import oidc as oidc_api

    tenant_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        username="tenantless-oidc-user",
        email="oidc@example.com",
        display_name="OIDC User",
        avatar_url=None,
        role="member",
        tenant_id=None,
        department_id=None,
        title=None,
        feishu_open_id=None,
        oidc_sub="sub-1",
        is_active=True,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
        created_at=datetime.now(timezone.utc),
    )
    setting = SimpleNamespace(
        value={
            "issuer_url": "https://issuer.example.com",
            "client_id": "client-id",
            "client_secret": "secret",
            "auto_provision": True,
        }
    )
    db = _FakeDB([setting])

    with (
        patch("app.services.oidc_service.exchange_code", new_callable=AsyncMock) as exchange_code,
        patch("app.services.oidc_service.login_or_register", new_callable=AsyncMock) as login_or_register,
        patch(
            "app.core.policy.write_audit_event",
            new=AsyncMock(side_effect=RuntimeError("operator audit insert failed")),
        ),
    ):
        exchange_code.return_value = {
            "sub": "sub-1",
            "email": "oidc@example.com",
            "issuer": "https://issuer.example.com",
        }
        login_or_register.return_value = (user, "jwt-token")
        with pytest.raises(HTTPException) as exc_info:
            await oidc_api.oidc_callback(
                oidc_api.OIDCCallbackRequest(
                    code="code",
                    redirect_uri="https://app.example.com/callback",
                    tenant_id=str(tenant_id),
                ),
                db=db,
            )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Security audit unavailable; authentication was not completed"
    assert db.rollback_called is True
    assert db.committed is False
