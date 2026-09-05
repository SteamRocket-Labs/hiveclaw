"""Helpers for resolving tenant scope from request context."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import pin_rls_tenant_context


TENANT_SCOPE_QUARANTINE_ID = uuid.UUID("00000000-0000-4000-8000-000000000023")
TENANT_SCOPE_QUARANTINE_SLUG = "__hive_scope_quarantine__"


def resolve_tenant_scope(current_user, requested_tenant_id: uuid.UUID | str | None = None) -> uuid.UUID:
    """Resolve the effective tenant for a request.

    Company selection has exactly one validated channel: the authenticated
    selected tenant already proven live (and, for a cross-company platform
    administrator, impersonation-audited fail-closed) by ``get_current_user``.
    An explicit ``tenant_id`` query/body parameter is therefore only a
    consistency echo: for a platform administrator it must equal the
    authenticated ``current_user.tenant_id`` selection, otherwise the caller
    gets a truthful recovery error pointing at company selection instead of a
    second, unvalidated cross-company switch. Other users remain limited to
    their own tenant.
    """
    if requested_tenant_id:
        try:
            target_tenant_id = uuid.UUID(str(requested_tenant_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid tenant_id") from exc

        if current_user.role == "platform_admin":
            selected_tenant_id = getattr(current_user, "tenant_id", None)
            if selected_tenant_id is None or str(selected_tenant_id) != str(target_tenant_id):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Select the company first: the tenant_id parameter must match the "
                        "authenticated selected company (X-Tenant-Id)"
                    ),
                )
            return target_tenant_id
        if str(current_user.tenant_id) != str(target_tenant_id):
            raise HTTPException(status_code=403, detail="Access denied")
        return target_tenant_id

    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    return current_user.tenant_id


async def resolve_and_pin_tenant_scope(
    db: AsyncSession,
    current_user,
    requested_tenant_id: uuid.UUID | str | None = None,
) -> uuid.UUID:
    """Resolve request tenant scope and pin the active DB session to it for RLS."""
    target_tenant_id = resolve_tenant_scope(current_user, requested_tenant_id)
    await pin_rls_tenant_context(db, target_tenant_id)
    return target_tenant_id
