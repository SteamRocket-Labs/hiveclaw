"""Skill curator — anti-entropy cleanup loop for agent-authored skills.

Hive's skill evolution is otherwise one-directional: ``skill_lifecycle`` and
``skill_distiller`` only ever promote/patch/create skills. Without a negative
pressure the skill catalog grows unbounded and dilutes the model's attention
when injected into the prompt. This module is the missing decay axis.

Design (mirrors hermes-agent's curator, adapted to Hive's per-workspace MD
layout):

- **Sidecar telemetry**, not frontmatter. Usage counters live in
  ``workspace/skills/.usage.json`` keyed by skill *slug* (the directory name
  under ``skills/``). Keeps operational state out of user-authored SKILL.md.
- **Pure state machine.** ``apply_skill_auto_transitions`` is a pure function
  (usage dict in → transition list out) with no IO, so it is unit-testable
  without mocks. The orchestrator owns all file mutation.
- **Never delete.** The harshest action is ``archive`` — the skill directory is
  moved to ``skills/.archive/<slug>`` and can always be restored by hand.
- **Provenance-gated.** Only ``created_by == "agent"`` skills are curated.
  Bundled / template skills are upstream-owned and never touched.
- **Pinned opt-out.** A pinned skill is exempt from every automatic transition.

Lifecycle states:
    active   -> default
    stale    -> not used for > _STALE_AFTER_DAYS, still on disk
    archived -> not used for > _ARCHIVE_AFTER_DAYS, moved to .archive/
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"

_AGENT_PROVENANCE = "agent"
# Provenance for a skill observed only via use (load) but never created through
# save_skill — e.g. a bundled/template/default skill. The curator acts solely on
# _AGENT_PROVENANCE, so this sentinel keeps default skills out of stale/archive.
_UNKNOWN_PROVENANCE = "unknown"

_STALE_AFTER_DAYS = 30
_ARCHIVE_AFTER_DAYS = 90

_USAGE_FILENAME = ".usage.json"
_ARCHIVE_DIRNAME = ".archive"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _skills_dir(workspace: Path) -> Path:
    return workspace / "skills"


def _usage_path(workspace: Path) -> Path:
    return _skills_dir(workspace) / _USAGE_FILENAME


def _archive_dir(workspace: Path) -> Path:
    return _skills_dir(workspace) / _ARCHIVE_DIRNAME


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _empty_record(*, created_by: str = _UNKNOWN_PROVENANCE, now: datetime | None = None) -> dict[str, Any]:
    return {
        "created_by": created_by,
        "created_at": _now_iso(now),
        "last_used_at": None,
        "use_count": 0,
        "view_count": 0,
        "state": STATE_ACTIVE,
        "pinned": False,
        "archived_at": None,
    }


# ---------------------------------------------------------------------------
# Sidecar I/O
# ---------------------------------------------------------------------------


def load_skill_usage(workspace: Path) -> dict[str, dict[str, Any]]:
    """Read the entire ``.usage.json`` map. Returns ``{}`` on missing/corrupt."""
    path = _usage_path(workspace)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("[skill_curator] failed to read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(slug): rec for slug, rec in data.items() if isinstance(rec, dict)}


def save_skill_usage(workspace: Path, data: dict[str, dict[str, Any]]) -> None:
    """Write the usage map atomically (tempfile + replace)."""
    path = _usage_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _mutate(workspace: Path, slug: str, mutator, *, now: datetime | None = None) -> None:
    """Load → ``mutator(record)`` in place → save. Creates a fresh record if absent.

    A freshly created record is seeded with ``now`` so callers that set the
    creation clock (e.g. ``mark_skill_created``) see their timestamp instead of
    wall-clock-at-default.
    """
    data = load_skill_usage(workspace)
    rec = data.get(slug)
    if not isinstance(rec, dict):
        rec = _empty_record(now=now)
    mutator(rec)
    data[slug] = rec
    save_skill_usage(workspace, data)


# ---------------------------------------------------------------------------
# Counter / provenance mutators
# ---------------------------------------------------------------------------


def mark_skill_created(
    workspace: Path,
    slug: str,
    *,
    created_by: str = _AGENT_PROVENANCE,
    now: datetime | None = None,
) -> None:
    """Initialize a usage record on skill creation.

    ``created_at`` / ``created_by`` are written only once — a later call (e.g. a
    save_skill overwrite) must not rewrite provenance or reset the clock.
    """

    def _apply(rec: dict[str, Any]) -> None:
        # save_skill is the authoritative provenance signal: promote a record
        # that has no real provenance yet (fresh, or use-observed as unknown) to
        # the declared author. An already-established provenance is immutable —
        # an overwrite save must not rewrite it.
        if rec.get("created_by") in (None, _UNKNOWN_PROVENANCE):
            rec["created_by"] = created_by
        rec.setdefault("created_at", _now_iso(now))
        rec.setdefault("last_used_at", None)
        rec.setdefault("use_count", 0)
        rec.setdefault("view_count", 0)
        rec.setdefault("state", STATE_ACTIVE)
        rec.setdefault("pinned", False)
        rec.setdefault("archived_at", None)

    _mutate(workspace, slug, _apply, now=now)


def bump_skill_use(
    workspace: Path,
    slug: str,
    *,
    kind: str = "use",
    now: datetime | None = None,
) -> None:
    """Record skill activity.

    ``kind="use"`` increments ``use_count`` and refreshes ``last_used_at``;
    ``kind="view"`` increments ``view_count`` only. A ``stale`` skill that is
    used again is revived to ``active`` (archived skills are off-disk and stay
    archived).
    """

    def _apply(rec: dict[str, Any]) -> None:
        rec.setdefault("created_at", _now_iso(now))
        # Use (load) is NOT a provenance signal — a loaded skill of unknown
        # origin stays unknown so the curator never archives default skills.
        rec.setdefault("created_by", _UNKNOWN_PROVENANCE)
        if kind == "view":
            rec["view_count"] = int(rec.get("view_count") or 0) + 1
        else:
            rec["use_count"] = int(rec.get("use_count") or 0) + 1
            rec["last_used_at"] = _now_iso(now)
            if rec.get("state") == STATE_STALE:
                rec["state"] = STATE_ACTIVE

    _mutate(workspace, slug, _apply, now=now)


def set_skill_pinned(workspace: Path, slug: str, pinned: bool) -> None:
    """Toggle the pinned flag (exempts a skill from automatic transitions)."""

    def _apply(rec: dict[str, Any]) -> None:
        rec["pinned"] = bool(pinned)

    _mutate(workspace, slug, _apply)


# ---------------------------------------------------------------------------
# Pure state machine
# ---------------------------------------------------------------------------


def apply_skill_auto_transitions(
    usage: dict[str, dict[str, Any]],
    *,
    now: datetime,
    stale_after_days: int = _STALE_AFTER_DAYS,
    archive_after_days: int = _ARCHIVE_AFTER_DAYS,
) -> list[dict[str, str]]:
    """Pure function: decide lifecycle transitions for every usage record.

    Anchor is ``last_used_at`` (falling back to ``created_at`` so brand-new
    never-used skills don't archive on day one). Rules, evaluated per slug:

    - anchor older than ``archive_after_days`` and not already archived → archived
    - else anchor older than ``stale_after_days`` and currently active → stale
    - pinned skills and non-``agent`` provenance are skipped entirely

    Does not mutate ``usage``; the orchestrator applies the returned transitions.
    Each transition is ``{"slug", "from", "to", "reason"}``.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stale_cutoff = now - timedelta(days=stale_after_days)
    archive_cutoff = now - timedelta(days=archive_after_days)

    transitions: list[dict[str, str]] = []
    for slug, rec in usage.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("created_by") != _AGENT_PROVENANCE:
            continue
        if rec.get("pinned"):
            continue

        current = rec.get("state", STATE_ACTIVE)
        if current == STATE_ARCHIVED:
            continue

        anchor = _parse_iso(rec.get("last_used_at")) or _parse_iso(rec.get("created_at")) or now

        if anchor <= archive_cutoff:
            transitions.append(
                {
                    "slug": slug,
                    "from": current,
                    "to": STATE_ARCHIVED,
                    "reason": f"unused since {anchor.date()} (> {archive_after_days}d)",
                }
            )
        elif anchor <= stale_cutoff and current == STATE_ACTIVE:
            transitions.append(
                {
                    "slug": slug,
                    "from": current,
                    "to": STATE_STALE,
                    "reason": f"unused since {anchor.date()} (> {stale_after_days}d)",
                }
            )

    return transitions


# ---------------------------------------------------------------------------
# Archive (real directory move — never delete)
# ---------------------------------------------------------------------------


def archive_skill(workspace: Path, slug: str) -> bool:
    """Move ``skills/<slug>`` to ``skills/.archive/<slug>``.

    Never deletes. On a name collision the destination gets a UTC-timestamp
    suffix. Returns ``True`` on success, ``False`` when the source is missing.
    """
    src = _skills_dir(workspace) / slug
    if not src.is_dir():
        return False

    archive_root = _archive_dir(workspace)
    archive_root.mkdir(parents=True, exist_ok=True)

    dest = archive_root / slug
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = archive_root / f"{slug}-{stamp}"

    try:
        src.rename(dest)
    except OSError:
        # Cross-device fallback.
        import shutil

        shutil.move(str(src), str(dest))
    return True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_skill_curator_pass(
    workspace: Path,
    *,
    now: datetime | None = None,
    stale_after_days: int = _STALE_AFTER_DAYS,
    archive_after_days: int = _ARCHIVE_AFTER_DAYS,
) -> dict[str, Any]:
    """Run one decay pass over a workspace's agent-authored skills.

    Pipeline: load usage → compute transitions (pure) → archive directories for
    ``archived`` targets → persist new states → emit one audit event per
    transition. Skills whose ``state`` is ``stale`` but were used recently
    enough that the state machine no longer flags them are revived to ``active``
    and reported.

    Returns ``{"scanned", "to_stale", "to_archived", "revived"}``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    usage = load_skill_usage(workspace)
    transitions = apply_skill_auto_transitions(
        usage,
        now=now,
        stale_after_days=stale_after_days,
        archive_after_days=archive_after_days,
    )

    to_stale: list[str] = []
    to_archived: list[str] = []
    flagged: set[str] = set()

    for transition in transitions:
        slug = transition["slug"]
        target = transition["to"]
        rec = usage.get(slug)
        if not isinstance(rec, dict):
            continue
        flagged.add(slug)

        if target == STATE_ARCHIVED:
            if archive_skill(workspace, slug):
                rec["state"] = STATE_ARCHIVED
                rec["archived_at"] = _now_iso(now)
                to_archived.append(slug)
            else:
                # Directory already gone — record the state anyway so we don't
                # re-attempt every pass, but don't claim a move that didn't happen.
                rec["state"] = STATE_ARCHIVED
                rec["archived_at"] = rec.get("archived_at") or _now_iso(now)
        elif target == STATE_STALE:
            rec["state"] = STATE_STALE
            to_stale.append(slug)

    # Revival: a record parked in `stale` that the state machine did NOT flag
    # (because it was used recently) should be pulled back to active.
    revived: list[str] = []
    for slug, rec in usage.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("created_by") != _AGENT_PROVENANCE or rec.get("pinned"):
            continue
        if rec.get("state") == STATE_STALE and slug not in flagged:
            rec["state"] = STATE_ACTIVE
            revived.append(slug)

    save_skill_usage(workspace, usage)

    for transition in transitions:
        try:
            from app.services.skill_lifecycle import record_skill_lifecycle_event

            record_skill_lifecycle_event(
                workspace,
                skill_name=transition["slug"],
                status=transition["to"],
                note=f"curator auto-transition {transition['from']}→{transition['to']}: {transition['reason']}",
            )
        except Exception as exc:  # pragma: no cover - audit must never break the pass
            logger.warning("[skill_curator] failed to record audit for %s: %s", transition["slug"], exc)

    return {
        "scanned": len(usage),
        "to_stale": to_stale,
        "to_archived": to_archived,
        "revived": revived,
    }
