"""Local Agent Bridge pairing, token, and auth helpers."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gateway_message import GatewayMessage
from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.local_agent_channel import LocalAgentChannel
from app.models.local_bridge import LocalAgentBridgeConnection, LocalAgentBridgePairingSession

BRIDGE_TOKEN_PREFIX = "hb_"
DEFAULT_PAIRING_EXPIRES_SECONDS = 15 * 60
DEFAULT_PAIRING_POLL_INTERVAL_SECONDS = 3
DEFAULT_SCOPES = (
    "local_agent:connect",
    "local_agent:receive",
    "local_agent:send",
    "local_agent:report",
    "presence:write",
    "gateway:poll",
    "gateway:report",
    "gateway:send-message",
    "files:upload",
)


@dataclass(frozen=True)
class BridgeAuthContext:
    """Resolved runtime identity for one local bridge connection."""

    connection_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID | None
    user_id: uuid.UUID
    scopes: tuple[str, ...]
    client_kind: str
    device_name: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_user_code(user_code: str) -> str:
    return user_code.strip().upper().replace(" ", "")


def generate_user_code() -> str:
    # Avoid ambiguous characters where possible; still short enough to read.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"HIVE-{suffix[:4]}-{suffix[4:]}"


def generate_device_code() -> str:
    return secrets.token_urlsafe(48)


def generate_bridge_token() -> str:
    return f"{BRIDGE_TOKEN_PREFIX}{secrets.token_urlsafe(48)}"


def normalize_scopes(scopes: list[str] | tuple[str, ...] | None) -> list[str]:
    requested = [str(scope).strip() for scope in (scopes or DEFAULT_SCOPES) if str(scope).strip()]
    allowed = set(DEFAULT_SCOPES)
    normalized = [scope for scope in dict.fromkeys(requested) if scope in allowed]
    return normalized or list(DEFAULT_SCOPES)


def _verification_uri(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/local-bridge/activate"


def _pairing_public_payload(
    *,
    session: LocalAgentBridgePairingSession,
    device_code: str,
    user_code: str,
    base_url: str,
) -> dict[str, Any]:
    verification_uri = _verification_uri(base_url)
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": f"{verification_uri}?user_code={user_code}",
        "expires_in": DEFAULT_PAIRING_EXPIRES_SECONDS,
        "interval": DEFAULT_PAIRING_POLL_INTERVAL_SECONDS,
        "pairing_id": str(session.id),
    }


async def create_pairing_session(db: AsyncSession, request: Any, *, base_url: str) -> dict[str, Any]:
    """Create an unbound device-flow pairing session for a local CLI."""

    user_code = generate_user_code()
    device_code = generate_device_code()
    session = LocalAgentBridgePairingSession(
        pairing_code_hash=hash_secret(normalize_user_code(user_code)),
        device_code_hash=hash_secret(device_code),
        device_name=str(request.device_name or "Local Agent").strip()[:255],
        client_kind=str(request.client_kind or "generic_mcp_stdio").strip()[:64],
        device_fingerprint=str(request.device_fingerprint or "unknown").strip()[:255],
        scopes=normalize_scopes(getattr(request, "scopes", None)),
        status="pending",
        metadata_json={},
        expires_at=utcnow() + timedelta(seconds=DEFAULT_PAIRING_EXPIRES_SECONDS),
    )
    db.add(session)
    await db.flush()
    await db.commit()
    await db.refresh(session)
    return _pairing_public_payload(session=session, device_code=device_code, user_code=user_code, base_url=base_url)


async def _load_pairing_by_user_code(db: AsyncSession, user_code: str) -> LocalAgentBridgePairingSession:
    async with enter_rls_bypass(db, reason="local bridge user-code pairing lookup"):
        result = await db.execute(
            select(LocalAgentBridgePairingSession).where(
                LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
            )
        )
    pairing = result.scalar_one_or_none()
    if pairing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pairing request not found")
    return pairing


async def _load_pairing_by_device_code(db: AsyncSession, device_code: str) -> LocalAgentBridgePairingSession:
    async with enter_rls_bypass(db, reason="local bridge device-code pairing lookup"):
        result = await db.execute(
            select(LocalAgentBridgePairingSession).where(
                LocalAgentBridgePairingSession.device_code_hash == hash_secret(device_code)
            )
        )
    pairing = result.scalar_one_or_none()
    if pairing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pairing request not found")
    return pairing


def _ensure_pairing_not_expired(pairing: LocalAgentBridgePairingSession) -> None:
    if pairing.expires_at <= utcnow():
        pairing.status = "expired"
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Pairing request expired")


async def approve_pairing_session(
    db: AsyncSession,
    *,
    user_code: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pairing = await _load_pairing_by_user_code(db, user_code)
    _ensure_pairing_not_expired(pairing)
    if pairing.status not in {"pending", "approved"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Pairing is {pairing.status}")
    pairing.user_id = user_id
    pairing.tenant_id = tenant_id
    pairing.agent_id = agent_id
    pairing.status = "approved"
    pairing.approved_at = utcnow()
    pairing.metadata_json = {**(pairing.metadata_json or {}), **(metadata or {})}
    await db.commit()
    return {
        "status": "approved",
        "pairing_id": str(pairing.id),
        "agent_id": str(agent_id) if agent_id else None,
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
    }


async def reject_pairing_session(
    db: AsyncSession,
    *,
    user_code: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    pairing = await _load_pairing_by_user_code(db, user_code)
    _ensure_pairing_not_expired(pairing)
    if agent_id and pairing.agent_id and pairing.agent_id != agent_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pairing request not found")
    pairing.user_id = user_id
    pairing.tenant_id = tenant_id
    pairing.agent_id = agent_id
    pairing.status = "rejected"
    pairing.rejected_at = utcnow()
    await db.commit()
    return {"status": "rejected", "pairing_id": str(pairing.id)}


async def exchange_pairing_session(db: AsyncSession, *, device_code: str) -> dict[str, Any]:
    pairing = await _load_pairing_by_device_code(db, device_code)
    _ensure_pairing_not_expired(pairing)
    if pairing.status == "pending":
        return {"status": "pending", "interval": DEFAULT_PAIRING_POLL_INTERVAL_SECONDS}
    if pairing.status == "rejected":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Pairing request rejected")
    if pairing.status == "claimed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pairing token already claimed")
    if pairing.status != "approved" or not pairing.tenant_id or not pairing.user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Pairing is {pairing.status}")

    await pin_rls_tenant_context(db, pairing.tenant_id)
    raw_token = generate_bridge_token()
    connection = LocalAgentBridgeConnection(
        tenant_id=pairing.tenant_id,
        agent_id=pairing.agent_id,
        user_id=pairing.user_id,
        device_name=pairing.device_name,
        client_kind=pairing.client_kind,
        device_fingerprint=pairing.device_fingerprint,
        token_hash=hash_secret(raw_token),
        scopes=normalize_scopes(pairing.scopes),
        status="active",
        metadata_json={"pairing_id": str(pairing.id)},
    )
    db.add(connection)
    await db.flush()
    pairing.connection_id = connection.id
    pairing.status = "claimed"
    pairing.claimed_at = utcnow()
    await db.commit()
    await db.refresh(connection)
    return {
        "status": "active",
        "access_token": raw_token,
        "token_type": "Bearer",
        "connection_id": str(connection.id),
        "tenant_id": str(connection.tenant_id),
        "agent_id": str(connection.agent_id) if connection.agent_id else None,
        "user_id": str(connection.user_id),
        "scopes": list(connection.scopes or []),
    }


def _parse_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or not token.startswith(BRIDGE_TOKEN_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bridge token")
    return token.strip()


async def resolve_bridge_auth_context(
    db: AsyncSession,
    *,
    authorization: str | None,
    last_seen_ip: str | None = None,
    user_agent: str | None = None,
) -> BridgeAuthContext:
    token = _parse_bearer_token(authorization)
    async with enter_rls_bypass(db, reason="local bridge bearer token lookup"):
        result = await db.execute(
            select(LocalAgentBridgeConnection).where(
                LocalAgentBridgeConnection.token_hash == hash_secret(token),
                LocalAgentBridgeConnection.status == "active",
            )
        )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bridge token")
    await pin_rls_tenant_context(db, connection.tenant_id)
    if connection.expires_at and connection.expires_at <= utcnow():
        connection.status = "expired"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bridge token expired")

    connection.last_seen_at = utcnow()
    connection.last_seen_ip = last_seen_ip
    connection.last_seen_user_agent = user_agent
    return BridgeAuthContext(
        connection_id=connection.id,
        tenant_id=connection.tenant_id,
        agent_id=connection.agent_id,
        user_id=connection.user_id,
        scopes=tuple(connection.scopes or []),
        client_kind=connection.client_kind,
        device_name=connection.device_name,
    )


def _presence_status_for(connection: LocalAgentBridgeConnection, channel: LocalAgentChannel | None) -> str:
    if connection.status != "active":
        return "offline"
    if channel is None:
        return "unknown"
    if channel.status == "online":
        return "online"
    if channel.status in {"offline", "stale"}:
        return "offline"
    return "unknown"


def serialize_connection_for_list(
    connection: LocalAgentBridgeConnection,
    *,
    channel: LocalAgentChannel | None = None,
) -> dict[str, Any]:
    presence_status = _presence_status_for(connection, channel)
    return {
        "id": str(connection.id),
        "tenant_id": str(connection.tenant_id),
        "agent_id": str(connection.agent_id) if connection.agent_id else None,
        "user_id": str(connection.user_id),
        "device_name": connection.device_name,
        "client_kind": connection.client_kind,
        "status": connection.status,
        "presence_status": presence_status,
        "presence_last_seen_at": channel.last_seen_at.isoformat() if channel and channel.last_seen_at else None,
        "runtime_kind": channel.runtime_kind if channel and channel.runtime_kind else None,
        "scopes": list(connection.scopes or []),
        "last_seen_at": connection.last_seen_at.isoformat() if connection.last_seen_at else None,
        "created_at": connection.created_at.isoformat() if connection.created_at else None,
        "revoked_at": connection.revoked_at.isoformat() if connection.revoked_at else None,
    }


async def list_connections(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    stmt = select(LocalAgentBridgeConnection).order_by(LocalAgentBridgeConnection.created_at.desc())
    if agent_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.agent_id == agent_id)
    if user_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.user_id == user_id)
    if tenant_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.tenant_id == tenant_id)
    result = await db.execute(stmt)
    connections = result.scalars().all()
    if not connections:
        return []

    connection_ids = [conn.id for conn in connections]
    channel_result = await db.execute(
        select(LocalAgentChannel).where(LocalAgentChannel.connection_id.in_(connection_ids))
    )
    channels_by_connection = {channel.connection_id: channel for channel in channel_result.scalars().all()}
    return [
        serialize_connection_for_list(conn, channel=channels_by_connection.get(conn.id))
        for conn in connections
    ]


async def revoke_connection(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    stmt = select(LocalAgentBridgeConnection).where(LocalAgentBridgeConnection.id == connection_id)
    if agent_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.agent_id == agent_id)
    if user_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.user_id == user_id)
    if tenant_id is not None:
        stmt = stmt.where(LocalAgentBridgeConnection.tenant_id == tenant_id)
    result = await db.execute(stmt)
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bridge connection not found")
    connection.status = "revoked"
    connection.revoked_at = utcnow()
    await db.commit()
    return {"status": "revoked", "connection_id": str(connection.id)}


async def enqueue_work_request(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    sender_user_id: uuid.UUID,
    content: str,
    metadata: dict[str, Any] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    message = GatewayMessage(
        agent_id=agent_id,
        tenant_id=tenant_id,
        sender_user_id=sender_user_id,
        conversation_id=conversation_id,
        content=content,
        status="pending",
        metadata_json={"kind": "work_request", **(metadata or {})},
    )
    db.add(message)
    await db.flush()
    await db.commit()
    return {"status": "pending", "message_id": str(message.id), "conversation_id": conversation_id}
