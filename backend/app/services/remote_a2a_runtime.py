"""Authenticated executor selection and revision-bound standing remote authority."""

from datetime import datetime, timezone
import hashlib
import json

from fastapi import HTTPException

from app.core.permissions import is_scoped_business_admin, require_agent_owner_or_admin
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason, resolve_live_effect_principal
from app.services.machine_identity_service import _owner_scope


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _authorization_hash(value):
    # This exact field records withdrawal of older work; it grants no new
    # runtime capability. All standing authority fields remain hash-bound.
    return _digest({key: item for key, item in value.items() if key != "withdrawal"})


def authorization_valid(agent, connection, scope):
    authorization = connection.runtime_authorization if connection else None
    return bool(agent.agent_type == "remote_a2a" and isinstance(authorization, dict)
        and authorization.get("enabled") is True and authorization.get("uncontrolled_effects_acknowledged") is True
        and authorization.get("unknown_cost_acknowledged") is True and connection.state == "discovered"
        and authorization.get("configuration_version") == connection.version
        and scope == (connection.owner_user_id, connection.owner_epoch)
        and authorization.get("owner_scope") == [str(scope[0]), scope[1]])


async def require_runtime_authorization(db, *, agent, expected_hash=None):
    from app.services.remote_a2a_connections import _connection

    connection = await _connection(db, agent)
    scope = await _owner_scope(db, agent)
    if not authorization_valid(agent, connection, scope):
        raise HTTPException(409, detail={"code": "remote_a2a_runtime_authorization_required"})
    authorization = connection.runtime_authorization
    if expected_hash is not None and _authorization_hash(authorization) != expected_hash:
        raise HTTPException(409, detail={"code": "remote_a2a_runtime_authorization_changed"})
    return connection, authorization, _authorization_hash(authorization)


async def select_runtime(db, *, agent_id, actor, expected_user_id, expected_tenant_id, expected_version,
    expected_runtime_revision, request_id, selected_runtime, uncontrolled_effects_acknowledged,
    unknown_cost_acknowledged, required_controls):
    from app.services.remote_a2a_connections import _connection, _credentials, _frame, _view
    from app.services.business_teams import _live_user

    current = await _live_user(db, actor, fresh=True)
    agent = await require_agent_owner_or_admin(db, current, agent_id, lock=True)
    _frame(agent, current, expected_user_id, expected_tenant_id)
    scope = await _owner_scope(db, agent)
    connection = await _connection(db, agent, lock=True)
    if connection is None or connection.version != expected_version:
        raise HTTPException(409, detail={"code": "remote_a2a_version_conflict"})
    enabled = selected_runtime == "remote_a2a"
    if enabled and not is_scoped_business_admin(current, resource_tenant_id=agent.tenant_id):
        raise HTTPException(403, detail={"code": "remote_a2a_administrator_required"})
    principal = await resolve_live_effect_principal(db, agent)
    tenant = await db.get(Tenant, agent.tenant_id, populate_existing=True)
    if enabled and (tenant is None or not tenant.is_active or get_agent_lifecycle_block_reason(agent) or principal.block_reason):
        raise HTTPException(409, detail={"code": "remote_a2a_employee_unavailable"})
    if enabled and (not uncontrolled_effects_acknowledged or not unknown_cost_acknowledged):
        raise HTTPException(409, detail={"code": "remote_a2a_owner_authorization_required"})
    if enabled and required_controls:
        raise HTTPException(409, detail={"code": "remote_a2a_required_control_unavailable"})
    if enabled and (connection.state != "discovered" or scope != (connection.owner_user_id, connection.owner_epoch)):
        raise HTTPException(409, detail={"code": "remote_a2a_connection_unavailable"})
    if enabled and connection.credential_configured:
        await _credentials(db, connection)
    previous = connection.runtime_authorization or {}
    command = {"actor_user_id": str(current.id), "owner_scope": [str(scope[0]), scope[1]],
        "configuration_version": expected_version, "expected_revision": expected_runtime_revision,
        "selected_runtime": selected_runtime, "uncontrolled_effects_acknowledged": uncontrolled_effects_acknowledged,
        "unknown_cost_acknowledged": unknown_cost_acknowledged, "required_controls": required_controls}
    request_hash = _digest(command)
    if previous.get("request_id") == str(request_id):
        if previous.get("request_hash") != request_hash:
            raise HTTPException(409, detail={"code": "remote_a2a_request_conflict"})
        return {**_view(agent, current, connection, scope), "idempotent_replay": True}
    if previous.get("revision") != expected_runtime_revision:
        raise HTTPException(409, detail={"code": "remote_a2a_runtime_revision_conflict"})
    prior_runtime = previous.get("previous_runtime") if agent.agent_type == "remote_a2a" else agent.agent_type
    if prior_runtime not in {"native", "local_agent"}:
        raise HTTPException(409, detail={"code": "remote_a2a_previous_runtime_unavailable"})
    authorization = {**command, "schema": "hive.remote_a2a_runtime_authorization.v1",
        "request_id": str(request_id), "request_hash": request_hash, "enabled": enabled,
        "previous_runtime": prior_runtime, "revision": (expected_runtime_revision or 0) + 1,
        "selected_at": datetime.now(timezone.utc).isoformat()}
    if isinstance(previous.get("withdrawal"), dict):
        # Restoring an executor does not revoke an already committed request
        # to cancel old remote work or erase its unresolved control receipts.
        authorization["withdrawal"] = previous["withdrawal"]
    connection.runtime_authorization = authorization
    agent.agent_type = "remote_a2a" if enabled else prior_runtime
    db.add(AuditLog(tenant_id=agent.tenant_id, agent_id=agent.id, user_id=current.id,
        action="remote_a2a:runtime_selected", details=authorization))
    await db.commit()
    return {**_view(agent, current, connection, scope), "idempotent_replay": False}
