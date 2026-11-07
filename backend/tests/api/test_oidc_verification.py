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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.oidc import router
from app.core.security import get_current_user
from app.database import get_db
from app.models.participant import Participant
from app.models.tenant import Tenant  # noqa: F401 - register the User foreign-key target
from app.models.user import User
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
            "authorization_endpoint": f"{ISSUER}/authorize",
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
        algorithm="RS256",
        jwks={"keys": [signing_keys[0].as_dict(is_private=False)]},
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
            return httpx.Response(200, json=provider.jwks)
        if request.url.path == "/token":
            assert request.method == "POST"
            if provider.token_error:
                raise RuntimeError("sensitive-provider-code-and-secret")
            provider.token = jwt.encode({"alg": provider.algorithm}, provider.claims, provider.key).decode()
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


@pytest.mark.parametrize("endpoint", [
    "javascript:alert(1)",
    "http://remote.example/authorize",
    "http://localhost.evil/authorize",
    r"http://evil.com\@127.0.0.1/authorize",
    r"http://evil.com\@localhost/authorize",
    123,
    ["https://idp.test/authorize"],
])
async def test_discovery_refuses_unsafe_authorization_endpoint(provider, endpoint):
    provider.metadata["authorization_endpoint"] = endpoint
    with pytest.raises(ValueError, match="authorization endpoint"):
        await oidc_service.discover_oidc(ISSUER)
    assert ISSUER not in oidc_service._discovery_cache


@pytest.mark.parametrize("endpoint", ["https://remote.example/authorize", "http://localhost:18232/authorize", "http://127.0.0.1:18232/authorize"])
async def test_discovery_allows_https_and_local_authorization_endpoints(provider, endpoint):
    provider.metadata["authorization_endpoint"] = endpoint
    assert (await oidc_service.discover_oidc(ISSUER))["authorization_endpoint"] == endpoint


async def call_route(monkeypatch, route, nonce=NONCE, *, db=None, user=None, tenant_id=None):
    tenant_id = tenant_id or uuid4()
    if user is None:
        user = _fake_user(tenant_id=tenant_id)
        user.oidc_sub = "verified-subject" if route == "callback" else None
        user.oidc_issuer = ISSUER if route == "callback" else None
    setting = SimpleNamespace(value={
        "issuer_url": ISSUER, "client_id": CLIENT_ID, "client_secret": "fake-client-secret",
    })
    if db is None:
        db = _FakeDB([setting, user])
        db.flush = AsyncMock()
    else:
        # SQLite exercises real uniqueness constraints; PostgreSQL RLS has its own suite.
        monkeypatch.setattr("app.api.oidc.pin_rls_tenant_context", AsyncMock())
        monkeypatch.setattr(oidc_service, "get_tenant_oidc_config", AsyncMock(return_value=setting.value))
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
    async with AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://app.test") as client:
        response = await client.post(f"/api/auth/oidc/{route}", json=body)
    return response, db, user, audit


