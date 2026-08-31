"""Output artifacts for autonomous trigger/job attempts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
import uuid


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe[:120] or "trigger-output"


def trigger_output_artifact_ref(runtime_task_id: str | None) -> dict[str, str] | None:
    if not runtime_task_id:
        return None
    try:
        canonical_task_id = uuid.UUID(str(runtime_task_id)).hex
    except (TypeError, ValueError, AttributeError):
        canonical_task_id = str(runtime_task_id)
    return {
        "path": f"runtime_artifacts/triggers/{_safe_name(canonical_task_id)}.json",
        "schema": "trigger_output_artifact.v1",
    }


def _trigger_value(trigger: Any, key: str, default: Any = "") -> Any:
    if isinstance(trigger, Mapping):
        return trigger.get(key, default)
    return getattr(trigger, key, default)


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
    artifact = trigger_output_artifact_ref(runtime_task_id)
    if artifact is None:
        return None
    try:
        canonical_task_id = uuid.UUID(str(runtime_task_id)).hex
    except (TypeError, ValueError, AttributeError):
        canonical_task_id = str(runtime_task_id)
    agent_root = Path(agent_data_dir) / str(agent_id)
    path = agent_root / artifact["path"]
    artifact_dir = path.parent
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "trigger_output_artifact.v1",
        "runtime_task_id": canonical_task_id,
        "agent_id": str(agent_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "triggers": [
            {
                "id": str(_trigger_value(trigger, "id")),
                "name": str(_trigger_value(trigger, "name")),
                "type": str(_trigger_value(trigger, "type")),
                "trigger_class": str((_trigger_value(trigger, "config", {}) or {}).get("trigger_class") or ""),
            }
            for trigger in triggers
        ],
        "metadata": metadata or {},
        "final_reply": final_reply,
    }
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    comparable = {key: value for key, value in payload.items() if key != "created_at"}
    if (
        isinstance(existing, dict)
        and {key: value for key, value in existing.items() if key != "created_at"} == comparable
    ):
        return artifact

    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=artifact_dir,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            Path(temporary_path).unlink(missing_ok=True)
    return artifact
