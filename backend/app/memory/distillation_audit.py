"""Distillation audit sidecar — memory/distillation_audit.jsonl.

Spec §3: structured sidecars explain and index Markdown truth. Every curator
decision that does NOT result in a durable write (held candidates, rejected
patches) must still leave an auditable record here, so "no write happened"
is an observable decision rather than silence.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def write_distillation_audit(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    stage: str,
    outcome: str,
    reason: str,
    detail: dict | None = None,
) -> None:
    """Append one audit record. Best-effort: audit failure never breaks curation."""
    try:
        mem_dir = Path(data_root) / str(agent_id) / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, object] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "outcome": outcome,
            "reason": reason,
        }
        if detail:
            record["detail"] = detail
        with (mem_dir / "distillation_audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("[DistillationAudit] failed to write audit for %s: %s", agent_id, exc)
