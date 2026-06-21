"""Tests for tenant middleware public-path classification and context hygiene."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from jose import jwt
from starlette.responses import Response

from app.config import get_settings
from app.core.tenant_middleware import _is_public_path
from app.database import get_current_tenant_id, set_current_tenant


def test_auth_me_is_not_public_path():
    assert _is_public_path("/api/auth/me") is False
    assert _is_public_path("/api/auth/me/password") is False


def test_login_and_registration_public_paths_stay_public():
    assert _is_public_path("/api/auth/login") is True
    assert _is_public_path("/api/auth/register") is True
    assert _is_public_path("/api/auth/registration-config") is True


def test_feishu_login_public_paths_stay_public_but_bind_requires_auth():
    assert _is_public_path("/api/auth/feishu/sso/available") is True
    assert _is_public_path("/api/auth/feishu/sso/init") is True
    assert _is_public_path("/api/auth/feishu/sso/poll") is True
    assert _is_public_path("/api/auth/feishu/callback") is True
    assert _is_public_path("/api/auth/feishu/authorize") is True
    assert _is_public_path("/api/auth/feishu/callback-desktop") is True
    assert _is_public_path("/api/auth/feishu/bind/init") is False
    assert _is_public_path("/api/auth/feishu/bind") is False


@pytest.mark.asyncio
async def test_tenant_middleware_restores_previous_context_after_authenticated_request():
    """A request-local tenant must not leak into the next async request/task."""
    from app.core.tenant_middleware import TenantMiddleware

    previous_tenant = str(uuid4())
    request_tenant = str(uuid4())
    set_current_tenant(previous_tenant)
    seen_during_request: list[str | None] = []

    settings = get_settings()
    token = jwt.encode(
        {"tid": request_tenant, "role": "member"},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    request = SimpleNamespace(
        url=SimpleNamespace(path="/api/agents"),
        headers={"authorization": f"Bearer {token}"},
        state=SimpleNamespace(),
    )

    async def call_next(_request):
        seen_during_request.append(get_current_tenant_id())
        return Response("ok")

    try:
        await TenantMiddleware(app=object()).dispatch(request, call_next)
        assert seen_during_request == [request_tenant]
        assert get_current_tenant_id() == previous_tenant
    finally:
        set_current_tenant(None)
