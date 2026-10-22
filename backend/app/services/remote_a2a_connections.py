"""Owner-bound remote endpoint configuration and observed card discovery."""

from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import uuid
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.core.permissions import is_scoped_business_admin, require_agent_owner_or_admin
from app.database import pin_rls_tenant_context
from app.models.audit import AuditLog
from app.models.remote_a2a import RemoteA2AConnection
from app.services.agent_tool_config_storage import AgentToolConfigDecryptionError
from app.services.machine_identity_service import _owner_scope
from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError, _same_endpoint


def _frame(agent, actor, expected_user_id, expected_tenant_id):
    if actor.id != expected_user_id or agent.tenant_id != expected_tenant_id:
        raise HTTPException(409, detail={"code": "remote_a2a_request_scope_changed"})


def _view(agent, actor, connection, scope):
    from app.services.remote_a2a_runtime import authorization_valid
    from app.services.remote_a2a_withdrawal import public_withdrawal

    state = connection.state if connection else "not_configured"
    if connection and state != "disabled":
        if scope != (connection.owner_user_id, connection.owner_epoch):
            state = "needs_reauthorization"
        elif agent.deleted_at is not None or agent.deactivated_at is not None:
            state = "unavailable"
    return {
        "agent_id": str(agent.id),
        "state": state,
        "can_configure": is_scoped_business_admin(actor, resource_tenant_id=agent.tenant_id),
        "can_disable": bool(connection),
        "version": connection.version if connection else None,
        "request_id": str(connection.request_id) if connection else None,
        "card_url": connection.card_url if connection else None,
        "rpc_url": connection.rpc_url if connection else None,
        "credential_configured": bool(connection and connection.credential_configured),
        "allowed_networks": list(connection.allowed_networks) if connection else [],
        "max_response_bytes": connection.max_response_bytes if connection else 16 * 1024 * 1024,
        "timeout_seconds": connection.timeout_seconds if connection else 20,
        "agent_card": connection.agent_card if connection else None,
        "discovered_at": connection.discovered_at if connection else None,
        "discovery_error": connection.discovery_error if connection else None,
        "protocol_version": "1.0",
        "protocol_binding": "JSONRPC",
        "effect_control": "unverified",
        "cost_metering": "unknown",
        "withdrawal": public_withdrawal(connection),
        "runtime_activation": ("activated" if authorization_valid(agent, connection, scope)
            else "authorization_required" if agent.agent_type == "remote_a2a" else "not_activated"),
        "runtime_revision": (connection.runtime_authorization or {}).get("revision") if connection else None,
        "runtime_request_id": (connection.runtime_authorization or {}).get("request_id") if connection else None,
        "previous_runtime": (connection.runtime_authorization or {}).get("previous_runtime") if connection else None,
    }


async def _connection(db, agent, *, lock=False):
    statement = (
        select(RemoteA2AConnection)
        .where(RemoteA2AConnection.agent_id == agent.id, RemoteA2AConnection.tenant_id == agent.tenant_id)
        .execution_options(populate_existing=True)
    )
    return await db.scalar(statement.with_for_update() if lock else statement)


async def _credentials(db, connection):
    try:
        value = await db.scalar(
            select(RemoteA2AConnection.credential_config).where(
                RemoteA2AConnection.agent_id == connection.agent_id,
                RemoteA2AConnection.tenant_id == connection.tenant_id,
            )
        )
    except AgentToolConfigDecryptionError:
        raise HTTPException(503, detail={"code": "remote_a2a_credential_unavailable"}) from None
    if not isinstance(value, dict):
        raise HTTPException(503, detail={"code": "remote_a2a_credential_unavailable"})
    return value


async def get_connection(db, *, agent_id, actor):
    agent = await require_agent_owner_or_admin(db, actor, agent_id)
    try:
        scope = await _owner_scope(db, agent)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        scope = None
    return _view(agent, actor, await _connection(db, agent), scope)


def _validate_endpoint(value):
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise ValueError("Invalid endpoint")
        if any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid endpoint")
        if parts.port is not None and not 0 < parts.port <= 65535:
            raise ValueError("Invalid port")
    except ValueError:
        raise HTTPException(422, detail={"code": "remote_a2a_invalid_endpoint"}) from None
    return value


