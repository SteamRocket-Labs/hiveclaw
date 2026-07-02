"""Reference-counted T2 retention with archival (C9-3, spec §3.6/§6.2.3/§6.5).

T2 is never hard-deleted: a package with zero live references that has aged
past the threshold MOVES to ``memory/.archive/t2/**`` (structure preserved,
append-only archive log) and its ``t2://`` ref keeps resolving through the
reference index. Retention manages the hot zone, not the truth.

Protection order per package:
1. pipeline — reviewed/closed packages still routed to t3_intake or
   episode_stitching are consolidation debt, not garbage (C9-2 watches them);
2. protected domain — decision/permission-domain evidence never expires
   (spec §6.5, Letta-aligned rule);
3. referenced — any live referrer (T3 block, explicit entry, episode) pins it;
4. recent — under ``archive_after_days`` stays hot regardless.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.memory.reference_index import (
    ARCHIVE_LOG_RELATIVE,
    mark_ref_archived,
    rebuild_reference_index,
    reference_counts,
    scan_t2_packages,
)

logger = logging.getLogger(__name__)

RETENTION_REPORT_SCHEMA = "t2_retention.v1"
DEFAULT_ARCHIVE_AFTER_DAYS = 30.0

_PIPELINE_STATUSES = {"reviewed", "closed"}
_PIPELINE_ALLOWED_NEXT = {"t3_intake", "episode_stitching"}
_PROTECTED_DOMAIN_MARKERS = ("decision", "permission")


@dataclass(frozen=True, slots=True)
class T2RetentionReport:
    agent_id: str
    scanned: int = 0
    archived: tuple[str, ...] = ()
    kept_referenced: int = 0
    kept_pipeline: int = 0
    kept_protected_domain: int = 0
    kept_recent: int = 0


async def run_t2_retention(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    now: datetime | None = None,
    archive_after_days: float = DEFAULT_ARCHIVE_AFTER_DAYS,
) -> T2RetentionReport:
    """Archive cold, unreferenced, non-pipeline T2 packages. Never deletes."""

    root = Path(data_root)
    current = _now_utc(now)
    rebuild_reference_index(agent_id=agent_id, data_root=root)
    counts = reference_counts(agent_id=agent_id, data_root=root)

    scanned = 0
    archived: list[str] = []
    kept_referenced = 0
    kept_pipeline = 0
    kept_protected_domain = 0
    kept_recent = 0

    for package in scan_t2_packages(root, agent_id):
        if package["archived"]:
            continue
        scanned += 1
        manifest: dict[str, Any] = package["manifest"]
        ref: str = package["ref"]
        status = str(manifest.get("package_status") or manifest.get("status") or "").strip().lower()
        if status in _PIPELINE_STATUSES and _allowed_next(package) in _PIPELINE_ALLOWED_NEXT:
            kept_pipeline += 1
            continue
        if package["kind"] == "segment" and _has_protected_domain(package["dir"]):
            kept_protected_domain += 1
            continue
        if counts.get(ref, 0) > 0:
            kept_referenced += 1
            continue
        if _age_days(manifest.get("created_at"), now=current) <= archive_after_days:
            kept_recent += 1
            continue
        _archive_package(root=root, agent_id=agent_id, package=package, now=current)
        archived.append(ref)

    report = T2RetentionReport(
        agent_id=str(agent_id),
        scanned=scanned,
        archived=tuple(archived),
        kept_referenced=kept_referenced,
        kept_pipeline=kept_pipeline,
        kept_protected_domain=kept_protected_domain,
        kept_recent=kept_recent,
    )
    _write_control_report(root, agent_id, report, now=current)
    return report


def _archive_package(*, root: Path, agent_id: uuid.UUID | str, package: dict[str, Any], now: datetime) -> None:
    agent_root = root / str(agent_id)
    source_dir: Path = package["dir"]
    relative = source_dir.relative_to(agent_root / "memory")
    target_dir = agent_root / "memory" / ".archive" / relative
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source_dir, target_dir)

    archived_at = now.isoformat()
    target_rel = target_dir.relative_to(agent_root).as_posix()
    log_path = agent_root / "memory" / ARCHIVE_LOG_RELATIVE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ref": package["ref"],
                    "package_id": package["package_id"],
                    "from_path": package["path"],
                    "to_path": target_rel,
                    "archived_at": archived_at,
                    "reason": "unreferenced past retention threshold; archived, never deleted (spec §3.6)",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    mark_ref_archived(agent_id=agent_id, data_root=root, ref=package["ref"], path=target_rel, archived_at=archived_at)
    logger.info("T2 retention archived %s -> %s", package["ref"], target_rel)


def _allowed_next(package: dict[str, Any]) -> str:
    tag = "episode_review" if package["kind"] == "episode" else "t2_review"
    review_path: Path = package["dir"] / "review.md"
    try:
        text = review_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    review = _extract_single_xml(text, tag)
    if review is None:
        return ""
    return (review.findtext("allowed_next") or "").strip()


def _has_protected_domain(package_dir: Path) -> bool:
    try:
        text = (package_dir / "labels.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    labels = _extract_single_xml(text, "t2_labels")
    if labels is None:
        return False
    for node in labels.findall(".//memory_domain"):
        domain = (node.text or "").strip().lower()
        if any(marker in domain for marker in _PROTECTED_DOMAIN_MARKERS):
            return True
    return False


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


def _age_days(raw: Any, *, now: datetime) -> float:
    stamp_text = str(raw or "").strip()
    if not stamp_text:
        return 0.0
    try:
        stamp = datetime.fromisoformat(stamp_text)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, (now - stamp).total_seconds() / 86_400.0)


def _write_control_report(root: Path, agent_id: uuid.UUID | str, report: T2RetentionReport, *, now: datetime) -> None:
    payload = {
        "schema": RETENTION_REPORT_SCHEMA,
        "agent_id": report.agent_id,
        "generated_at": now.isoformat(),
        "scanned": report.scanned,
        "archived": list(report.archived),
        "kept_referenced": report.kept_referenced,
        "kept_pipeline": report.kept_pipeline,
        "kept_protected_domain": report.kept_protected_domain,
        "kept_recent": report.kept_recent,
    }
    report_path = root / str(agent_id) / "memory" / "control" / "t2_retention.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now_utc(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
