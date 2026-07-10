"""Pure protocol contracts for governed Local Agent remote execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Iterable

from app.services.execution_receipts import build_execution_receipt as build_execution_receipt


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def effective_local_capabilities(
    *,
    server_capabilities: Iterable[str],
    agent_capabilities: Iterable[str],
    reported_capabilities: Iterable[str],
) -> tuple[str, ...]:
    """Return the only capabilities a local runner may exercise."""

    server = {str(item).strip() for item in server_capabilities if str(item).strip()}
    agent = {str(item).strip() for item in agent_capabilities if str(item).strip()}
    reported = {str(item).strip() for item in reported_capabilities if str(item).strip()}
    return tuple(sorted(server & agent & reported))


def build_signed_capability_snapshot(
    *,
    signing_secret: str,
    issuer: str,
    subject_agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    scopes: Iterable[str],
    issued_at: datetime,
    expires_at: datetime,
    version: int,
) -> dict[str, Any]:
    if not signing_secret:
        raise ValueError("signing_secret is required")
    issued = issued_at.astimezone(UTC)
    expires = expires_at.astimezone(UTC)
    if expires <= issued:
        raise ValueError("expires_at must be after issued_at")
    body = {
        "schema": "hive.local_capability_snapshot.v1",
        "issuer": str(issuer),
        "subject_agent_id": str(subject_agent_id),
        "tenant_id": str(tenant_id),
        "scopes": sorted({str(scope).strip() for scope in scopes if str(scope).strip()}),
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "version": max(1, int(version)),
    }
    snapshot_hash = hashlib.sha256(_canonical_json(body)).hexdigest()
    signature = hmac.new(
        signing_secret.encode("utf-8"),
        snapshot_hash.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return {**body, "snapshot_hash": snapshot_hash, "signature": signature}


def verify_capability_snapshot(
    snapshot: dict[str, Any],
    *,
    signing_secret: str,
    now: datetime | None = None,
) -> bool:
    if not signing_secret or snapshot.get("schema") != "hive.local_capability_snapshot.v1":
        return False
    body = {
        key: snapshot.get(key)
        for key in (
            "schema",
            "issuer",
            "subject_agent_id",
            "tenant_id",
            "scopes",
            "issued_at",
            "expires_at",
            "version",
        )
    }
    expected_hash = hashlib.sha256(_canonical_json(body)).hexdigest()
    supplied_hash = str(snapshot.get("snapshot_hash") or "")
    supplied_signature = str(snapshot.get("signature") or "")
    expected_signature = hmac.new(
        signing_secret.encode("utf-8"),
        expected_hash.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, supplied_hash):
        return False
    if not hmac.compare_digest(expected_signature, supplied_signature):
        return False
    try:
        expires_at = datetime.fromisoformat(str(snapshot["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    check_at = (now or datetime.now(UTC)).astimezone(UTC)
    return expires_at.astimezone(UTC) > check_at
