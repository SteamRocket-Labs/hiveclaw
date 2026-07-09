"""Session working set W_t — the incremental conversational activation state.

Dynamic-memory-activation design §4.2: the KV-cache idea worth borrowing from
transformers is not matrix attention but *stateful, incrementally-evolving
session context*. W_t tracks which memories/pages the session has already
activated, with per-item strength that refreshes on re-activation and decays
each turn. Recall then seeds graph diffusion (PPR) from the strongest members,
so the same query recalls differently in different conversational contexts.

ACL hard boundary (design §4.2): W_t is persistable, restart-recoverable
session state, therefore it stores POINTERS AND STATISTICS ONLY — ``ref``,
``strength``, ``last_turn``, ``ts``. Memory bodies, sensitive tool payloads,
and KB document content are forbidden here; consumers re-resolve refs through
the normal sensitivity/ACL gates at recall time. Exporting or restoring this
file can leak at most "which ids were active", never confidential prose.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

WORKING_SET_SCHEMA = "hive.memory.session_working_set.v1"
# Per-turn multiplicative decay: an untouched item fades below the eviction
# floor after ~10 turns — roughly one topic switch.
WORKING_SET_DECAY = 0.8
WORKING_SET_MIN_STRENGTH = 0.1
WORKING_SET_MAX_ITEMS = 32
# Seeds handed to PPR per recall (strongest members carry the diffusion).
WORKING_SET_SEED_TOP_N = 8


@dataclass(slots=True)
class SessionWorkingSet:
    turn_index: int = 0
    items: list[dict] = field(default_factory=list)

    def as_pairs(self) -> tuple[tuple[str, float], ...]:
        return tuple((str(item["ref"]), float(item["strength"])) for item in self.items)


def advance_working_set(
    existing: SessionWorkingSet | None,
    activated_refs: list[str] | tuple[str, ...],
    *,
    now: datetime | None = None,
) -> SessionWorkingSet:
    """One turn tick: decay every member, refresh/insert this turn's refs.

    Pinned attention-set seeds (M7, design §4.4) are exempt from decay and
    eviction — they hold at full strength until the goal reaches a terminal
    state or a newer declaration supersedes them.
    """
    when = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    turn_index = (existing.turn_index if existing else 0) + 1
    merged: dict[str, dict] = {}
    for item in existing.items if existing else []:
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        if item.get("pinned"):
            merged[ref] = {
                "ref": ref,
                "strength": 1.0,
                "last_turn": int(item.get("last_turn") or 0),
                "ts": str(item.get("ts") or when),
                "pinned": True,
            }
            continue
        strength = float(item.get("strength") or 0.0) * WORKING_SET_DECAY
        if strength < WORKING_SET_MIN_STRENGTH:
            continue
        merged[ref] = {
            "ref": ref,
            "strength": round(strength, 4),
            "last_turn": int(item.get("last_turn") or 0),
            "ts": str(item.get("ts") or when),
        }
    for raw_ref in activated_refs:
        ref = str(raw_ref or "").strip()
        if not ref:
            continue
        pinned = bool(merged.get(ref, {}).get("pinned"))
        entry = {"ref": ref, "strength": 1.0, "last_turn": turn_index, "ts": when}
        if pinned:
            entry["pinned"] = True
        merged[ref] = entry
    items = sorted(merged.values(), key=lambda item: (-item["strength"], item["ref"]))[:WORKING_SET_MAX_ITEMS]
    return SessionWorkingSet(turn_index=turn_index, items=items)


def pin_attention_set(
    existing: SessionWorkingSet | None,
    declared_refs: list[str] | tuple[str, ...],
    *,
    now: datetime | None = None,
) -> SessionWorkingSet:
    """Pin an agent-declared attention set as persistent W_t seeds (M7).

    The declaration comes from the model itself (goal_start/update_goal tool
    arguments) — the intelligent judgment stays with the LLM, the platform
    only records pointers. A new declaration supersedes the previous one.
    """
    when = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    base = existing or SessionWorkingSet()
    kept = [dict(item) for item in base.items if not item.get("pinned")]
    merged: dict[str, dict] = {str(item["ref"]): item for item in kept}
    for raw_ref in declared_refs:
        ref = str(raw_ref or "").strip()
        if not ref:
            continue
        merged[ref] = {"ref": ref, "strength": 1.0, "last_turn": base.turn_index, "ts": when, "pinned": True}
    items = sorted(merged.values(), key=lambda item: (-item["strength"], item["ref"]))[:WORKING_SET_MAX_ITEMS]
    return SessionWorkingSet(turn_index=base.turn_index, items=items)


def clear_pinned_items(existing: SessionWorkingSet) -> SessionWorkingSet:
    """Drop pinned seeds (goal reached a terminal state)."""
    items = [dict(item) for item in existing.items if not item.get("pinned")]
    return SessionWorkingSet(turn_index=existing.turn_index, items=items)


def working_set_seeds(working_set: SessionWorkingSet, *, top_n: int = WORKING_SET_SEED_TOP_N) -> dict[str, float]:
    return {str(item["ref"]): float(item["strength"]) for item in working_set.items[: max(1, top_n)]}


def touch_working_set(
    existing: SessionWorkingSet,
    refs: list[str] | tuple[str, ...],
    *,
    now: datetime | None = None,
) -> SessionWorkingSet:
    """Refresh/insert refs WITHOUT a turn tick (no decay, no turn advance).

    Used by same-turn secondary activation surfaces (e.g. the personal-KB
    hint) so one conversational turn causes exactly one decay step regardless
    of how many activation surfaces fire within it.
    """
    when = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    merged: dict[str, dict] = {str(item["ref"]): dict(item) for item in existing.items}
    for raw_ref in refs:
        ref = str(raw_ref or "").strip()
        if not ref:
            continue
        merged[ref] = {"ref": ref, "strength": 1.0, "last_turn": existing.turn_index, "ts": when}
    items = sorted(merged.values(), key=lambda item: (-item["strength"], item["ref"]))[:WORKING_SET_MAX_ITEMS]
    return SessionWorkingSet(turn_index=existing.turn_index, items=items)


def _working_set_path(data_root: Path, agent_id: uuid.UUID | str, session_id: str) -> Path:
    safe_session = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(session_id))[:80] or "session"
    return Path(data_root) / str(agent_id) / "memory" / "control" / "working_sets" / f"{safe_session}.json"


def load_working_set(data_root: Path, agent_id: uuid.UUID | str, session_id: str) -> SessionWorkingSet:
    path = _working_set_path(data_root, agent_id, session_id)
    if not path.exists():
        return SessionWorkingSet()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[working_set] unreadable state for session %s: %s", session_id, exc)
        return SessionWorkingSet()
    items = []
    for item in payload.get("items") or []:
        if not str(item.get("ref") or "").strip():
            continue
        entry = {
            "ref": str(item.get("ref") or ""),
            "strength": float(item.get("strength") or 0.0),
            "last_turn": int(item.get("last_turn") or 0),
            "ts": str(item.get("ts") or ""),
        }
        if item.get("pinned"):
            entry["pinned"] = True
        items.append(entry)
    return SessionWorkingSet(turn_index=int(payload.get("turn_index") or 0), items=items)


def save_working_set(
    data_root: Path,
    agent_id: uuid.UUID | str,
    session_id: str,
    working_set: SessionWorkingSet,
) -> None:
    path = _working_set_path(data_root, agent_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized_items = []
    for item in working_set.items:
        entry = {
            "ref": item["ref"],
            "strength": item["strength"],
            "last_turn": item["last_turn"],
            "ts": item["ts"],
        }
        if item.get("pinned"):
            entry["pinned"] = True
        serialized_items.append(entry)
    payload = {
        "schema": WORKING_SET_SCHEMA,
        "session_id": str(session_id),
        "turn_index": working_set.turn_index,
        "items": serialized_items,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
