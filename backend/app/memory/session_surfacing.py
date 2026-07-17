"""Durable per-Session budget for automatic memory body surfacing.

The budget applies only to bodies that the runtime prefetches into the prompt.
It never limits ``search_memory``/``load_memory`` or deletes evidence.  The
ledger stores byte counts and turn identities only, so no memory prose is
duplicated into a control sidecar.
"""

from __future__ import annotations

import fcntl
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

SESSION_AUTO_SURFACE_BUDGET_BYTES = 60 * 1024
SESSION_AUTO_SURFACE_MIN_USEFUL_BYTES = 256
SESSION_SURFACING_SCHEMA = "hive.memory.session_surfacing.v1"


@dataclass(frozen=True, slots=True)
class SessionSurfacingResult:
    content: str
    surfaced_bytes: int
    total_surfaced_bytes: int
    remaining_bytes: int
    exhausted: bool
    already_recorded: bool = False


def _safe_session_id(session_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(session_id))[:80] or "session"


def _ledger_path(data_root: Path, agent_id: uuid.UUID | str, session_id: str) -> Path:
    return (
        Path(data_root)
        / str(agent_id)
        / "memory"
        / "control"
        / "session_surfacing"
        / f"{_safe_session_id(session_id)}.json"
    )


def _load_ledger(path: Path) -> dict:
    if not path.exists():
        return {"schema": SESSION_SURFACING_SCHEMA, "surfaced_bytes": 0, "turns": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("memory_session_surfacing_ledger_unreadable") from exc
    if payload.get("schema") != SESSION_SURFACING_SCHEMA or not isinstance(payload.get("turns"), dict):
        raise RuntimeError("memory_session_surfacing_ledger_invalid")
    return payload


def _write_ledger(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def surface_with_session_budget(
    *,
    data_root: Path,
    agent_id: uuid.UUID | str,
    session_id: str | None,
    turn_id: str | None,
    render: Callable[[int], str],
    budget_bytes: int = SESSION_AUTO_SURFACE_BUDGET_BYTES,
) -> SessionSurfacingResult:
    """Render and account automatic memory bytes under one cross-process lock.

    ``render`` receives the exact remaining byte budget and must return UTF-8
    text no larger than that budget.  Replaying the same durable ``turn_id`` is
    idempotent and does not consume the Session budget again.
    """

    maximum = max(0, int(budget_bytes))
    if not session_id:
        content = render(maximum)
        used = len(content.encode("utf-8"))
        if used > maximum:
            raise RuntimeError("memory_auto_surface_renderer_exceeded_budget")
        return SessionSurfacingResult(
            content=content,
            surfaced_bytes=used,
            total_surfaced_bytes=used,
            remaining_bytes=max(0, maximum - used),
            exhausted=max(0, maximum - used) < SESSION_AUTO_SURFACE_MIN_USEFUL_BYTES,
        )

    path = _ledger_path(Path(data_root), agent_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    durable_turn_id = str(turn_id or f"unkeyed-{uuid.uuid4().hex}")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            ledger = _load_ledger(path)
            turns = ledger["turns"]
            total = max(0, int(ledger.get("surfaced_bytes") or 0))
            prior = turns.get(durable_turn_id)
            if isinstance(prior, dict):
                reserved = max(0, int(prior.get("bytes") or 0))
                content = render(reserved)
                used = len(content.encode("utf-8"))
                if used > reserved:
                    raise RuntimeError("memory_auto_surface_replay_exceeded_reservation")
                remaining = max(0, maximum - total)
                return SessionSurfacingResult(
                    content=content,
                    surfaced_bytes=reserved,
                    total_surfaced_bytes=total,
                    remaining_bytes=remaining,
                    exhausted=remaining < SESSION_AUTO_SURFACE_MIN_USEFUL_BYTES,
                    already_recorded=True,
                )

            remaining_before = max(0, maximum - total)
            content = render(remaining_before)
            used = len(content.encode("utf-8"))
            if used > remaining_before:
                raise RuntimeError("memory_auto_surface_renderer_exceeded_budget")
            if used:
                turns[durable_turn_id] = {
                    "bytes": used,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
                total += used
                ledger.update(
                    {
                        "schema": SESSION_SURFACING_SCHEMA,
                        "agent_id": str(agent_id),
                        "session_id": str(session_id),
                        "budget_bytes": maximum,
                        "surfaced_bytes": total,
                        "turns": turns,
                    }
                )
                _write_ledger(path, ledger)
            remaining_after = max(0, maximum - total)
            return SessionSurfacingResult(
                content=content,
                surfaced_bytes=used,
                total_surfaced_bytes=total,
                remaining_bytes=remaining_after,
                exhausted=remaining_after < SESSION_AUTO_SURFACE_MIN_USEFUL_BYTES,
            )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
