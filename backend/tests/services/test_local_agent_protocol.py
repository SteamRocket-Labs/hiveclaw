from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.services.local_agent_protocol import (
    build_execution_receipt,
    build_signed_capability_snapshot,
    effective_local_capabilities,
    verify_capability_snapshot,
)


def test_effective_local_capabilities_are_strict_three_way_intersection() -> None:
    assert effective_local_capabilities(
        server_capabilities={"execute", "file_upload", "file_download"},
        agent_capabilities={"execute", "file_download"},
        reported_capabilities={"execute", "file_upload"},
    ) == ("execute",)


def test_capability_snapshot_is_signed_scoped_and_expires() -> None:
    issued_at = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    snapshot = build_signed_capability_snapshot(
        signing_secret="test-signing-secret",
        issuer="hive-control-plane",
        subject_agent_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        tenant_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        scopes=("execute", "file_download"),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=5),
        version=3,
    )

    assert snapshot["schema"] == "hive.local_capability_snapshot.v1"
    assert snapshot["version"] == 3
    assert snapshot["snapshot_hash"]
    assert snapshot["signature"]
    assert verify_capability_snapshot(
        snapshot,
        signing_secret="test-signing-secret",
        now=issued_at + timedelta(minutes=1),
    )
    assert not verify_capability_snapshot(
        {**snapshot, "scopes": ["execute", "file_upload"]},
        signing_secret="test-signing-secret",
        now=issued_at + timedelta(minutes=1),
    )
    assert not verify_capability_snapshot(
        snapshot,
        signing_secret="test-signing-secret",
        now=issued_at + timedelta(minutes=6),
    )


def test_execution_receipt_has_stable_replay_and_result_refs() -> None:
    receipt = build_execution_receipt(
        request_hash="a" * 64,
        capability_snapshot_hash="b" * 64,
        result_refs=["artifact://one", "artifact://one", "artifact://two"],
        status="succeeded",
        replay_key="local-agent:task-1",
        trace_id="local-agent:session-1",
        span_id="remote-action:message-1",
    )

    assert receipt == {
        "schema": "hive.execution_receipt.v1",
        "request_hash": "a" * 64,
        "capability_snapshot_hash": "b" * 64,
        "result_refs": ["artifact://one", "artifact://two"],
        "status": "succeeded",
        "replay_key": "local-agent:task-1",
        "trace_id": "local-agent:session-1",
        "span_id": "remote-action:message-1",
    }
