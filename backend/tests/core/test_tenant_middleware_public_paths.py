"""Tests for tenant middleware public-path classification."""

from app.core.tenant_middleware import _is_public_path


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
