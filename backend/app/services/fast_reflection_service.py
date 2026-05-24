"""Fast, non-durable reflection candidates for next-turn learning."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.evolution_ledger import record_evolution_candidate


_CORRECTION_MARKERS = (
    "错了",
    "不是",
    "不对",
    "下次",
    "以后",
    "不要",
    "别再",
    "wrong",
    "incorrect",
    "next time",
    "from now on",
    "do not",
    "don't",
    "instead",
)
_WORKFLOW_MARKERS = ("workflow", "流程", "步骤", "procedure", "sop")
_TOOL_FAILURE_MARKERS = ("tool failed", "工具失败", "traceback", "exception", "pytest", "ruff", "failed")


def _latest_user_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role") or "").lower() == "user":
            return str(message.get("content") or "").strip()
    return ""


def _message_digest(messages: list[dict[str, Any]]) -> str:
    snippets: list[str] = []
    for message in messages[-6:]:
        role = str(message.get("role") or "unknown")
        content = str(message.get("content") or "").strip().replace("\n", " ")[:400]
        if content:
            snippets.append(f"{role}: {content}")
    return "\n".join(snippets)


def _classify_signal(messages: list[dict[str, Any]], metadata: dict[str, Any]) -> tuple[str, str] | None:
    explicit = str(metadata.get("fast_reflection_signal") or metadata.get("user_correction") or "").strip()
    latest_user = _latest_user_content(messages)
    haystack = "\n".join([explicit, latest_user, str(metadata.get("error") or ""), str(metadata.get("test_artifacts") or "")])
    lowered = haystack.lower()

    if explicit:
        return "user_preference_correction", explicit[:1000]
    if any(marker in lowered for marker in _CORRECTION_MARKERS):
        signal = "workflow_correction" if any(marker in lowered for marker in _WORKFLOW_MARKERS) else "user_preference_correction"
        return signal, latest_user[:1000]
    if metadata.get("verification_failed") or any(marker in lowered for marker in _TOOL_FAILURE_MARKERS):
        return "verification_failure", haystack[:1000]
    if metadata.get("repeated_workflow_signature"):
        return "repeated_task_pattern", str(metadata["repeated_workflow_signature"])[:1000]
    return None


def create_fast_reflection_candidate(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a ledger-only candidate from strong session feedback.

    This is intentionally not a durable memory writer. P2 may project the
    candidate into the same session prompt; P3 decides whether anything durable
    can be promoted.
    """

    metadata = metadata or {}
    signal = _classify_signal(messages, metadata)
    if signal is None:
        return {"status": "skipped", "reason": "low_signal"}

    signal_type, lesson = signal
    workspace = Path(data_root) / str(agent_id)
    normalized_session_id = str(session_id or metadata.get("session_id") or "unknown-session")
    source_attempt_ids = [normalized_session_id]
    payload = {
        "schema": "fast_reflection_candidate.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent_id),
        "tenant_id": str(metadata.get("tenant_id") or ""),
        "user_id": str(metadata.get("user_id") or ""),
        "session_id": normalized_session_id,
        "signal_type": signal_type,
        "lesson": lesson,
        "message_digest": _message_digest(messages),
        "final_response": str(metadata.get("final_response") or "")[:1000],
        "promotion_state": "candidate",
    }
    candidate = record_evolution_candidate(
        workspace,
        target_type="fast_reflection",
        target_id=f"{normalized_session_id}:{signal_type}",
        diff=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        source_attempt_ids=source_attempt_ids,
        baseline_version="fast-reflection@candidate",
        metadata=payload,
    )
    return {
        "status": "candidate_created",
        "candidate_id": candidate["candidate_id"],
        "signal_type": signal_type,
        "manifest": candidate["manifest"],
    }
