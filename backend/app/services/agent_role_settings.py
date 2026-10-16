"""Versioned responsibilities remain settings, never capability authority."""

import hashlib
import uuid

from fastapi import HTTPException


def role_revision(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def role_snapshot(agent):
    return {"target_agent_id": str(agent.id), "target_agent_name": agent.name,
        "role_description": agent.role_description or "", "role_revision": role_revision(agent.role_description)}


async def validate_role_configuration_answer(db, *, payload, user, tenant_id):
    from app.core.permissions import check_agent_access

    if user is None:
        raise HTTPException(status_code=403, detail="Authenticated requester required")
    target, access = await check_agent_access(db, user, uuid.UUID(payload["configuration_request"]["target_agent_id"]))
    if target.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Configuration target not found")
    if access != "manage":
        raise HTTPException(status_code=403, detail="Employee management access required")
    ready = payload.get("configuration_ready") or {}
    if ready.get("role_revision") != role_revision(target.role_description):
        raise HTTPException(status_code=409, detail={"code": "role_configuration_changed",
            "message": "Review the current responsibilities before continuing."})
