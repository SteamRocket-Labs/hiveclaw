"""Convergence-loop dirtiness measurement (工序 4, spec §4.3).

Writes are incremental operation patches; convergence is the opposite — a
full-file LLM rewrite that merges duplicate motifs, resolves contradictions,
and physically clears retired entries. The split of labor:

- Platform (here): measure dirtiness mechanically per profile-plane file
  (bloat vs budget share, retired-entry count, resident over-budget signal),
  persist the ledger to ``memory/control/convergence_dirtiness.json``, and
  surface a CONVERGENCE NEEDED warning in the curator's neighborhood.
- LLM (heartbeat curator / dream): perform the actual rewrite through the
  gate's ``rewrite_file`` operation — full input, convergence note required,
  old version archived, source refs unioned.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.memory.t3_platform_gate import PROFILE_PLANE_TARGETS

logger = logging.getLogger(__name__)

DIRTINESS_REPORT_SCHEMA = "convergence_dirtiness.v1"
DEFAULT_MAX_CHARS_PER_FILE = 6_000.0
DEFAULT_MAX_RETIRED_ENTRIES = 2

_RETIRED_MARKER_RE = re.compile(r"<!--\s*retired:", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ConvergenceDirtinessReport:
    agent_id: str
    dirty_files: tuple[dict, ...] = ()


def assess_convergence_dirtiness(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    max_chars_per_file: float = DEFAULT_MAX_CHARS_PER_FILE,
    max_retired_entries: int = DEFAULT_MAX_RETIRED_ENTRIES,
) -> ConvergenceDirtinessReport:
    """Measure profile-plane dirtiness. Pure read — no writes, no LLM."""

    mem_dir = Path(data_root) / str(agent_id) / "memory"
    dirty: list[dict] = []
    resident_over_budget = _resident_over_budget(mem_dir)
    for target in PROFILE_PLANE_TARGETS:
        path = mem_dir.parent / target
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Convergence dirtiness skipped unreadable %s: %s", path, exc)
            continue
        reasons: list[str] = []
        if len(content) > max_chars_per_file:
            reasons.append(f"chars {len(content)} > {int(max_chars_per_file)} (profile plane must converge, not grow)")
        retired_count = len(_RETIRED_MARKER_RE.findall(content))
        if retired_count > max_retired_entries:
            reasons.append(f"{retired_count} retired entries awaiting physical removal")
        if resident_over_budget and target == "memory/self/self.md":
            reasons.append("resident memory over budget (read-side signal)")
        if reasons:
            dirty.append(
                {"target": target, "chars": len(content), "retired_entries": retired_count, "reasons": reasons}
            )
    return ConvergenceDirtinessReport(agent_id=str(agent_id), dirty_files=tuple(dirty))


def refresh_convergence_dirtiness(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    max_chars_per_file: float = DEFAULT_MAX_CHARS_PER_FILE,
    max_retired_entries: int = DEFAULT_MAX_RETIRED_ENTRIES,
) -> ConvergenceDirtinessReport:
    """Assess and persist the dirtiness ledger for the curator/observers."""

    report = assess_convergence_dirtiness(
        agent_id=agent_id,
        data_root=data_root,
        max_chars_per_file=max_chars_per_file,
        max_retired_entries=max_retired_entries,
    )
    payload = {
        "schema": DIRTINESS_REPORT_SCHEMA,
        "agent_id": report.agent_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "dirty_files": [dict(item) for item in report.dirty_files],
    }
    report_path = Path(data_root) / str(agent_id) / "memory" / "control" / "convergence_dirtiness.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def convergence_warning_lines(*, agent_id: uuid.UUID | str, data_root: Path | str) -> list[str]:
    """Warning lines for the T3 neighborhood when convergence is due."""

    report_path = Path(data_root) / str(agent_id) / "memory" / "control" / "convergence_dirtiness.json"
    if not report_path.exists():
        return []
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Convergence dirtiness ledger unreadable for %s: %s", agent_id, exc)
        return []
    dirty_files = payload.get("dirty_files") or []
    if not dirty_files:
        return []
    lines = ["", "## ⚠ CONVERGENCE NEEDED (工序 4)", ""]
    for item in dirty_files:
        target = str(item.get("target") or "")
        reasons = "; ".join(str(reason) for reason in item.get("reasons") or [])
        lines.append(f"- `{target}` — {reasons}")
    lines.append(
        "Perform a full-file convergence rewrite through `rewrite_file` "
        "(merge motifs, resolve contradictions, remove retired entries, union source refs)."
    )
    return lines


def _resident_over_budget(mem_dir: Path) -> bool:
    marker = mem_dir / "control" / "resident_budget.json"
    if not marker.exists():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(payload.get("over_budget"))
