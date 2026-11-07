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
    db = _FakeDB([1, None, None])
    client = _make_client(db)
    policy.PUBLIC_REGISTRATION_ENABLED = False
    denied = client.post("/api/auth/register", json=_PAYLOAD)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Public registration is closed. Sign in with an existing account or SSO."
    assert db.added == []
    assert any("count(" in statement for statement in db.executed_statements)
    assert not any("pg_advisory_xact_lock" in statement for statement in db.executed_statements)
    policy.PUBLIC_REGISTRATION_ENABLED = True
    allowed = _make_client(_FakeDB([1, None, None])).post("/api/auth/register", json=_PAYLOAD)
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
@pytest.mark.parametrize("enabled", [True, False])
def test_first_admin_requires_matching_install_token(policy, supplied, enabled):
    policy.PUBLIC_REGISTRATION_ENABLED = enabled
    db = _FakeDB([0, 0, None, None, None])
    body = {**_PAYLOAD, **({"install_token": supplied} if supplied is not None else {})}
    response = _make_client(db).post("/api/auth/register", json=body)
    assert response.status_code == 403
    assert response.json()["detail"] == "A valid install token is required for the first administrator."
    assert db.added == []
    assert any("pg_advisory_xact_lock" in statement for statement in db.executed_statements)


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("supplied", [None, "wrong-token", "local-install-token"])
def test_unconfigured_install_token_refuses_bootstrap(policy, enabled, supplied):
    policy.PUBLIC_REGISTRATION_ENABLED = enabled
    policy.INSTALL_TOKEN = ""
    db = _FakeDB([0, 0, None, None, None])
    response = _make_client(db).post("/api/auth/register", json={**_PAYLOAD, "install_token": supplied})
    assert response.status_code == 503
    assert response.json()["detail"] == "Set INSTALL_TOKEN on the server before creating the first administrator."
    assert db.added == []
    assert any("pg_advisory_xact_lock" in statement for statement in db.executed_statements)


@pytest.mark.parametrize("enabled", [True, False])
def test_matching_install_token_bootstraps_once_and_is_ignored_afterward(policy, enabled):
    policy.PUBLIC_REGISTRATION_ENABLED = enabled
    db = _FakeDB([0, 0, None, None, None])
    response = _make_client(db).post("/api/auth/register", json={**_PAYLOAD, "install_token": policy.INSTALL_TOKEN})
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "platform_admin"
    assert response.json()["user"]["tenant_id"]
    assert any("pg_advisory_xact_lock" in statement for statement in db.executed_statements)
    policy.PUBLIC_REGISTRATION_ENABLED = True
    policy.INSTALL_TOKEN = ""
    later = _make_client(_FakeDB([1, None, None])).post("/api/auth/register", json={**_PAYLOAD, "install_token": "wrong"})
    assert later.status_code == 201
    assert later.json()["user"]["role"] == "member"


@pytest.mark.parametrize("configured", ["local-install-token", " \tlocal-install-token\n"])
def test_install_token_comparison_ignores_surrounding_whitespace(policy, configured):
    policy.INSTALL_TOKEN = configured
    response = _make_client(_FakeDB([0, 0, None, None, None])).post("/api/auth/register",
        json={**_PAYLOAD, "install_token": " \nlocal-install-token\t "})
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "platform_admin"


@pytest.mark.parametrize("enabled", [True, False])
def test_account_created_during_bootstrap_lock_does_not_require_install_token(policy, enabled):
    policy.PUBLIC_REGISTRATION_ENABLED = enabled
    policy.INSTALL_TOKEN = ""
    db = _FakeDB([0, 1, None, None])
    response = _make_client(db).post("/api/auth/register", json=_PAYLOAD)
    assert response.status_code == (201 if enabled else 403)
    if enabled:
        assert response.json()["user"]["role"] == "member"
    else:
        assert response.json()["detail"] == "Public registration is closed. Sign in with an existing account or SSO."
        assert db.added == []
    assert any("pg_advisory_xact_lock" in statement for statement in db.executed_statements)


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("configured", [True, False])
@pytest.mark.parametrize("supplied", [None, "wrong-token", "local-install-token"])
def test_existing_installation_applies_registration_policy_not_install_token(policy, enabled, configured, supplied):
    policy.PUBLIC_REGISTRATION_ENABLED = enabled
    policy.INSTALL_TOKEN = "local-install-token" if configured else ""
    db = _FakeDB([1, None, None])
    response = _make_client(db).post("/api/auth/register", json={**_PAYLOAD, "install_token": supplied})
    assert response.status_code == (201 if enabled else 403)
    if enabled:
        assert response.json()["user"]["role"] == "member"
    else:
        assert response.json()["detail"] == "Public registration is closed. Sign in with an existing account or SSO."
        assert db.added == []
