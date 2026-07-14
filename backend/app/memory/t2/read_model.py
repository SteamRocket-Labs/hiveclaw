"""Read-only views over canonical T2 Segment Packages.

This module reads canonical
``memory/t2/sessions/<session_id>/segments/<segment_id>`` packages first, with
read-only compatibility for legacy ``memory/sessions`` packages. Legacy
``memory/learnings/*.md`` files are compatibility artifacts and must not feed
runtime memory, heartbeat, or self-evolution context.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path


_PACKAGE_FILES = ("manifest.json", "summary.md", "labels.md", "synthesis.md", "review.md")
_ACTIVE_PACKAGE_STATUSES = {"reviewed", "closed", "absorbed", "t3_absorbed"}


@dataclass(frozen=True, slots=True)
class T2PackageSnapshot:
    rel_path: str
    package_kind: str
    package_status: str
    source_refs: tuple[str, ...]
    summary_md: str
    labels_md: str
    synthesis_md: str
    review_md: str
    updated_mtime: float


def load_t2_package_snapshots(
    data_root: Path | str,
    agent_id: uuid.UUID | str,
    *,
    known_mtimes: dict[str, float] | None = None,
    limit: int | None = None,
) -> tuple[list[T2PackageSnapshot], dict[str, float]]:
    """Load canonical T2 Segment Package snapshots and current mtimes.

    ``known_mtimes`` filters unchanged packages for incremental heartbeat reads.
    The returned mtime map contains only canonical package relpaths.
    """

    root = Path(data_root)
    agent_root = root / str(agent_id)

    snapshots: list[T2PackageSnapshot] = []
    current_mtimes: dict[str, float] = {}
    seen: set[str] = set()
    for sessions_root in (agent_root / "memory" / "t2" / "sessions", agent_root / "memory" / "sessions"):
        if not sessions_root.exists():
            continue
        manifest_paths = sorted(
            [*sessions_root.glob("*/segments/*/manifest.json"), *sessions_root.glob("*/episodes/*/manifest.json")]
        )
        for manifest_path in manifest_paths:
            package_dir = manifest_path.parent
            rel_path = _relative_package_path(agent_root, package_dir)
            updated_mtime = _package_mtime(package_dir)
            current_mtimes[rel_path] = updated_mtime
            if known_mtimes is not None and updated_mtime <= float(known_mtimes.get(rel_path, 0.0)):
                continue

            snapshot = _load_snapshot(package_dir=package_dir, agent_root=agent_root, updated_mtime=updated_mtime)
            if snapshot is not None:
                dedupe_key = _package_identity_key(snapshot.rel_path)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                snapshots.append(snapshot)

    snapshots.sort(key=lambda item: item.updated_mtime, reverse=True)
    if limit is not None:
        snapshots = snapshots[: max(0, limit)]
    return snapshots, current_mtimes


def render_t2_package_snapshots(snapshots: list[T2PackageSnapshot] | tuple[T2PackageSnapshot, ...]) -> str:
    blocks: list[str] = []
    for snapshot in snapshots:
        lines = [
            f"### {snapshot.rel_path}",
            f"- package_kind: {snapshot.package_kind}",
            f"- package_status: {snapshot.package_status or 'unknown'}",
        ]
        if snapshot.source_refs:
            lines.append("- source_refs:")
            lines.extend(f"  - {ref}" for ref in snapshot.source_refs)
        sections = (
            (("synthesis.md", snapshot.synthesis_md), ("review.md", snapshot.review_md))
            if snapshot.package_kind == "episode_stitch_package"
            else (
                ("summary.md", snapshot.summary_md),
                ("labels.md", snapshot.labels_md),
                ("review.md", snapshot.review_md),
            )
        )
        for label, text in sections:
            if text.strip():
                lines.append(f"#### {label}")
                lines.append(text.strip())
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def t2_package_has_source_refs(snapshot: T2PackageSnapshot) -> bool:
    if snapshot.source_refs:
        return True
    text = "\n".join((snapshot.summary_md, snapshot.labels_md, snapshot.review_md)).lower()
    return "source_ref" in text or "source_refs" in text or "t0://" in text


def _load_snapshot(*, package_dir: Path, agent_root: Path, updated_mtime: float) -> T2PackageSnapshot | None:
    try:
        manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(manifest, dict):
        return None

    status = str(manifest.get("package_status") or manifest.get("status") or "").strip().lower()
    if status and status not in _ACTIVE_PACKAGE_STATUSES:
        return None
    package_kind = (
        "episode_stitch_package"
        if str(manifest.get("schema_version") or "").strip() == "t2.episode-stitch.manifest.v1"
        or package_dir.parent.name == "episodes"
        else "segment_package"
    )
    source_refs = tuple(str(ref).strip() for ref in (manifest.get("source_refs") or []) if str(ref).strip())
    return T2PackageSnapshot(
        rel_path=_relative_package_path(agent_root, package_dir),
        package_kind=package_kind,
        package_status=status,
        source_refs=source_refs,
        summary_md=_read_optional(package_dir / "summary.md"),
        labels_md=_read_optional(package_dir / "labels.md"),
        synthesis_md=_read_optional(package_dir / "synthesis.md"),
        review_md=_read_optional(package_dir / "review.md"),
        updated_mtime=updated_mtime,
    )


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _package_mtime(package_dir: Path) -> float:
    mtimes = []
    for filename in _PACKAGE_FILES:
        try:
            mtimes.append((package_dir / filename).stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0.0


def _relative_package_path(agent_root: Path, package_dir: Path) -> str:
    try:
        return package_dir.relative_to(agent_root).as_posix()
    except ValueError:
        return package_dir.as_posix()


def _package_identity_key(rel_path: str) -> str:
    normalized = str(rel_path).strip().strip("/")
    if normalized.startswith("memory/t2/sessions/"):
        return normalized.removeprefix("memory/t2/")
    if normalized.startswith("memory/sessions/"):
        return normalized.removeprefix("memory/")
    return normalized
