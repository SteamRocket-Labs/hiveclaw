"""Read-only projection of an agent's self-evolution activity.

Surfaces what the agent's evolution loop has been doing — skill lifecycle and
the candidate→eval→promotion audit chain — for display in the UI. Pure and
side-effect free: it only *reads* workspace files and never mutates agent
state. Missing or corrupt files degrade to an empty structure rather than
raising, matching Hive's fault-tolerant read endpoints.

Data sources (all inside an agent workspace):

- ``evolution/skill_usage.json`` — skill state sidecar maintained by ``skill_curator``.
- ``evolution/skill_review.md`` — skill lifecycle audit lines written by
  ``skill_lifecycle.record_skill_lifecycle_event``:
  ``- <iso> [<status>] <skill_name>: <note>``.
- ``evolution/evolution_ledger.jsonl`` — candidate/eval/promotion records from
  ``evolution_ledger`` (one JSON object per line).

The single public entry point ``build_evolution_view(workspace)`` returns a
JSON-serializable dict the API layer can hand straight to the client.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.services.skill_curator import (
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_STALE,
    load_skill_usage,
)

# Order skills by lifecycle relevance first, then by how heavily they are used.
_STATE_RANK = {STATE_ACTIVE: 0, STATE_STALE: 1, STATE_ARCHIVED: 2}

# ``- 2026-05-20T09:05:00+00:00 [promote] weekly-report: note text``
_REVIEW_LINE = re.compile(r"^-\s+(?P<at>\S+)\s+\[(?P<status>[^\]]+)\]\s+(?P<skill>[^:]+?)\s*:\s*(?P<note>.*)$")

# Ledger ``event`` value → public timeline ``kind``.
_LEDGER_EVENT_KINDS = {
    "candidate": "candidate",
    "memory_promotion_candidate": "candidate",
    "eval_run": "eval",
    "promotion_decision": "promotion",
    "memory_promotion_decision": "promotion",
    "rollback": "rollback",
}


def _empty_view() -> dict[str, Any]:
    return {
        "skill_summary": {"active": 0, "stale": 0, "archived": 0, "total": 0},
        "skills": [],
        "timeline": [],
    }


def _build_skills(workspace: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Project ``skill_usage.json`` into a skill list + per-state summary counts."""
    usage = load_skill_usage(workspace)
    summary = {STATE_ACTIVE: 0, STATE_STALE: 0, STATE_ARCHIVED: 0}
    skills: list[dict[str, Any]] = []

    for slug, record in usage.items():
        if not isinstance(record, dict):
            continue
        state = str(record.get("state") or STATE_ACTIVE)
        if state in summary:
            summary[state] += 1
        skills.append(
            {
                "slug": slug,
                "state": state,
                "use_count": int(record.get("use_count") or 0),
                "last_used_at": record.get("last_used_at"),
                "pinned": bool(record.get("pinned")),
            }
        )

    skills.sort(key=lambda s: (_STATE_RANK.get(s["state"], 99), -s["use_count"], s["slug"]))
    return skills, summary


def _parse_review_timeline(workspace: Path) -> list[dict[str, str]]:
    """Parse skill lifecycle audit lines from ``skill_review.md``."""
    path = workspace / "evolution" / "skill_review.md"
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []

    events: list[dict[str, str]] = []
    for line in raw.splitlines():
        match = _REVIEW_LINE.match(line.strip())
        if not match:
            continue
        status = match.group("status").strip()
        skill = match.group("skill").strip()
        note = match.group("note").strip()
        events.append(
            {
                "at": match.group("at").strip(),
                "kind": status,
                "title": f"{skill} — {status}",
                "detail": note,
            }
        )
    return events


def _parse_ledger_timeline(workspace: Path) -> list[dict[str, str]]:
    """Parse candidate/eval/promotion records from ``evolution_ledger.jsonl``."""
    path = workspace / "evolution" / "evolution_ledger.jsonl"
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []

    events: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue

        kind = _LEDGER_EVENT_KINDS.get(str(payload.get("event")))
        if kind is None:
            continue
        at = str(payload.get("created_at") or "")
        if not at:
            continue

        target = str(payload.get("target_id") or payload.get("candidate_id") or "").strip()
        title = f"{kind}: {target}" if target else kind
        events.append(
            {
                "at": at,
                "kind": kind,
                "title": title,
                "detail": _ledger_detail(kind, payload),
            }
        )
    return events


def _ledger_detail(kind: str, payload: dict[str, Any]) -> str:
    """Human-readable one-liner for a ledger event."""
    if kind == "eval":
        reward = payload.get("reward")
        baseline = payload.get("baseline_reward")
        passed = payload.get("passed")
        return f"reward {reward} vs baseline {baseline} (passed={passed})"
    if kind == "promotion":
        decision = payload.get("decision") or ""
        reason = payload.get("reason") or ""
        return f"{decision}: {reason}".strip(": ").strip()
    if kind == "rollback":
        return str(payload.get("reason") or "")
    return str(payload.get("target_type") or "")


def build_evolution_view(workspace: Path) -> dict[str, Any]:
    """Assemble the read-only evolution view for one agent workspace.

    Returns ``{"skill_summary", "skills", "timeline"}``. Never mutates the
    workspace; missing/corrupt files yield empty sub-structures.
    """
    if not workspace.exists():
        return _empty_view()

    skills, state_counts = _build_skills(workspace)
    timeline = _parse_review_timeline(workspace) + _parse_ledger_timeline(workspace)
    timeline.sort(key=lambda item: item["at"], reverse=True)

    return {
        "skill_summary": {
            "active": state_counts[STATE_ACTIVE],
            "stale": state_counts[STATE_STALE],
            "archived": state_counts[STATE_ARCHIVED],
            "total": len(skills),
        },
        "skills": skills,
        "timeline": timeline,
    }


def render_skill_evolution_digest(workspace: Path, *, max_skills: int = 8) -> str:
    """Render a compact digest of the agent's own skill evolution for the prompt.

    Closes the self-evolution loop on the cognition side: memory already feeds
    back into the prompt, but skill evolution (usage, curator decay) did not. The
    digest lets the agent see its skill assets, reuse what works, and rescue idle
    skills before the curator archives them — making skills a first-class
    evolution axis alongside memory.

    Injected into the *dynamic* suffix only (it changes as skills evolve), so it
    never invalidates the frozen system-prompt cache. Read-only and cheap enough
    to run every round; returns "" when the agent has no tracked skills.
    """
    view = build_evolution_view(workspace)
    summary = view["skill_summary"]
    if summary["total"] == 0:
        return ""

    skills = view["skills"]
    lines = [
        "## Your Skill Assets",
        f"You have {summary['total']} tracked skills — "
        f"{summary['active']} active, {summary['stale']} idle, {summary['archived']} archived.",
    ]

    active = [s for s in skills if s["state"] == STATE_ACTIVE][:max_skills]
    if active:
        names = ", ".join(f"{s['slug']} ({s['use_count']}×)" for s in active)
        lines.append(f"Active skills you can reuse: {names}.")

    stale = [s for s in skills if s["state"] == STATE_STALE and not s["pinned"]][:max_skills]
    if stale:
        names = ", ".join(s["slug"] for s in stale)
        lines.append(
            f"Idle and nearing auto-archival: {names}. Reuse them if still useful, or call `pin_skill` to keep them."
        )

    return "\n".join(lines)
