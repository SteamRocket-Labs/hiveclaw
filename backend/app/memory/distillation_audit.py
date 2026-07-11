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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.agent_asset_transaction import AgentAssetTransaction

logger = logging.getLogger(__name__)


def write_distillation_audit(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    stage: str,
    outcome: str,
    reason: str,
    detail: dict | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> None:
    """Append one audit record. Best-effort: audit failure never breaks curation."""
    try:
        record: dict[str, object] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "outcome": outcome,
            "reason": reason,
        }
        if detail:
            record["detail"] = detail
        rendered = json.dumps(record, ensure_ascii=False) + "\n"
        if transaction is not None:
            transaction.append_text("memory/distillation_audit.jsonl", rendered)
        else:
            mem_dir = Path(data_root) / str(agent_id) / "memory"
            mem_dir.mkdir(parents=True, exist_ok=True)
            with (mem_dir / "distillation_audit.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(rendered)
    except OSError as exc:
        logger.warning("[DistillationAudit] failed to write audit for %s: %s", agent_id, exc)
