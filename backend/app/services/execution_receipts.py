"""Provider-neutral execution receipt contracts for remote actions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def canonical_payload_hash(payload: Any) -> str:
    """Hash a JSON-compatible payload using one stable, provider-neutral encoding."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_execution_receipt(
    *,
    request_hash: str,
    capability_snapshot_hash: str,
    result_refs: Iterable[str],
    status: str,
    replay_key: str,
    trace_id: str,
    span_id: str,
) -> dict[str, Any]:
    """Build the shared receipt shape consumed by local and cloud A2A runtimes."""

    return {
        "schema": "hive.execution_receipt.v1",
        "request_hash": str(request_hash),
        "capability_snapshot_hash": str(capability_snapshot_hash),
        "result_refs": list(dict.fromkeys(str(ref) for ref in result_refs if str(ref))),
        "status": str(status),
        "replay_key": str(replay_key),
        "trace_id": str(trace_id),
        "span_id": str(span_id),
    }
