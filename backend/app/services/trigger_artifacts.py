"""Output artifacts for autonomous trigger/job attempts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe[:120] or "trigger-output"


def write_trigger_output_artifact(
    *,
    agent_data_dir: str | Path,
    agent_id: Any,
    runtime_task_id: str | None,
    triggers: list[Any],
    final_reply: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Persist a structured output artifact for a trigger execution.

    This is intentionally file-based for P5 so scheduled jobs get durable,
    inspectable output without a schema migration.
    """
    if not runtime_task_id:
        return None
    agent_root = Path(agent_data_dir) / str(agent_id)
    artifact_dir = agent_root / "runtime_artifacts" / "triggers"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_name(runtime_task_id) + ".json"
    path = artifact_dir / filename
    payload = {
        "schema": "trigger_output_artifact.v1",
        "runtime_task_id": runtime_task_id,
        "agent_id": str(agent_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "triggers": [
            {
                "id": str(getattr(trigger, "id", "")),
                "name": str(getattr(trigger, "name", "")),
                "type": str(getattr(trigger, "type", "")),
                "trigger_class": str((getattr(trigger, "config", None) or {}).get("trigger_class") or ""),
                "focus_ref": getattr(trigger, "focus_ref", None),
            }
            for trigger in triggers
        ],
        "metadata": metadata or {},
        "final_reply": final_reply,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "path": path.relative_to(agent_root).as_posix(),
        "schema": "trigger_output_artifact.v1",
    }
