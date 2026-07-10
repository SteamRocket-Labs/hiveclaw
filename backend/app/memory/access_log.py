"""Access-count writeback for retriever hits (D1: telemetry → sidecar).

Every prompt-included memory entry accumulates evidence of its own relevance:
`access_count` and `last_accessed` feed the retention/heat formula (§6.3). Those
two fields are **pure telemetry** and belong in the lifecycle sidecar
(`lifecycle.json`), not in the markdown prose. Up to purity-debt D1 this module
stamped them back into the `.md` line on every read — the canonical "telemetry
drifting prose" pollution — rewriting the file on every prompt activation. It
now bumps the sidecar record and leaves the prose byte-for-byte untouched.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from app.memory.lifecycle_store import bump_access_telemetry


def bump_access(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    file_relpath: str,
    entry_id: str,
    now: datetime | None = None,
    create_if_missing: bool = False,
) -> bool:
    """Increment access telemetry (count / last_accessed / K-ring) in the sidecar.

    Returns True when a sidecar record exists for `entry_id` and was bumped;
    False when no sidecar record exists (orphan prose line, or sidecar absent)
    unless ``create_if_missing`` seeds a telemetry-only record (knowledge and
    milestone pages have no authored lifecycle record but still accumulate
    BaseLevel frequency). `file_relpath` is retained for caller/audit
    compatibility but is no longer used to locate the entry — the sidecar is
    keyed by `entry_id`.
    """

    del file_relpath  # join key is entry_id; prose path no longer touched
    return bump_access_telemetry(data_root, agent_id, entry_id=entry_id, now=now, create_if_missing=create_if_missing)
