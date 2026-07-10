"""Reportable reflection artifacts.

Reportable reflections are audit/source artifacts only. Canonical T2 is built
from sealed T0 segments by the Segment Package pipeline; this service must not
project reflections into legacy ``memory/learnings/*.md``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _message_digest(messages: list[dict[str, Any]]) -> str:
    snippets = []
    for msg in messages[-6:]:
        role = str(msg.get("role") or "unknown")
        content = str(msg.get("content") or "")[:300]
        snippets.append(f"{role}: {content}")
    return "\n".join(snippets)


def create_reportable_reflection(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    reason: str,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(data_root) / str(agent_id)
    now = datetime.now(timezone.utc)
    reflection_dir = root / "memory" / "reflections"
    reflection_dir.mkdir(parents=True, exist_ok=True)
    safe_reason = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in reason)[:80] or "reflection"
    report_path = reflection_dir / f"{now.strftime('%Y%m%d-%H%M%S')}-{safe_reason}.jsonl"
    payload = {
        "schema": "failure_reflection.v1",
        "created_at": now.isoformat(),
        "agent_id": str(agent_id),
        "session_id": session_id,
        "reason": reason,
        "trace_ref": (metadata or {}).get("trace_ref"),
        "decision": f"session closed with reportable reason: {reason}",
        "evidence": _message_digest(messages),
        "outcome": (metadata or {}).get("outcome", "requires_review"),
        "root_cause": (metadata or {}).get("root_cause", "not yet classified"),
        "next_policy": (metadata or {}).get("next_policy", f"Review {reason} before repeating the same pattern"),
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    return {
        "report_path": str(report_path),
        # Back-compat signal for callers that used to inspect the old projection
        # count. New canonical T2 packages are created from sealed T0 segments,
        # not from this reflection artifact.
        "t2_projected": 0,
        "canonical_t2_projected": False,
        "projection_status": "reflection_artifact_only",
        "trace_ref": str(payload.get("trace_ref") or f"reflection:{report_path.name}"),
    }
