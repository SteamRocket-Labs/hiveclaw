"""Session-visible learning projections derived from fast reflection candidates."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.agent_asset_transaction import AgentAssetTransaction


SESSION_LEARNING_SCHEMA = "session_learning_projection.v1"
_ACTIVE_STATES = {"candidate", "held", "promoted"}
_INACTIVE_STATES = {"rejected", "expired"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _workspace(data_root: Path, agent_id: uuid.UUID) -> Path:
    return Path(data_root) / str(agent_id)


def _projection_path(data_root: Path, agent_id: uuid.UUID) -> Path:
    path = _workspace(data_root, agent_id) / "evolution" / "session_learning_projections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_projection_event(
    data_root: Path,
    agent_id: uuid.UUID,
    payload: dict[str, Any],
    *,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    if transaction is not None:
        transaction.append_text(
            "evolution/session_learning_projections.jsonl",
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        )
        return payload
    path = _projection_path(data_root, agent_id)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


def record_session_learning_projection(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    candidate_id: str,
    lesson: str,
    source_refs: list[str],
    evidence: str,
    now: datetime | None = None,
    ttl_minutes: int = 60,
    promotion_state: str = "candidate",
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    created_at = now or _now()
    expires_at = created_at + timedelta(minutes=max(ttl_minutes, 1))
    payload = {
        "schema": SESSION_LEARNING_SCHEMA,
        "event": "session_learning_projection",
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "candidate_id": str(candidate_id),
        "lesson": str(lesson).strip(),
        "source_refs": [str(ref).strip() for ref in source_refs if str(ref).strip()],
        "evidence": str(evidence or "inferred").strip().lower(),
        "promotion_state": str(promotion_state or "candidate").strip().lower(),
    }
    return _append_projection_event(data_root, agent_id, payload, transaction=transaction)


def update_session_learning_projection_state(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    candidate_id: str,
    promotion_state: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    updated_at = now or _now()
    payload = {
        "schema": SESSION_LEARNING_SCHEMA,
        "event": "session_learning_projection_state",
        "updated_at": updated_at.isoformat(),
        "agent_id": str(agent_id),
        "candidate_id": str(candidate_id),
        "promotion_state": str(promotion_state or "candidate").strip().lower(),
    }
    return _append_projection_event(data_root, agent_id, payload)


def _load_projection_events(data_root: Path, agent_id: uuid.UUID) -> list[dict[str, Any]]:
    path = _projection_path(data_root, agent_id)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("schema") == SESSION_LEARNING_SCHEMA:
            events.append(payload)
    return events


def load_session_learning_projections(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current_time = now or _now()
    projections: dict[str, dict[str, Any]] = {}
    for event in _load_projection_events(data_root, agent_id):
        candidate_id = str(event.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        if event.get("event") == "session_learning_projection":
            if str(event.get("session_id") or "") == str(session_id):
                projections[candidate_id] = dict(event)
            continue
        if event.get("event") == "session_learning_projection_state" and candidate_id in projections:
            projections[candidate_id]["promotion_state"] = str(event.get("promotion_state") or "").strip().lower()

    active: list[dict[str, Any]] = []
    for projection in projections.values():
        state = str(projection.get("promotion_state") or "candidate").lower()
        expires_at = _parse_dt(str(projection.get("expires_at") or ""))
        if state in _INACTIVE_STATES:
            continue
        if state not in _ACTIVE_STATES:
            continue
        if expires_at is not None and expires_at <= current_time:
            continue
        active.append(projection)
    return sorted(active, key=lambda item: str(item.get("created_at") or ""))


def render_session_learning_projection(projections: list[dict[str, Any]]) -> str:
    if not projections:
        return ""
    lines = [
        "## Session Learning",
        "These are short-lived lessons from this session. Use them now, but do not treat them as durable memory.",
    ]
    for projection in projections:
        refs = ", ".join(projection.get("source_refs") or [])
        lines.append(
            "- "
            f"[{projection.get('promotion_state')}][{projection.get('evidence')}] "
            f"{projection.get('lesson')} "
            f"(candidate={projection.get('candidate_id')}; refs={refs})"
        )
    return "\n".join(lines)


def render_active_session_learning_projection(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    now: datetime | None = None,
) -> str:
    return render_session_learning_projection(
        load_session_learning_projections(
            data_root=data_root,
            agent_id=agent_id,
            session_id=session_id,
            now=now,
        )
    )
