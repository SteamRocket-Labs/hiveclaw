from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4


def _legacy_row(*, created_at: datetime) -> dict:
    return {
        "id": uuid4(),
        "action": "platform_security.auth.login_failed",
        "details": {
            "schema_version": "hive.platform_security_audit.v1",
            "event_type": "auth.login_failed",
            "actor": {"type": "user", "id": str(uuid4())},
        },
        "created_at": created_at,
    }


def test_platform_security_chain_covers_legacy_anchor_and_v2_events() -> None:
    from app.services.platform_security_audit import (
        compute_legacy_platform_audit_anchor,
        seal_platform_security_envelope,
        verify_platform_security_chain_rows,
    )

    recorded_at = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    legacy_rows = [_legacy_row(created_at=recorded_at - timedelta(minutes=1))]
    legacy_anchor = compute_legacy_platform_audit_anchor(legacy_rows)

    cutover_id = uuid4()
    cutover_action = "platform_security.chain_cutover"
    cutover = seal_platform_security_envelope(
        event_id=cutover_id,
        row_action=cutover_action,
        base_envelope={
            "event_type": "chain_cutover",
            "severity": "info",
            "actor": {"type": "system", "id": None},
            "action": "chain_cutover",
            "resource": {"type": "platform_security_audit", "id": None},
            "details": legacy_anchor,
            "legacy_anchor": legacy_anchor,
            "ip_address": None,
            "request_id": None,
            "execution_identity": None,
        },
        sequence_num=1,
        prev_hash="genesis",
        recorded_at=recorded_at,
    )
    event_id = uuid4()
    event_action = "platform_security.tenant_impersonation"
    event = seal_platform_security_envelope(
        event_id=event_id,
        row_action=event_action,
        base_envelope={
            "event_type": "tenant_impersonation",
            "severity": "warn",
            "actor": {"type": "user", "id": str(uuid4())},
            "action": "tenant_impersonation",
            "resource": {"type": "tenant", "id": str(uuid4())},
            "details": {"request_path": "/api/agents"},
            "legacy_anchor": legacy_anchor,
            "ip_address": "192.0.2.10",
            "request_id": "trace-1",
            "execution_identity": None,
        },
        sequence_num=2,
        prev_hash=cutover["event_hash"],
        recorded_at=recorded_at + timedelta(seconds=1),
    )

    verification = verify_platform_security_chain_rows(
        [
            {"id": cutover_id, "action": cutover_action, "details": cutover, "created_at": recorded_at},
            {
                "id": event_id,
                "action": event_action,
                "details": event,
                "created_at": recorded_at + timedelta(seconds=1),
            },
        ],
        legacy_rows,
    )

    assert verification == {
        "valid": True,
        "chain_version": "hive.platform_security_audit.v2",
        "total_events": 2,
        "legacy_event_count": 1,
        "head_hash": event["event_hash"],
        "first_invalid_event_id": None,
        "reason": None,
    }


def test_platform_security_chain_verifier_detects_envelope_tampering() -> None:
    from app.services.platform_security_audit import (
        compute_legacy_platform_audit_anchor,
        seal_platform_security_envelope,
        verify_platform_security_chain_rows,
    )

    recorded_at = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    anchor = compute_legacy_platform_audit_anchor([])
    event_id = uuid4()
    envelope = seal_platform_security_envelope(
        event_id=event_id,
        row_action="platform_security.chain_cutover",
        base_envelope={
            "event_type": "chain_cutover",
            "severity": "info",
            "actor": {"type": "system", "id": None},
            "action": "chain_cutover",
            "resource": {"type": "platform_security_audit", "id": None},
            "details": anchor,
            "legacy_anchor": anchor,
            "ip_address": None,
            "request_id": None,
            "execution_identity": None,
        },
        sequence_num=1,
        prev_hash="genesis",
        recorded_at=recorded_at,
    )
    tampered = deepcopy(envelope)
    tampered["details"]["legacy_event_count"] = 99

    verification = verify_platform_security_chain_rows(
        [
            {
                "id": event_id,
                "action": "platform_security.chain_cutover",
                "details": tampered,
                "created_at": recorded_at,
            }
        ],
        [],
    )

    assert verification["valid"] is False
    assert verification["first_invalid_event_id"] == str(event_id)
    assert verification["reason"] == "event_hash_mismatch"


def test_platform_security_chain_recovers_late_legacy_rows_after_rolling_cutover() -> None:
    from app.services.platform_security_audit import (
        compute_legacy_platform_audit_anchor,
        seal_platform_security_envelope,
        verify_platform_security_chain_rows,
    )

    cutover_at = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    empty_anchor = compute_legacy_platform_audit_anchor([])
    cutover_id = uuid4()
    cutover = seal_platform_security_envelope(
        event_id=cutover_id,
        row_action="platform_security.chain_cutover",
        base_envelope={
            "event_type": "chain_cutover",
            "severity": "info",
            "actor": {"type": "system", "id": None},
            "action": "chain_cutover",
            "resource": {"type": "platform_security_audit", "id": None},
            "details": empty_anchor,
            "legacy_anchor": empty_anchor,
            "ip_address": None,
            "request_id": None,
            "execution_identity": None,
        },
        sequence_num=1,
        prev_hash="genesis",
        recorded_at=cutover_at,
    )

    late_legacy_rows = [_legacy_row(created_at=cutover_at + timedelta(milliseconds=500))]
    recovered_anchor = compute_legacy_platform_audit_anchor(late_legacy_rows)
    recovered_id = uuid4()
    recovered_at = cutover_at + timedelta(seconds=1)
    recovered = seal_platform_security_envelope(
        event_id=recovered_id,
        row_action="platform_security.tenant_impersonation",
        base_envelope={
            "event_type": "tenant_impersonation",
            "severity": "warn",
            "actor": {"type": "user", "id": str(uuid4())},
            "action": "tenant_impersonation",
            "resource": {"type": "tenant", "id": str(uuid4())},
            "details": {},
            "legacy_anchor": recovered_anchor,
            "ip_address": None,
            "request_id": "trace-rolling-cutover",
            "execution_identity": None,
        },
        sequence_num=2,
        prev_hash=cutover["event_hash"],
        recorded_at=recovered_at,
    )

    verification = verify_platform_security_chain_rows(
        [
            {
                "id": cutover_id,
                "action": "platform_security.chain_cutover",
                "details": cutover,
                "created_at": cutover_at,
            },
            {
                "id": recovered_id,
                "action": "platform_security.tenant_impersonation",
                "details": recovered,
                "created_at": recovered_at,
            },
        ],
        late_legacy_rows,
    )

    assert verification["valid"] is True
    assert verification["legacy_event_count"] == 1


def test_platform_security_chain_verifier_reports_malformed_sequence_without_crashing() -> None:
    from app.services.platform_security_audit import verify_platform_security_chain_rows

    event_id = uuid4()
    verification = verify_platform_security_chain_rows(
        [
            {
                "id": event_id,
                "action": "platform_security.auth.login_failed",
                "details": {
                    "schema_version": "hive.platform_security_audit.v2",
                    "sequence_num": "not-a-number",
                },
                "created_at": datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc),
            }
        ],
        [],
    )

    assert verification["valid"] is False
    assert verification["first_invalid_event_id"] == str(event_id)
    assert verification["reason"] == "invalid_sequence"
