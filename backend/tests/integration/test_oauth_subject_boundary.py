"""Real OAuth consumers require a verified subject and a current account."""

import json
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.database import get_db, pin_rls_tenant_context
from app.models.identity import ExternalIdentity
from app.models.user import User
from app.services.auth_provider import FeishuAuthProvider
from tests.integration.test_m1c_repair_postgres import company  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
_EXCHANGE = FeishuAuthProvider._exchange_code_for_user


def application(scope):
    from app.api import desktop_auth, feishu

    async def database():
        async with scope.app() as db:
            await pin_rls_tenant_context(db, scope.tenant)
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    app = FastAPI()
    app.include_router(feishu.router)
    app.include_router(desktop_auth.router)
    app.dependency_overrides[get_db] = database
    return app


def provider_transport(monkeypatch, profile, *, on_profile=None):
    calls = []

    async def exchange(_transport, request):
        calls.append(request.url.path)
        if request.url.path == "/open-apis/authen/v2/oauth/token":
            return httpx.Response(200, json={"access_token": "synthetic-provider-token"}, request=request)
        if request.url.path == "/open-apis/authen/v1/user_info":
            if on_profile is not None:
                await on_profile()
            return httpx.Response(200, json={"code": 0, "data": profile}, request=request)
        raise AssertionError("Unexpected HTTP request in the controlled OAuth transport")

    monkeypatch.setattr(FeishuAuthProvider, "_exchange_code_for_user", _EXCHANGE)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", exchange)
    return calls


@pytest.mark.parametrize("mode", ["company", "desktop"])
@pytest.mark.parametrize("profile_kind", ["valid", "missing", "blank"])
async def test_oauth_requires_a_provider_subject_before_creating_an_account(company, monkeypatch, tmp_path, mode, profile_kind):  # noqa: F811
    from app.api.desktop_auth import _store_oauth_state

    tag = uuid.uuid4().hex
    profile = {"name": "Controlled OAuth user", "email": f"subject-{tag}@fixture.test"}
    if profile_kind == "valid":
        profile["open_id"] = f"ou-{tag}"
    elif profile_kind == "blank":
        profile.update(user_id=" ", open_id="\t", union_id="\n")
    calls = provider_transport(monkeypatch, profile)
    async with company.owner() as db:
        before = await db.scalar(select(func.count()).select_from(User))
        identities_before = await db.scalar(select(func.count()).select_from(ExternalIdentity))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(company)), base_url="http://oauth.fixture") as client:
        if mode == "company":
            response = await client.post("/auth/feishu/callback", params={"code": "synthetic-code", "tenant_id": str(company.tenant)})
            token_returned = bool(response.json().get("access_token"))
            accepted = response.status_code == 200
        else:
            nonce = uuid.uuid4().hex
            await _store_oauth_state(nonce, "controlled-oauth-device")
            response = await client.get("/auth/feishu/callback-desktop", params={"code": "synthetic-code", "state": nonce})
            redirect = urlparse(response.headers["location"])
            token_returned = bool(parse_qs(redirect.query).get("token"))
            accepted = redirect.path == "/callback"
    async with company.owner() as db:
        accounts_created = (await db.scalar(select(func.count()).select_from(User))) - before
        identities_created = (await db.scalar(select(func.count()).select_from(ExternalIdentity))) - identities_before
    observed = {"mode": mode, "profile_kind": profile_kind, "accepted": accepted, "token_returned": token_returned,
        "accounts_created": accounts_created, "identities_created": identities_created, "provider_calls": calls}
    (tmp_path / "oauth-subject-observation.json").write_text(json.dumps(observed, indent=2))
    assert len(calls) == 2
    assert accepted is (profile_kind == "valid"), observed
    assert token_returned is (profile_kind == "valid"), observed
    assert accounts_created == identities_created == int(profile_kind == "valid"), observed


@pytest.mark.parametrize("deactivated", ["user", "company"])
async def test_bind_rechecks_account_deactivation_during_the_provider_exchange(company, monkeypatch, tmp_path, deactivated):  # noqa: F811
    from app.core.security import create_access_token

    subject = "ou-bind-" + uuid.uuid4().hex

    async def deactivate():
        async with company.owner() as db:
            from app.models.tenant import Tenant

            row = await db.get(User if deactivated == "user" else Tenant, company.admin if deactivated == "user" else company.tenant)
            row.is_active = False
            await db.commit()

    provider_transport(monkeypatch, {"open_id": subject, "name": "Bound profile"}, on_profile=deactivate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(company)), base_url="http://oauth.fixture") as client:
        response = await client.post("/auth/feishu/bind", params={"code": "synthetic-bind-code"}, headers={
            "Authorization": "Bearer " + create_access_token(str(company.admin), "org_admin", str(company.tenant))})
    async with company.owner() as db:
        user = await db.get(User, company.admin)
        identities = (await db.scalars(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id))).all()
        observed = {"status": response.status_code, "is_active": user.is_active, "identity_count": len(identities),
            "subject_written": user.feishu_open_id is not None}
        (tmp_path / "oauth-bind-observation.json").write_text(json.dumps(observed, indent=2))
        assert response.status_code == 409, observed
        assert response.json()["detail"]["code"] == ("inactive_user" if deactivated == "user" else "provider_disabled")
        assert user.is_active is (deactivated != "user") and user.feishu_open_id is None and not identities
        assert user.password_hash == "unmodified-hash" and user.role == "org_admin" and user.tenant_id == company.tenant


async def test_company_deactivation_during_oauth_cannot_create_a_new_member(company, monkeypatch, tmp_path):  # noqa: F811
    from app.models.tenant import Tenant

    async def deactivate():
        async with company.owner() as db:
            (await db.get(Tenant, company.tenant)).is_active = False
            await db.commit()

    provider_transport(monkeypatch, {"open_id": "ou-new-" + uuid.uuid4().hex, "name": "New member"}, on_profile=deactivate)
    async with company.owner() as db:
        before = await db.scalar(select(func.count()).select_from(User))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(company)), base_url="http://oauth.fixture") as client:
        response = await client.post("/auth/feishu/callback", params={"code": "synthetic-new-code", "tenant_id": str(company.tenant)})
    async with company.owner() as db:
        created = (await db.scalar(select(func.count()).select_from(User))) - before
        (tmp_path / "oauth-company-observation.json").write_text(json.dumps({"status": response.status_code, "new_accounts": created}))
        assert response.status_code == 409 and response.json()["detail"]["code"] == "provider_disabled"
        assert created == 0