@pytest.mark.parametrize("route", ["callback", "bind"])
@pytest.mark.parametrize("case", [
    "no_jwks", "tampered_payload", "other_key", "wrong_aud", "wrong_iss", "expired",
    "missing_token_nonce", "wrong_nonce", "missing_request_nonce", "empty_request_nonce",
    "userinfo_subject", "missing_id_token", "missing_exp", "missing_sub",
    "multi_aud_missing_azp", "multi_aud_wrong_azp", "provider_error",
    "empty_sub", "symmetric_key", "discovery_issuer",
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
        provider.claims["azp"] = CLIENT_ID
    elif case == "wrong_iss":
        provider.claims["iss"] = "https://another-idp.test"
    elif case == "expired":
        provider.claims["exp"] = int(time.time()) - 120
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
    elif case == "empty_sub":
        provider.claims["sub"] = ""
        provider.userinfo["sub"] = ""
    elif case == "symmetric_key":
        provider.algorithm = "HS256"
        provider.key = JsonWebKey.generate_key("oct", 256, is_private=True)
        provider.jwks = {"keys": [provider.key.as_dict(is_private=True)]}
    elif case == "discovery_issuer":
        provider.metadata["issuer"] = provider.claims["iss"] = "https://another-idp.test"
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
    if case in {"no_jwks", "discovery_issuer"}:
        assert "/token" not in provider.requests
    if case == "provider_error":
        assert "RuntimeError" in "\n".join(messages)
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
        assert "set-cookie" not in response.headers
        assert db.committed
        audit.assert_awaited_once()
        lookup = db.statements[-1].compile().params
        assert "verified-subject" in lookup.values()
        result = result["user"]
    assert result["oidc_sub"] == user.oidc_sub == "verified-subject"
    assert user.oidc_issuer == ISSUER
    assert set(provider.requests) == {"/.well-known/openid-configuration", "/token", "/jwks", "/userinfo"}
    db.flush.assert_awaited_once()


@pytest.mark.parametrize("route", ["callback", "bind"])
@pytest.mark.parametrize("clock_skew", ["expired_recently", "issued_in_future"])
async def test_accepts_sixty_seconds_clock_skew(monkeypatch, provider, route, clock_skew):
    provider.claims["exp" if clock_skew == "expired_recently" else "iat"] = int(time.time()) + (
        -30 if clock_skew == "expired_recently" else 30
    )
    response, *_ = await call_route(monkeypatch, route)
    assert response.status_code == 200


async def test_malformed_tenant_is_plain_refusal(monkeypatch, provider):
    response, db, _, audit = await call_route(monkeypatch, "callback", tenant_id="not-a-uuid")
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid tenant ID"}
    assert not provider.requests
    db.flush.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.fixture
async def account_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(Participant.__table__.create)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            execute = db.execute

            async def sqlite_execute(statement, *args, **kwargs):
                # SQLite has no GUCs; retain the real bypass scope and SQL queries.
                if str(statement).startswith("SET LOCAL"):
                    return None
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(db, "execute", sqlite_execute)
            yield db
    finally:
        await engine.dispose()


@pytest.mark.parametrize("collision", [
    "email_same_tenant", "email_other_tenant", "sub_other_tenant", "bind_sub",
    "sub_other_issuer", "email_case_same_tenant", "email_case_other_tenant",
])
async def test_account_conflicts_are_plain_refusals(monkeypatch, provider, account_db, collision):
    tenant_id = uuid4()
    victim = User(**vars(_fake_user(
        email=provider.claims["email"],
        tenant_id=uuid4() if collision.endswith("other_tenant") else tenant_id,
    )))
    victim.role = "platform_admin"
    victim.oidc_sub = "verified-subject" if "sub" in collision else "original-subject"
    victim.oidc_issuer = "https://original-idp.test" if collision == "sub_other_issuer" else ISSUER
    actor = User(**vars(_fake_user(username="bob", email="bob@example.com", tenant_id=tenant_id)))
    account_db.add_all([victim, actor])
    await account_db.commit()
    victim_id, actor_id, original_sub, original_issuer = victim.id, actor.id, victim.oidc_sub, victim.oidc_issuer
    if "sub" in collision:
        provider.claims["email"] = "new@example.com"
    elif "email_case" in collision:
        provider.claims["email"] = provider.claims["email"].upper()
    # Even a provider's verified-email claim must not link an existing account.
    provider.claims["email_verified"] = True
    route = "bind" if collision == "bind_sub" else "callback"
    messages = []
    sink = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        response, _, _, audit = await call_route(
            monkeypatch, route, db=account_db, user=actor, tenant_id=tenant_id,
        )
    finally:
        logger.remove(sink)
    assert response.status_code == 409
    assert "OIDC account conflict" in messages
    assert set(response.json()) == {"detail"}
    assert "token" not in response.json()
    audit.assert_not_awaited()
    await account_db.rollback()
    victim = await account_db.get(User, victim_id)
    actor = await account_db.get(User, actor_id)
    assert victim.oidc_sub == original_sub
    assert victim.oidc_issuer == original_issuer
    assert victim.role == "platform_admin"
    assert actor.oidc_sub is None
    assert await account_db.scalar(select(func.count()).select_from(User)) == 2


async def test_admin_save_refuses_discovery_issuer_mismatch(monkeypatch, provider):
    from app.api import enterprise
    from app.core.security import get_current_admin

    provider.metadata["issuer"] = ISSUER + "/"
    db = _FakeDB([])
    admin = _fake_user(tenant_id=uuid4())
    monkeypatch.setattr(enterprise, "resolve_and_pin_tenant_scope", AsyncMock(return_value=admin.tenant_id))
    app = FastAPI()
    app.include_router(enterprise.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin
    async with AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://app.test") as client:
        response = await client.put("/api/enterprise/oidc-config", json={
            "issuer_url": ISSUER, "client_id": CLIENT_ID, "client_secret": "fake-client-secret",
        })
    assert response.status_code == 400
    assert response.json() == {"detail": "OIDC discovery issuer does not match configured issuer"}
    assert not db.statements
    assert not db.committed


async def test_admin_save_refuses_unsafe_authorization_endpoint_with_its_reason(monkeypatch, provider):
    from app.api import enterprise
    from app.core.security import get_current_admin

    provider.metadata["authorization_endpoint"] = "http://remote.example/authorize"
    db = _FakeDB([])
    admin = _fake_user(tenant_id=uuid4())
    monkeypatch.setattr(enterprise, "resolve_and_pin_tenant_scope", AsyncMock(return_value=admin.tenant_id))
    app = FastAPI()
    app.include_router(enterprise.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin
    async with AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://app.test") as client:
        response = await client.put("/api/enterprise/oidc-config", json={
            "issuer_url": ISSUER, "client_id": CLIENT_ID, "client_secret": "fake-client-secret",
        })
    assert response.status_code == 400
    assert response.json() == {"detail": "OIDC authorization endpoint must use HTTPS (HTTP is allowed only on localhost or 127.0.0.1)"}
    assert not db.statements
    assert not db.committed


@pytest.mark.parametrize("route", ["callback", "bind"])
async def test_new_account_or_authenticated_binding_succeeds(monkeypatch, provider, account_db, route):
    tenant_id = uuid4()
    actor = User(**vars(_fake_user(email=provider.claims["email"], tenant_id=tenant_id)))
    if route == "bind":
        account_db.add(actor)
        await account_db.commit()
    response, *_ = await call_route(monkeypatch, route, db=account_db, user=actor, tenant_id=tenant_id)
    assert response.status_code == 200
    result = response.json()["user"] if route == "callback" else response.json()
    assert result["oidc_sub"] == "verified-subject"
