"""Tamper-evident operator security audit chain and read surface."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping
import uuid

from sqlalchemy import text

from app.database import async_session, enter_rls_bypass

PLATFORM_SECURITY_AUDIT_SCHEMA = "hive.platform_security_audit.v2"
PLATFORM_SECURITY_AUDIT_LOCK_KEY = 7_240_202_607
PLATFORM_SECURITY_ACTION_PATTERN = "platform_security.%"

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(:lock_key)")
_HEAD_SQL = text(
    """
    SELECT id, action, details, created_at
    FROM audit_logs
    WHERE tenant_id IS NULL
      AND action LIKE :action_pattern
      AND details->>'schema_version' = 'hive.platform_security_audit.v2'
      AND details->>'sequence_num' ~ '^[0-9]+$'
    ORDER BY (details->>'sequence_num')::bigint DESC
    LIMIT 1
    """
)
_LEGACY_SQL = text(
    """
    SELECT id, action, details, created_at
    FROM audit_logs
    WHERE tenant_id IS NULL
      AND action LIKE :action_pattern
      AND COALESCE(details->>'schema_version', '') != 'hive.platform_security_audit.v2'
    ORDER BY created_at ASC, id ASC
    """
)
_CHAIN_SQL = text(
    """
    SELECT id, action, details, created_at
    FROM audit_logs
    WHERE tenant_id IS NULL
      AND action LIKE :action_pattern
      AND details->>'schema_version' = 'hive.platform_security_audit.v2'
    ORDER BY created_at ASC, id ASC
    """
)


def _as_mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    return {
        "id": getattr(row, "id"),
        "action": getattr(row, "action"),
        "details": getattr(row, "details"),
        "created_at": getattr(row, "created_at"),
    }


def _details_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("platform security audit details must be a JSON object")


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat()
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_platform_security_event_hash(
    *,
    event_id: uuid.UUID | str,
    row_action: str,
    envelope: dict[str, Any],
) -> str:
    hash_envelope = deepcopy(envelope)
    hash_envelope.pop("event_hash", None)
    payload = {
        "event_id": str(event_id),
        "row_action": row_action,
        "envelope": hash_envelope,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def seal_platform_security_envelope(
    *,
    event_id: uuid.UUID,
    row_action: str,
    base_envelope: dict[str, Any],
    sequence_num: int,
    prev_hash: str,
    recorded_at: datetime,
) -> dict[str, Any]:
    envelope = deepcopy(base_envelope)
    envelope.update(
        {
            "schema_version": PLATFORM_SECURITY_AUDIT_SCHEMA,
            "sequence_num": sequence_num,
            "prev_hash": prev_hash,
            "recorded_at": _timestamp(recorded_at),
        }
    )
    envelope["event_hash"] = compute_platform_security_event_hash(
        event_id=event_id,
        row_action=row_action,
        envelope=envelope,
    )
    return envelope


def compute_legacy_platform_audit_anchor(rows: list[Any]) -> dict[str, Any]:
    canonical_rows: list[dict[str, Any]] = []
    for raw_row in rows:
        row = _as_mapping(raw_row)
        canonical_rows.append(
            {
                "id": str(row["id"]),
                "action": str(row["action"]),
                "details": _details_dict(row["details"]),
                "created_at": _timestamp(row["created_at"]),
            }
        )
    canonical_rows.sort(key=lambda row: (row["created_at"], row["id"]))
    digest = hashlib.sha256(_canonical_json(canonical_rows).encode("utf-8")).hexdigest()
    return {
        "legacy_event_count": len(canonical_rows),
        "legacy_events_digest": digest,
        "legacy_first_event_id": canonical_rows[0]["id"] if canonical_rows else None,
        "legacy_last_event_id": canonical_rows[-1]["id"] if canonical_rows else None,
    }


def platform_security_chain_position(row: Any) -> tuple[int, str]:
    mapped_row = _as_mapping(row)
    details = _details_dict(mapped_row["details"])
    sequence_num = details.get("sequence_num")
    event_hash = details.get("event_hash")
    prev_hash = details.get("prev_hash")
    if (
        details.get("schema_version") != PLATFORM_SECURITY_AUDIT_SCHEMA
        or not isinstance(sequence_num, int)
        or isinstance(sequence_num, bool)
        or sequence_num < 1
        or not isinstance(event_hash, str)
        or len(event_hash) != 64
        or (sequence_num == 1 and prev_hash != "genesis")
        or (sequence_num > 1 and (not isinstance(prev_hash, str) or len(prev_hash) != 64))
        or details.get("recorded_at") != _timestamp(mapped_row["created_at"])
        or mapped_row["action"] != f"platform_security.{details.get('event_type')}"
        or event_hash
        != compute_platform_security_event_hash(
            event_id=mapped_row["id"],
            row_action=str(mapped_row["action"]),
            envelope=details,
        )
    ):
        raise ValueError("invalid platform security audit chain head")
    return sequence_num, event_hash


def _invalid_verification(
    *,
    rows: list[dict[str, Any]],
    legacy_rows: list[Any],
    event_id: Any,
    reason: str,
    head_hash: str | None,
) -> dict[str, Any]:
    return {
        "valid": False,
        "chain_version": PLATFORM_SECURITY_AUDIT_SCHEMA,
        "total_events": len(rows),
        "legacy_event_count": len(legacy_rows),
        "head_hash": head_hash,
        "first_invalid_event_id": str(event_id) if event_id is not None else None,
        "reason": reason,
    }


def verify_platform_security_chain_rows(chain_rows: list[Any], legacy_rows: list[Any]) -> dict[str, Any]:
    rows = [_as_mapping(row) for row in chain_rows]
    for row in rows:
        try:
            envelope = _details_dict(row["details"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return _invalid_verification(
                rows=rows,
                legacy_rows=legacy_rows,
                event_id=row.get("id"),
                reason="invalid_envelope",
                head_hash=None,
            )
        sequence_num = envelope.get("sequence_num")
        if not isinstance(sequence_num, int) or isinstance(sequence_num, bool):
            return _invalid_verification(
                rows=rows,
                legacy_rows=legacy_rows,
                event_id=row.get("id"),
                reason="invalid_sequence",
                head_hash=None,
            )
    rows.sort(key=lambda row: _details_dict(row["details"])["sequence_num"])
    if not rows:
        if legacy_rows:
            return _invalid_verification(
                rows=rows,
                legacy_rows=legacy_rows,
                event_id=_as_mapping(legacy_rows[0]).get("id"),
                reason="chain_not_initialized",
                head_hash=None,
            )
        return {
            "valid": True,
            "chain_version": PLATFORM_SECURITY_AUDIT_SCHEMA,
            "total_events": 0,
            "legacy_event_count": 0,
            "head_hash": None,
            "first_invalid_event_id": None,
            "reason": None,
        }

    expected_prev_hash = "genesis"
    expected_sequence = 1
    previous_legacy_count = 0
    head_hash: str | None = None
    first_envelope: dict[str, Any] | None = None
    for row in rows:
        event_id = row["id"]
        try:
            envelope = _details_dict(row["details"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return _invalid_verification(
                rows=rows,
                legacy_rows=legacy_rows,
                event_id=event_id,
                reason="invalid_envelope",
                head_hash=head_hash,
            )
        if first_envelope is None:
            first_envelope = envelope
        if envelope.get("schema_version") != PLATFORM_SECURITY_AUDIT_SCHEMA:
            reason = "schema_version_mismatch"
        elif envelope.get("sequence_num") != expected_sequence:
            reason = "sequence_gap"
        elif envelope.get("prev_hash") != expected_prev_hash:
            reason = "prev_hash_mismatch"
        elif envelope.get("recorded_at") != _timestamp(row["created_at"]):
            reason = "recorded_at_mismatch"
        elif row["action"] != f"platform_security.{envelope.get('event_type')}":
            reason = "row_action_mismatch"
        elif not isinstance(envelope.get("legacy_anchor"), dict):
            reason = "legacy_anchor_missing"
        elif (
            not isinstance(envelope["legacy_anchor"].get("legacy_event_count"), int)
            or envelope["legacy_anchor"]["legacy_event_count"] < previous_legacy_count
        ):
            reason = "legacy_anchor_regression"
        elif envelope.get("event_hash") != compute_platform_security_event_hash(
            event_id=event_id,
            row_action=str(row["action"]),
            envelope=envelope,
        ):
            reason = "event_hash_mismatch"
        else:
            reason = None
        if reason is not None:
            return _invalid_verification(
                rows=rows,
                legacy_rows=legacy_rows,
                event_id=event_id,
                reason=reason,
                head_hash=head_hash,
            )
        expected_sequence += 1
        expected_prev_hash = str(envelope["event_hash"])
        previous_legacy_count = envelope["legacy_anchor"]["legacy_event_count"]
        head_hash = expected_prev_hash

    if first_envelope is None or first_envelope.get("event_type") != "chain_cutover":
        return _invalid_verification(
            rows=rows,
            legacy_rows=legacy_rows,
            event_id=rows[0]["id"],
            reason="missing_chain_cutover",
            head_hash=head_hash,
        )
    cutover_anchor = first_envelope.get("legacy_anchor")
    if first_envelope.get("details") != cutover_anchor:
        return _invalid_verification(
            rows=rows,
            legacy_rows=legacy_rows,
            event_id=rows[0]["id"],
            reason="cutover_anchor_mismatch",
            head_hash=head_hash,
        )
    expected_anchor = compute_legacy_platform_audit_anchor(legacy_rows)
    latest_anchor = _details_dict(rows[-1]["details"]).get("legacy_anchor")
    if not isinstance(latest_anchor, dict) or any(
        latest_anchor.get(key) != value for key, value in expected_anchor.items()
    ):
        return _invalid_verification(
            rows=rows,
            legacy_rows=legacy_rows,
            event_id=rows[-1]["id"],
            reason="legacy_anchor_mismatch",
            head_hash=head_hash,
        )
    return {
        "valid": True,
        "chain_version": PLATFORM_SECURITY_AUDIT_SCHEMA,
        "total_events": len(rows),
        "legacy_event_count": len(legacy_rows),
        "head_hash": head_hash,
        "first_invalid_event_id": None,
        "reason": None,
    }


async def acquire_platform_security_chain_lock(db: Any) -> None:
    await db.execute(_LOCK_SQL, {"lock_key": PLATFORM_SECURITY_AUDIT_LOCK_KEY})


async def load_platform_security_chain_head(db: Any) -> dict[str, Any] | None:
    result = await db.execute(_HEAD_SQL, {"action_pattern": PLATFORM_SECURITY_ACTION_PATTERN})
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def load_legacy_platform_security_rows(db: Any) -> list[dict[str, Any]]:
    result = await db.execute(_LEGACY_SQL, {"action_pattern": PLATFORM_SECURITY_ACTION_PATTERN})
    return [dict(row) for row in result.mappings().all()]


async def query_platform_security_audit_events(
    *,
    event_type: str | None,
    severity: str | None,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    conditions = ["tenant_id IS NULL", "action LIKE :action_pattern"]
    params: dict[str, Any] = {
        "action_pattern": PLATFORM_SECURITY_ACTION_PATTERN,
        "limit": limit,
        "offset": offset,
    }
    if event_type:
        conditions.append("details->>'event_type' = :event_type")
        params["event_type"] = event_type
    if severity:
        conditions.append("details->>'severity' = :severity")
        params["severity"] = severity
    if actor_id:
        conditions.append("details->'actor'->>'id' = :actor_id")
        params["actor_id"] = str(actor_id)
    if request_id:
        conditions.append("details->>'request_id' = :request_id")
        params["request_id"] = request_id
    where_clause = " AND ".join(conditions)
    count_sql = text(f"SELECT count(*) FROM audit_logs WHERE {where_clause}")
    data_sql = text(
        f"""
        SELECT id, action, details, created_at
        FROM audit_logs
        WHERE {where_clause}
        ORDER BY created_at DESC, id DESC
        LIMIT :limit OFFSET :offset
        """
    )
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="operator platform security audit query") as bypass_db,
    ):
        await acquire_platform_security_chain_lock(bypass_db)
        chain_rows = (
            (await bypass_db.execute(_CHAIN_SQL, {"action_pattern": PLATFORM_SECURITY_ACTION_PATTERN})).mappings().all()
        )
        legacy_rows = await load_legacy_platform_security_rows(bypass_db)
        chain_verification = verify_platform_security_chain_rows(
            list(chain_rows),
            legacy_rows,
        )
        total = int((await bypass_db.execute(count_sql, params)).scalar_one())
        rows = (await bypass_db.execute(data_sql, params)).mappings().all()
    items = []
    for raw_row in rows:
        row = _as_mapping(raw_row)
        envelope = _details_dict(row["details"])
        items.append(
            {
                "id": str(row["id"]),
                "action": str(row["action"]),
                "created_at": _timestamp(row["created_at"]),
                "envelope": envelope,
                "chain_status": ("chained" if chain_verification["valid"] else "chain_invalid")
                if envelope.get("schema_version") == PLATFORM_SECURITY_AUDIT_SCHEMA
                else ("legacy_anchored" if chain_verification["valid"] else "legacy_unverified"),
            }
        )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "chain_verification": chain_verification,
    }


async def verify_persisted_platform_security_audit_chain() -> dict[str, Any]:
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="operator platform security audit chain verification") as bypass_db,
    ):
        await acquire_platform_security_chain_lock(bypass_db)
        chain_rows = (
            (await bypass_db.execute(_CHAIN_SQL, {"action_pattern": PLATFORM_SECURITY_ACTION_PATTERN})).mappings().all()
        )
        legacy_rows = await load_legacy_platform_security_rows(bypass_db)
        return verify_platform_security_chain_rows(list(chain_rows), legacy_rows)
