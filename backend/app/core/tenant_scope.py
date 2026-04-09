"""Helpers for resolving tenant scope from request context."""

from __future__ import annotations

import uuid

from fastapi import HTTPException


def resolve_tenant_scope(current_user, requested_tenant_id: uuid.UUID | str | None = None) -> uuid.UUID:
    """Resolve the effective tenant for a request.

    Platform admins may target any tenant by explicit `tenant_id`.
    Other users are limited to their own tenant.
    """
    if requested_tenant_id:
        try:
            target_tenant_id = uuid.UUID(str(requested_tenant_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid tenant_id") from exc

        if current_user.role == "platform_admin":
            return target_tenant_id
        if str(current_user.tenant_id) != str(target_tenant_id):
            raise HTTPException(status_code=403, detail="Access denied")
        return target_tenant_id

    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    return current_user.tenant_id