async def configure_connection(
    db,
    *,
    agent_id,
    actor,
    expected_user_id,
    expected_tenant_id,
    expected_version,
    request_id,
    card_url,
    rpc_url,
    bearer_token,
    allowed_networks,
    max_response_bytes,
    timeout_seconds,
):
    agent = await require_agent_owner_or_admin(db, actor, agent_id, lock=True)
    _frame(agent, actor, expected_user_id, expected_tenant_id)
    if not is_scoped_business_admin(actor, resource_tenant_id=agent.tenant_id):
        raise HTTPException(403, detail={"code": "remote_a2a_administrator_required"})
    if agent.deleted_at is not None or agent.deactivated_at is not None:
        raise HTTPException(409, detail={"code": "remote_a2a_employee_unavailable"})
    scope = await _owner_scope(db, agent)
    previous = await _connection(db, agent, lock=True)
    card_url, rpc_url = _validate_endpoint(card_url), _validate_endpoint(rpc_url)
    if bearer_token is not None and any(ord(char) <= 32 or ord(char) >= 127 for char in bearer_token):
        raise HTTPException(422, detail={"code": "remote_a2a_invalid_credential"})
    if bearer_token and any(bearer_token in url for url in (card_url, rpc_url)):
        raise HTTPException(422, detail={"code": "remote_a2a_credential_in_endpoint"})
    try:
        networks = sorted(set(str(ipaddress.ip_network(value, strict=False)) for value in allowed_networks))
    except ValueError:
        raise HTTPException(422, detail={"code": "remote_a2a_invalid_network_grant"}) from None
    request_hash = hmac.new(
        get_settings().SECRET_KEY.encode(),
        json.dumps(
            {
                "actor": str(actor.id),
                "owner_scope": [str(scope[0]), scope[1]],
                "expected_version": expected_version,
                "card_url": card_url,
                "rpc_url": rpc_url,
                "bearer_token": bearer_token,
                "allowed_networks": networks,
                "max_response_bytes": max_response_bytes,
                "timeout_seconds": timeout_seconds,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    if previous and previous.request_id == request_id:
        if previous.request_hash != request_hash:
            raise HTTPException(409, detail={"code": "remote_a2a_request_conflict"})
        return {**_view(agent, actor, previous, scope), "idempotent_replay": True}
    if (previous.version if previous else None) != expected_version:
        raise HTTPException(409, detail={"code": "remote_a2a_version_conflict"})
    if bearer_token is None and previous and scope != (previous.owner_user_id, previous.owner_epoch):
        raise HTTPException(409, detail={"code": "remote_a2a_new_owner_credential_required"})
    credentials = {"bearer_token": bearer_token} if bearer_token else {}
    if bearer_token is None and previous:
        if previous.credential_configured and (
            not _same_endpoint(card_url, previous.card_url) or not _same_endpoint(rpc_url, previous.rpc_url)
        ):
            raise HTTPException(409, detail={"code": "remote_a2a_new_endpoint_credential_required"})
        credentials = dict(await _credentials(db, previous)) if previous.credential_configured else {}
    if credentials.get("bearer_token") and any(credentials["bearer_token"] in url for url in (card_url, rpc_url)):
        raise HTTPException(422, detail={"code": "remote_a2a_credential_in_endpoint"})
    connection = previous or RemoteA2AConnection(agent_id=agent.id, tenant_id=agent.tenant_id)
    if not previous:
        db.add(connection)
    connection.owner_user_id, connection.owner_epoch = scope
    connection.configured_by_user_id = actor.id
    connection.card_url, connection.rpc_url = card_url, rpc_url
    connection.credential_config, connection.allowed_networks = credentials, networks
    connection.credential_configured = bool(credentials.get("bearer_token"))
    connection.max_response_bytes, connection.timeout_seconds = max_response_bytes, timeout_seconds
    connection.request_id, connection.request_hash = request_id, request_hash
    connection.state, connection.version = "configured", (previous.version + 1 if previous else 1)
    connection.agent_card = connection.discovered_at = connection.discovery_error = None
    connection.updated_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            user_id=actor.id,
            action="remote_a2a:configured",
            details={
                "version": connection.version,
                "request_id": str(request_id),
                "owner_epoch": scope[1],
                "private_network_grants": networks,
            },
        )
    )
    await db.commit()
    return {**_view(agent, actor, connection, scope), "idempotent_replay": False}


async def discover_connection(db, *, agent_id, actor, expected_user_id, expected_tenant_id, expected_version):
    agent = await require_agent_owner_or_admin(db, actor, agent_id, lock=True)
    _frame(agent, actor, expected_user_id, expected_tenant_id)
    if not is_scoped_business_admin(actor, resource_tenant_id=agent.tenant_id):
        raise HTTPException(403, detail={"code": "remote_a2a_administrator_required"})
    scope = await _owner_scope(db, agent)
    connection = await _connection(db, agent, lock=True)
    if not connection or connection.version != expected_version:
        raise HTTPException(409, detail={"code": "remote_a2a_version_conflict"})
    if (
        connection.state == "disabled"
        or scope != (connection.owner_user_id, connection.owner_epoch)
        or agent.deleted_at
        or agent.deactivated_at
    ):
        raise HTTPException(409, detail={"code": "remote_a2a_connection_unavailable"})
    credentials = await _credentials(db, connection) if connection.credential_configured else {}
    endpoint = RemoteA2AEndpoint(
        connection.card_url,
        connection.rpc_url,
        credentials.get("bearer_token"),
        tuple(connection.allowed_networks),
        connection.max_response_bytes,
        connection.timeout_seconds,
    )
    tenant_id = agent.tenant_id
    await db.commit()
    card, error = None, None
    try:
        card = await endpoint.discover()
    except RemoteA2AError as exc:
        error = exc.code
    # Do not retain an ownership lock while waiting on an external endpoint.
    # Recheck the actual actor, configuration and ownership before persisting.
    from app.services.runtime_user_identity import load_scoped_runtime_user

    await pin_rls_tenant_context(db, tenant_id)
    actor = await load_scoped_runtime_user(db, actor.id, tenant_id=tenant_id)
    if actor is None or not actor.is_active or not is_scoped_business_admin(actor, resource_tenant_id=tenant_id):
        raise HTTPException(403, detail={"code": "remote_a2a_administrator_unavailable"})
    agent = await require_agent_owner_or_admin(db, actor, agent_id, lock=True)
    _frame(agent, actor, expected_user_id, expected_tenant_id)
    current_scope = await _owner_scope(db, agent)
    connection = await _connection(db, agent, lock=True)
    if (
        not connection
        or connection.version != expected_version
        or current_scope != scope
        or connection.state == "disabled"
        or agent.deleted_at
        or agent.deactivated_at
    ):
        raise HTTPException(409, detail={"code": "remote_a2a_connection_changed"})
    connection.agent_card, connection.discovery_error = card, error
    connection.state = "configured" if error else "discovered"
    connection.discovered_at = None if error else datetime.now(timezone.utc)
    db.add(
        AuditLog(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            user_id=actor.id,
            action="remote_a2a:discovery",
            details={
                "version": connection.version,
                "state": connection.state,
                "error_code": connection.discovery_error,
            },
        )
    )
    await db.commit()
    return _view(agent, actor, connection, scope)


async def disable_connection(db, *, agent_id, actor, expected_user_id, expected_tenant_id, expected_version):
    agent = await require_agent_owner_or_admin(db, actor, agent_id, lock=True)
    _frame(agent, actor, expected_user_id, expected_tenant_id)
    connection = await _connection(db, agent, lock=True)
    from app.services.remote_a2a_withdrawal import capture, process

    previous = (connection.runtime_authorization or {}).get("withdrawal") if connection else None
    if (connection and connection.state == "disabled" and isinstance(previous, dict)
        and expected_version in {previous.get("configuration_version"), connection.version}):
        return _view(agent, actor, connection, None)
    if not connection or connection.version != expected_version:
        raise HTTPException(409, detail={"code": "remote_a2a_version_conflict"})
    withdrawal, endpoint = await capture(db, agent=agent, actor=actor, connection=connection)
    connection.state, connection.version = "disabled", connection.version + 1
    connection.credential_config = {}
    connection.credential_configured = False
    connection.agent_card = connection.discovered_at = connection.discovery_error = None
    connection.updated_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            user_id=actor.id,
            action="remote_a2a:disabled",
            details={"version": connection.version, "withdrawal": withdrawal},
        )
    )
    await db.commit()
    for task_id in withdrawal["task_ids"]:
        await process(db, task_id=uuid.UUID(task_id), receipt_id=withdrawal["id"], endpoint=endpoint)
    await db.refresh(connection)
    return _view(agent, actor, connection, None)
