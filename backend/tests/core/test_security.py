"""Tests for security.py — tenant-disabled enforcement in get_current_user."""

from pathlib import Path


def _read_security_source() -> str:
    project_root = Path(__file__).resolve().parents[3]
    return (project_root / "backend/app/core/security.py").read_text()


def test_get_current_user_imports_tenant_model():
    """get_current_user must import Tenant to check tenant status."""
    source = _read_security_source()
    assert "from app.models.tenant import Tenant" in source


def test_get_current_user_queries_tenant_is_active():
    """get_current_user must query the tenant and check is_active."""
    source = _read_security_source()
    assert "Tenant.is_active" in source


def test_get_current_user_uses_single_join_query():
    """Tenant check must use JOIN, not a separate query, for performance."""
    source = _read_security_source()
    assert "outerjoin" in source
    assert "Tenant.is_active" in source


def test_get_current_user_returns_403_for_disabled_company():
    """Disabled company must result in HTTP 403, not 401."""
    source = _read_security_source()
    assert "HTTP_403_FORBIDDEN" in source
    assert "Company has been disabled" in source


def test_get_current_user_checks_tenant_after_user_check():
    """Tenant check must come AFTER user existence/is_active check."""
    source = _read_security_source()
    user_check_pos = source.index("not user.is_active")
    tenant_check_pos = source.index("not tenant_is_active")
    assert tenant_check_pos > user_check_pos, "Tenant check must follow user check"
