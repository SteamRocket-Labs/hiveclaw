"""Exercise both OIDC entry points against an in-process signing provider."""

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt
from fastapi import FastAPI
from loguru import logger

from app.api.oidc import router
from app.core.security import get_current_user
from app.database import get_db
from app.services import oidc_service
from tests.api.test_auth_login import _fake_user
from tests.api.test_oidc_rls import _FakeDB

ISSUER = "https://idp.test"
CLIENT_ID = "sso-client"
NONCE = "browser-login-nonce"
AsyncClient = httpx.AsyncClient


@pytest.fixture(scope="module")
def signing_keys():
    return [JsonWebKey.generate_key("RSA", 2048, is_private=True) for _ in range(2)]


@pytest.fixture
def provider(monkeypatch, signing_keys):
    now = int(time.time())
    provider = SimpleNamespace(
        metadata={
            "issuer": ISSUER,
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
            "userinfo_endpoint": f"{ISSUER}/userinfo",
        },
        claims={
            "iss": ISSUER, "aud": CLIENT_ID, "sub": "verified-subject",
            "iat": now, "exp": now + 300, "nonce": NONCE,
            "email": "token@example.com", "name": "Token Name",
        },
        userinfo={"sub": "verified-subject", "name": "Profile Name", "picture": "https://idp.test/avatar"},
        key=signing_keys[0],
        tamper=False,
        missing_token=False,
        token_error=False,
        token="",
        requests=[],
    )

    def handle(request):
        provider.requests.append(request.url.path)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=provider.metadata)
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [signing_keys[0].as_dict(is_private=False)]})
        if request.url.path == "/token":
            assert request.method == "POST"
            if provider.token_error:
                raise RuntimeError("sensitive-provider-code-and-secret")
            provider.token = jwt.encode({"alg": "RS256"}, provider.claims, provider.key).decode()
            if provider.tamper:
                parts = provider.token.split(".")
                parts[1] = base64.urlsafe_b64encode(
                    json.dumps({**provider.claims, "sub": "forged-subject"}).encode()
                ).decode().rstrip("=")
                provider.token = ".".join(parts)
            data = {"access_token": "fake-provider-access-token"}
            if not provider.missing_token:
                data["id_token"] = provider.token
            return httpx.Response(200, json=data)
        if request.url.path == "/userinfo":
            return httpx.Response(200, json=provider.userinfo)
        raise AssertionError("Unexpected provider endpoint")

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        oidc_service.httpx, "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    oidc_service._discovery_cache.clear()
    yield provider
    oidc_service._discovery_cache.clear()


async def call_route(monkeypatch, route, nonce=NONCE):
    tenant_id = uuid4()
    user = _fake_user(tenant_id=tenant_id)
    user.oidc_sub = "verified-subject" if route == "callback" else None
    user.oidc_issuer = None
    setting = SimpleNamespace(value={
        "issuer_url": ISSUER, "client_id": CLIENT_ID, "client_secret": "fake-client-secret",
    })
    db = _FakeDB([setting, user])
    db.flush = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr("app.core.policy.write_audit_event", audit)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    body = {"code": "fake-authorization-code", "redirect_uri": "https://app.test/sso", "tenant_id": str(tenant_id)}
    if nonce is not None:
        body["nonce"] = nonce
    # Use the base client class so the provider mock cannot intercept the app request.
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as client:
        response = await client.post(f"/api/auth/oidc/{route}", json=body)
    return response, db, user, audit


@pytest.mark.parametrize("route", ["callback", "bind"])
@pytest.mark.parametrize("case", [
    "no_jwks", "tampered_payload", "other_key", "wrong_aud", "wrong_iss", "expired",
    "missing_token_nonce", "wrong_nonce", "missing_request_nonce", "empty_request_nonce",
    "userinfo_subject", "missing_id_token", "missing_exp", "missing_sub",
    "multi_aud_missing_azp", "multi_aud_wrong_azp", "provider_error",
])
async def test_rejects_invalid_login(monkeypatch, provider, signing_keys, route, case):
    nonce = NONCE
    if case == "no_jwks":
        del provider.metadata["jwks_uri"]
    elif case == "tampered_payload":
        provider.tamper = True
    elif case == "other_key":
        provider.key = signing_keys[1]
    elif case == "wrong_aud":
        provider.claims["aud"] = "another-client"
    elif case == "wrong_iss":
        provider.claims["iss"] = "https://another-idp.test"
    elif case == "expired":
        provider.claims["exp"] = int(time.time()) - 60
    elif case == "missing_token_nonce":
        del provider.claims["nonce"]
    elif case == "wrong_nonce":
        provider.claims["nonce"] = "other-login"
    elif case == "missing_request_nonce":
        nonce = None
    elif case == "empty_request_nonce":
        nonce = ""
    elif case == "userinfo_subject":
        provider.userinfo["sub"] = "different-subject"
    elif case == "missing_id_token":
        provider.missing_token = True
    elif case == "missing_exp":
        del provider.claims["exp"]
    elif case == "missing_sub":
        del provider.claims["sub"]
    elif case.startswith("multi_aud"):
        provider.claims["aud"] = [CLIENT_ID, "other-client"]
        if case == "multi_aud_wrong_azp":
            provider.claims["azp"] = "other-client"
    elif case == "provider_error":
        provider.token_error = True

    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        response, db, user, audit = await call_route(monkeypatch, route, nonce)
    finally:
        logger.remove(sink)
    assert response.status_code == 400
    assert response.json() == {"detail": "OIDC authentication failed"}
    db.flush.assert_not_awaited()
    audit.assert_not_awaited()
    assert not db.committed
    assert user.oidc_sub == ("verified-subject" if route == "callback" else None)
    assert messages
    log = "\n".join(messages)
    assert all(sensitive not in log for sensitive in [
        provider.token, "fake-authorization-code", "fake-client-secret",
        "fake-provider-access-token", "sensitive-provider-code-and-secret",
    ] if sensitive), "Authentication logs must not contain sensitive values"


@pytest.mark.parametrize("route", ["callback", "bind"])
@pytest.mark.parametrize("audience", ["single", "list", "multiple"])
async def test_valid_token_signs_in_with_verified_subject(monkeypatch, provider, route, audience):
    if audience != "single":
        provider.claims["aud"] = [CLIENT_ID]
    if audience == "multiple":
        provider.claims["aud"].append("other-client")
        provider.claims["azp"] = CLIENT_ID
    response, db, user, audit = await call_route(monkeypatch, route)
    assert response.status_code == 200
    result = response.json()
    if route == "callback":
        assert result["access_token"]
        assert db.committed
        audit.assert_awaited_once()
        lookup = db.statements[-1].compile().params
        assert "verified-subject" in lookup.values()
        result = result["user"]
    assert result["oidc_sub"] == user.oidc_sub == "verified-subject"
    assert user.oidc_issuer == ISSUER
    assert set(provider.requests) == {"/.well-known/openid-configuration", "/token", "/jwks", "/userinfo"}
    db.flush.assert_awaited_once()
