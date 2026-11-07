"""Public registration and cold-install authority are operator controlled."""

from types import SimpleNamespace

import pytest

from app.api import auth
from tests.api.test_auth_register import _FakeDB, _make_client, _PAYLOAD


@pytest.fixture
def policy(monkeypatch):
    settings = SimpleNamespace(PUBLIC_REGISTRATION_ENABLED=True, INSTALL_TOKEN="local-install-token")
    monkeypatch.setattr(auth, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr(auth, "hash_password", lambda _: "hash")
    monkeypatch.setattr(auth, "create_access_token", lambda *a, **kw: "test-token")

    async def no_seed():
        pass

    monkeypatch.setattr("app.services.agent_seeder.seed_default_agents", no_seed)
    return settings


def test_registration_switch_is_read_for_each_request(policy):
    db = _FakeDB([None, None, 1])
    client = _make_client(db)
    policy.PUBLIC_REGISTRATION_ENABLED = False
    denied = client.post("/api/auth/register", json=_PAYLOAD)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Public registration is closed. Sign in with an existing account or SSO."
    assert db.added == []
    assert db.executed_statements == []
    policy.PUBLIC_REGISTRATION_ENABLED = True
    allowed = client.post("/api/auth/register", json=_PAYLOAD)
    assert allowed.status_code == 201
    assert allowed.json()["user"]["role"] == "member"


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("enabled", [True, False])
def test_registration_config_reports_policy_without_exposing_token(policy, count, enabled):
    policy.PUBLIC_REGISTRATION_ENABLED = enabled
    client = _make_client(_FakeDB([None, count]))
    response = client.get("/api/auth/registration-config")
    assert response.status_code == 200
    assert response.json() == {
        "invitation_code_required": False,
        "registration_enabled": enabled,
        "install_token_required": count == 0,
    }
    assert policy.INSTALL_TOKEN not in response.text


@pytest.mark.parametrize("supplied", [None, "wrong-token", "口令不匹配"])
def test_first_admin_requires_matching_install_token(policy, supplied):
    db = _FakeDB([None, None, 0, 0, None])
    body = {**_PAYLOAD, **({"install_token": supplied} if supplied is not None else {})}
    response = _make_client(db).post("/api/auth/register", json=body)
    assert response.status_code == 403
    assert response.json()["detail"] == "A valid install token is required for the first administrator."
    assert db.added == []


def test_unconfigured_install_token_refuses_bootstrap(policy):
    policy.INSTALL_TOKEN = ""
    db = _FakeDB([None, None, 0, 0, None])
    response = _make_client(db).post("/api/auth/register", json=_PAYLOAD)
    assert response.status_code == 503
    assert response.json()["detail"] == "Set INSTALL_TOKEN on the server before creating the first administrator."
    assert db.added == []


def test_matching_install_token_bootstraps_once_and_is_ignored_afterward(policy):
    db = _FakeDB([None, None, 0, 0, None])
    response = _make_client(db).post("/api/auth/register", json={**_PAYLOAD, "install_token": policy.INSTALL_TOKEN})
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "platform_admin"
    assert response.json()["user"]["tenant_id"]
    policy.INSTALL_TOKEN = ""
    later = _make_client(_FakeDB([None, None, 1])).post("/api/auth/register", json={**_PAYLOAD, "install_token": "wrong"})
    assert later.status_code == 201
    assert later.json()["user"]["role"] == "member"


def test_account_created_during_bootstrap_lock_does_not_require_install_token(policy):
    policy.INSTALL_TOKEN = ""
    db = _FakeDB([None, None, 0, 1])
    response = _make_client(db).post("/api/auth/register", json=_PAYLOAD)
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "member"
