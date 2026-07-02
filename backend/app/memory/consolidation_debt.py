"""Consolidation-debt ledger + stall alerting (C9-2, spec §6.2.2).

T2→T3 consolidation debt is everything that has been distilled but not yet
absorbed into durable memory: reviewed t3_intake packages, segments waiting on
episode stitching, held/exhausted T2 jobs, and active explicit overlay
entries. Without accounting, a stopped heartbeat or a persistently failing
consolidator silently piles this up forever.

- ``assess_consolidation_debt`` is pure measurement (no writes, no alerts).
- ``refresh_consolidation_debt`` persists the ledger to
  ``memory/control/consolidation_debt.json`` and raises a one-shot
  ``memory_consolidation_stalled`` audit alert while stalled; the alert marker
  clears on recovery so a fresh stall alerts again.

Thresholds are platform config (``MEMORY_DEBT_*`` settings) threaded in by the
heartbeat wrapper; the module itself stays parameterized.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.memory.explicit_overlay import load_explicit_overlay_entries
from app.memory.t2.job_sweep import DEFAULT_MAX_RETRIES

logger = logging.getLogger(__name__)

DEBT_REPORT_SCHEMA = "consolidation_debt.v1"
DEFAULT_PENDING_AGE_ALERT_HOURS = 48.0
DEFAULT_EXPLICIT_AGE_ALERT_HOURS = 72.0

_PENDING_PACKAGE_STATUSES = {"reviewed", "closed"}
_JOB_DEBT_STATUSES = {"held", "failed"}


@dataclass(frozen=True, slots=True)
class ConsolidationDebtReport:
    agent_id: str
    pending_packages: int = 0
    pending_stitch_packages: int = 0
    oldest_pending_age_hours: float | None = None
    held_jobs: int = 0
    exhausted_jobs: int = 0
    active_explicit_entries: int = 0
    oldest_explicit_age_hours: float | None = None
    stalled: bool = False
    stall_reasons: tuple[str, ...] = ()
    alerted: bool = False


def assess_consolidation_debt(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    now: datetime | None = None,
    pending_age_alert_hours: float = DEFAULT_PENDING_AGE_ALERT_HOURS,
    explicit_age_alert_hours: float = DEFAULT_EXPLICIT_AGE_ALERT_HOURS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> ConsolidationDebtReport:
    """Measure current consolidation debt. Pure read — no writes, no alerts."""

    root = Path(data_root)
    current = _now_utc(now)

    pending_ages: list[float] = []
    stitch_waiting = 0
    stitched_triggers: set[str] = set()
    stitch_candidates: list[str] = []

    for package_dir, manifest, kind in _iter_t2_packages(root, agent_id):
        status = str(manifest.get("package_status") or manifest.get("status") or "").strip().lower()
        if kind == "episode":
            stitched_triggers.add(str(manifest.get("trigger_package_id") or "").strip())
        if status not in _PENDING_PACKAGE_STATUSES:
            continue
        allowed_next = _package_allowed_next(package_dir, kind)
        if allowed_next == "t3_intake":
            pending_ages.append(_age_hours(manifest.get("created_at"), now=current))
        elif allowed_next == "episode_stitching":
            stitch_candidates.append(str(manifest.get("package_id") or "").strip())

    stitch_waiting = sum(1 for package_id in stitch_candidates if package_id not in stitched_triggers)

    held_jobs = 0
    exhausted_jobs = 0
    for manifest in _iter_job_manifests(root, agent_id):
        status = str(manifest.get("status") or "").strip().lower()
        if status not in _JOB_DEBT_STATUSES:
            continue
        held_jobs += 1
        try:
            retry_count = max(0, int(manifest.get("retry_count") or 0))
        except (TypeError, ValueError):
            retry_count = 0
        if retry_count >= max_retries:
            exhausted_jobs += 1

    explicit_ages = [
        _age_hours(entry.created_at, now=current)
        for entry in load_explicit_overlay_entries(root, agent_id)
        if entry.status == "active"
    ]

    oldest_pending = max(pending_ages) if pending_ages else None
    oldest_explicit = max(explicit_ages) if explicit_ages else None

    reasons: list[str] = []
    if oldest_pending is not None and oldest_pending > pending_age_alert_hours:
        reasons.append("pending_package_overdue")
    if oldest_explicit is not None and oldest_explicit > explicit_age_alert_hours:
        reasons.append("explicit_entry_overdue")
    if exhausted_jobs > 0:
        reasons.append("t2_jobs_retry_exhausted")

    return ConsolidationDebtReport(
        agent_id=str(agent_id),
        pending_packages=len(pending_ages),
        pending_stitch_packages=stitch_waiting,
        oldest_pending_age_hours=oldest_pending,
        held_jobs=held_jobs,
        exhausted_jobs=exhausted_jobs,
        active_explicit_entries=len(explicit_ages),
        oldest_explicit_age_hours=oldest_explicit,
        stalled=bool(reasons),
        stall_reasons=tuple(reasons),
    )


async def refresh_consolidation_debt(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    now: datetime | None = None,
    pending_age_alert_hours: float = DEFAULT_PENDING_AGE_ALERT_HOURS,
    explicit_age_alert_hours: float = DEFAULT_EXPLICIT_AGE_ALERT_HOURS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> ConsolidationDebtReport:
    """Assess debt, persist the control ledger, and alert once per stall episode."""

    root = Path(data_root)
    current = _now_utc(now)
    report = assess_consolidation_debt(
        agent_id=agent_id,
        data_root=root,
        now=current,
        pending_age_alert_hours=pending_age_alert_hours,
        explicit_age_alert_hours=explicit_age_alert_hours,
        max_retries=max_retries,
    )

    report_path = root / str(agent_id) / "memory" / "control" / "consolidation_debt.json"
    previous_alerted_at = ""
    if report_path.exists():
        try:
            previous = json.loads(report_path.read_text(encoding="utf-8"))
            previous_alerted_at = str(previous.get("stall_alerted_at") or "").strip()
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Consolidation debt ledger unreadable for %s, rebuilding: %s", agent_id, exc)

    alerted = False
    stall_alerted_at = ""
    if report.stalled:
        if previous_alerted_at:
            stall_alerted_at = previous_alerted_at
        else:
            await _emit_stall_alert(report)
            stall_alerted_at = current.isoformat()
            alerted = True
    elif previous_alerted_at:
        logger.info("Memory consolidation recovered for agent %s (was stalled since %s)", agent_id, previous_alerted_at)

    payload = {
        "schema": DEBT_REPORT_SCHEMA,
        "agent_id": report.agent_id,
        "generated_at": current.isoformat(),
        "pending_packages": report.pending_packages,
        "pending_stitch_packages": report.pending_stitch_packages,
        "oldest_pending_age_hours": report.oldest_pending_age_hours,
        "held_jobs": report.held_jobs,
        "exhausted_jobs": report.exhausted_jobs,
        "active_explicit_entries": report.active_explicit_entries,
        "oldest_explicit_age_hours": report.oldest_explicit_age_hours,
        "stalled": report.stalled,
        "stall_reasons": list(report.stall_reasons),
        "stall_alerted_at": stall_alerted_at,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return ConsolidationDebtReport(
        agent_id=report.agent_id,
        pending_packages=report.pending_packages,
        pending_stitch_packages=report.pending_stitch_packages,
        oldest_pending_age_hours=report.oldest_pending_age_hours,
        held_jobs=report.held_jobs,
        exhausted_jobs=report.exhausted_jobs,
        active_explicit_entries=report.active_explicit_entries,
        oldest_explicit_age_hours=report.oldest_explicit_age_hours,
        stalled=report.stalled,
        stall_reasons=report.stall_reasons,
        alerted=alerted,
    )


async def _emit_stall_alert(report: ConsolidationDebtReport) -> None:
    payload = {
        "pending_packages": report.pending_packages,
        "pending_stitch_packages": report.pending_stitch_packages,
        "oldest_pending_age_hours": report.oldest_pending_age_hours,
        "held_jobs": report.held_jobs,
        "exhausted_jobs": report.exhausted_jobs,
        "active_explicit_entries": report.active_explicit_entries,
        "oldest_explicit_age_hours": report.oldest_explicit_age_hours,
        "stall_reasons": list(report.stall_reasons),
    }
    agent_id = _parse_uuid(report.agent_id)
    try:
        from app.services.audit_logger import write_audit_log

        await write_audit_log("memory_consolidation_stalled", payload, agent_id=agent_id)
    except Exception as exc:  # noqa: BLE001 - alerting must not break the ledger refresh; the ledger still records stalled
        logger.warning("Consolidation stall audit alert failed for agent %s: %s", report.agent_id, exc)


def _iter_t2_packages(root: Path, agent_id: uuid.UUID | str) -> list[tuple[Path, dict[str, Any], str]]:
    packages: list[tuple[Path, dict[str, Any], str]] = []
    for sessions_dir in (
        root / str(agent_id) / "memory" / "t2" / "sessions",
        root / str(agent_id) / "memory" / "sessions",
    ):
        if not sessions_dir.exists():
            continue
        for kind, pattern in (("segment", "*/segments/*/manifest.json"), ("episode", "*/episodes/*/manifest.json")):
            for manifest_path in sorted(sessions_dir.glob(pattern)):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError) as exc:
                    logger.warning("Consolidation debt skipped unreadable manifest %s: %s", manifest_path, exc)
                    continue
                if isinstance(manifest, dict):
                    packages.append((manifest_path.parent, manifest, kind))
    return packages


def _iter_job_manifests(root: Path, agent_id: uuid.UUID | str) -> list[dict[str, Any]]:
    jobs_dir = root / str(agent_id) / "memory" / ".staging" / "t2_jobs"
    if not jobs_dir.exists():
        return []
    manifests: list[dict[str, Any]] = []
    for manifest_path in sorted(jobs_dir.glob("*/job_manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Consolidation debt skipped unreadable job manifest %s: %s", manifest_path, exc)
            continue
        if isinstance(manifest, dict):
            manifests.append(manifest)
    return manifests


def _package_allowed_next(package_dir: Path, kind: str) -> str:
    tag = "episode_review" if kind == "episode" else "t2_review"
    try:
        text = (package_dir / "review.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    review = _extract_single_xml(text, tag)
    if review is None:
        return ""
    return (review.findtext("allowed_next") or "").strip()


def _extract_single_xml(markdown: str, tag: str) -> ET.Element | None:
    text = markdown or ""
    starts = list(re.finditer(rf"<{re.escape(tag)}(?=[\s>/])", text))
    end = text.rfind(f"</{tag}>")
    if not starts or end < 0 or len(starts) > 1:
        return None
    end += len(f"</{tag}>")
    try:
        return ET.fromstring(text[starts[0].start() : end])
    except ET.ParseError:
        return None


def _age_hours(raw: Any, *, now: datetime) -> float:
    stamp_text = str(raw or "").strip()
    if not stamp_text:
        return 0.0
    try:
        stamp = datetime.fromisoformat(stamp_text)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, (now - stamp).total_seconds() / 3600.0)


def _parse_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _now_utc(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
