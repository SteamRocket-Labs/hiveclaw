"""Tests for security.py — tenant-disabled enforcement in get_current_user."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.core.security import get_current_user


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


class _Result:
    def __init__(self, *, first_row=None, scalar=None):
        self._first_row = first_row
        self._scalar = scalar

    def first(self):
        return self._first_row

    def scalar_one_or_none(self):
        return self._scalar


class _TenantOverrideDB:
    """Fake DB that hides the admin user unless the query enters RLS bypass.

    This reproduces production strict RLS: when the request is already scoped to
    the selected target tenant, the platform_admin's own user row lives in a
    different home tenant and is invisible until identity lookup is deliberately
    performed outside that selected-tenant scope.
    """

    def __init__(self, user, target_tenant_active: bool | None = True):
        self.user = user
        self.target_tenant_active = target_tenant_active
        self.bypass_active = False
        self.statements: list[str] = []
        self.expunged = []

    async def execute(self, stmt):
        statement = str(stmt)
        self.statements.append(statement)
        if "SET LOCAL app.current_tenant_id = 'BYPASS'" in statement:
            self.bypass_active = True
            return _Result()
        if "SET LOCAL app.current_tenant_id" in statement:
            self.bypass_active = False
            return _Result()
        if "FROM users" in statement:
            if not self.bypass_active:
                return _Result(first_row=None)
            return _Result(first_row=(self.user, True))
        if "FROM tenants" in statement:
            return _Result(scalar=self.target_tenant_active)
        return _Result()

    def expunge(self, obj):
        self.expunged.append(obj)


@pytest.mark.asyncio
async def test_platform_admin_selected_tenant_override_can_still_resolve_admin_identity():
    home_tenant_id = uuid4()
    selected_tenant_id = uuid4()
    user_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        role="platform_admin",
        tenant_id=home_tenant_id,
        is_active=True,
    )
    db = _TenantOverrideDB(user)
    request = SimpleNamespace(headers={"x-tenant-id": str(selected_tenant_id)})
    credentials = SimpleNamespace(credentials="jwt")

    with patch(
        "app.core.security.decode_access_token",
        return_value={"sub": str(user_id), "role": "platform_admin", "tid": str(home_tenant_id)},
    ):
        current_user = await get_current_user(request=request, credentials=credentials, db=db)

    assert current_user.tenant_id == selected_tenant_id
    assert db.expunged == [user]
    assert any("app.current_tenant_id = 'BYPASS'" in statement for statement in db.statements)


@pytest.mark.asyncio
async def test_platform_admin_selected_tenant_override_rejects_missing_target_tenant():
    home_tenant_id = uuid4()
    selected_tenant_id = uuid4()
    user_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        role="platform_admin",
        tenant_id=home_tenant_id,
        is_active=True,
    )
    db = _TenantOverrideDB(user, target_tenant_active=None)
    request = SimpleNamespace(headers={"x-tenant-id": str(selected_tenant_id)})
    credentials = SimpleNamespace(credentials="jwt")

    with patch(
        "app.core.security.decode_access_token",
        return_value={"sub": str(user_id), "role": "platform_admin", "tid": str(home_tenant_id)},
    ), pytest.raises(HTTPException) as exc_info:
        await get_current_user(request=request, credentials=credentials, db=db)

    assert exc_info.value.status_code == 404
