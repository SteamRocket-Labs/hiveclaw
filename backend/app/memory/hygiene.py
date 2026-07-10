"""Workspace-wide memory hygiene repair for D1/D2/D8/D10 purity debts."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_RETIRED_ARTIFACTS = (
    Path("memory.sqlite3"),
    Path("memory.json"),
    Path("memory/memory.sqlite3"),
    Path("memory/memory.json"),
)
_DEAD_STUB_FILES = (
    Path("reflections.md"),
    Path("memory/reflections.md"),
    Path("evolution/reflections.md"),
)
_RETIRED_ROOT_FILES = (Path("focus" + ".md"),)


def _rel(path: Path) -> str:
    return path.as_posix()


def _iter_agent_ids(data_root: Path) -> list[uuid.UUID]:
    root = Path(data_root)
    if not root.exists():
        return []
    agent_ids: list[uuid.UUID] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        try:
            agent_ids.append(uuid.UUID(child.name))
        except ValueError:
            continue
    return agent_ids


def _archive_target(agent_root: Path, rel_path: Path) -> Path:
    target = agent_root / "memory" / "retired_artifacts" / rel_path
    if not target.exists():
        return target
    counter = 1
    while True:
        candidate = target.with_name(f"{target.name}.{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quarantine_file(
    agent_root: Path,
    rel_path: Path,
    *,
    reason: str,
    dry_run: bool,
) -> dict[str, Any] | None:
    source = agent_root / rel_path
    if not source.is_file():
        return None
    target = _archive_target(agent_root, rel_path)
    bytes_before = source.stat().st_size
    sha256_before = _sha256(source)
    action = {
        "path": _rel(rel_path),
        "target": _rel(target.relative_to(agent_root)),
        "reason": reason,
        "bytes_before": bytes_before,
        "sha256_before": sha256_before,
        "bytes_after": bytes_before,
        "sha256_after": sha256_before,
    }
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        action["bytes_after"] = target.stat().st_size
        action["sha256_after"] = _sha256(target)
        if action["bytes_after"] != bytes_before or action["sha256_after"] != sha256_before:
            raise RuntimeError(f"quarantine hash mismatch for {rel_path}")
    return action


def _append_quarantine_archive(agent_root: Path, actions: list[dict[str, Any]]) -> None:
    if not actions:
        return
    memory_dir = agent_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    archive_path = memory_dir / "archive.md"
    existing = archive_path.read_text(encoding="utf-8", errors="replace") if archive_path.exists() else "# Archive\n"
    now = datetime.now(UTC).isoformat()
    lines = ["", "## Memory Hygiene Quarantine", ""]
    for action in actions:
        lines.append(f"- [{now}] moved `{action['path']}` -> `{action['target']}` ({action['reason']})")
    archive_path.write_text(existing.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")


def _legacy_learning_paths(agent_root: Path) -> list[Path]:
    learnings_root = agent_root / "memory" / "learnings"
    if not learnings_root.is_dir():
        return []
    return sorted(
        (path.relative_to(agent_root) for path in learnings_root.rglob("*") if path.is_file()),
        key=_rel,
    )


def _digest_actions(actions: list[dict[str, Any]], *, suffix: str) -> str:
    digest = hashlib.sha256()
    for action in sorted(actions, key=lambda item: str(item["path"])):
        digest.update(str(action["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(action[f"bytes_{suffix}"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(action[f"sha256_{suffix}"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _reconciliation(actions: list[dict[str, Any]], *, dry_run: bool) -> dict[str, Any]:
    source_bytes = sum(int(action["bytes_before"]) for action in actions)
    quarantine_bytes = sum(int(action["bytes_after"]) for action in actions)
    source_digest = _digest_actions(actions, suffix="before")
    quarantine_digest = _digest_actions(actions, suffix="after")
    verified = source_bytes == quarantine_bytes and source_digest == quarantine_digest
    return {
        "mode": "planned" if dry_run else "applied",
        "source_file_count": len(actions),
        "quarantine_file_count": len(actions),
        "source_bytes": source_bytes,
        "quarantine_bytes": quarantine_bytes,
        "source_digest": source_digest,
        "quarantine_digest": quarantine_digest,
        "verified": verified,
    }


def repair_agent_memory_hygiene(data_root: Path, agent_id: uuid.UUID, *, dry_run: bool = True) -> dict[str, Any]:
    """Repair one agent workspace's memory purity issues.

    Retired stores and dead reflection stubs are moved to
    ``memory/retired_artifacts`` and recorded in ``memory/archive.md``.
    """
    root = Path(data_root)
    agent_root = root / str(agent_id)
    backfill = {}  # legacy flat-T3 prose backfill retired at the C7 cutover

    retired_artifacts: list[dict[str, Any]] = []
    for rel_path in _RETIRED_ARTIFACTS:
        action = _quarantine_file(
            agent_root,
            rel_path,
            reason="retired MD-first shadow store artifact",
            dry_run=dry_run,
        )
        if action:
            retired_artifacts.append(action)

    dead_stubs: list[dict[str, Any]] = []
    for rel_path in _DEAD_STUB_FILES:
        action = _quarantine_file(
            agent_root,
            rel_path,
            reason="dead pre-spec reflections.md stub; canonical reflections live under memory/reflections/",
            dry_run=dry_run,
        )
        if action:
            dead_stubs.append(action)

    retired_root_files: list[dict[str, Any]] = []
    for rel_path in _RETIRED_ROOT_FILES:
        action = _quarantine_file(
            agent_root,
            rel_path,
            reason="retired objective/focus projection; canonical task state lives in Work Ledger and RuntimeTask",
            dry_run=dry_run,
        )
        if action:
            retired_root_files.append(action)

    legacy_learnings: list[dict[str, Any]] = []
    for rel_path in _legacy_learning_paths(agent_root):
        action = _quarantine_file(
            agent_root,
            rel_path,
            reason="retired weighted-learnings compatibility store; canonical T2 is Segment Packages",
            dry_run=dry_run,
        )
        if action:
            legacy_learnings.append(action)

    all_actions = retired_artifacts + dead_stubs + retired_root_files + legacy_learnings

    if not dry_run:
        _append_quarantine_archive(agent_root, all_actions)

    changed = bool(backfill.get("entries_migrated") or all_actions)
    return {
        "agent_id": str(agent_id),
        "dry_run": dry_run,
        "changed": changed,
        "files_changed": int(backfill.get("files_changed") or 0),
        "entries_migrated": int(backfill.get("entries_migrated") or 0),
        "backfill_diff": list(backfill.get("diff") or []),
        "retired_artifacts": retired_artifacts,
        "dead_stubs": dead_stubs,
        "retired_root_files": retired_root_files,
        "legacy_learnings": legacy_learnings,
        "reconciliation": _reconciliation(all_actions, dry_run=dry_run),
    }


def repair_all_memory_hygiene(data_root: Path, *, dry_run: bool = True) -> dict[str, Any]:
    """Repair every UUID-named agent workspace under ``data_root``."""
    agent_reports = [
        repair_agent_memory_hygiene(data_root, agent_id, dry_run=dry_run) for agent_id in _iter_agent_ids(data_root)
    ]
    source_file_count = sum(int(report["reconciliation"]["source_file_count"]) for report in agent_reports)
    quarantine_file_count = sum(int(report["reconciliation"]["quarantine_file_count"]) for report in agent_reports)
    source_bytes = sum(int(report["reconciliation"]["source_bytes"]) for report in agent_reports)
    quarantine_bytes = sum(int(report["reconciliation"]["quarantine_bytes"]) for report in agent_reports)
    return {
        "schema": "memory_hygiene_report.v2",
        "dry_run": dry_run,
        "generated_at": datetime.now(UTC).isoformat(),
        "agents_scanned": len(agent_reports),
        "agents_changed": sum(1 for report in agent_reports if report["changed"]),
        "entries_migrated": sum(int(report["entries_migrated"]) for report in agent_reports),
        "files_changed": sum(int(report["files_changed"]) for report in agent_reports),
        "retired_artifacts": sum(len(report["retired_artifacts"]) for report in agent_reports),
        "dead_stubs": sum(len(report["dead_stubs"]) for report in agent_reports),
        "retired_root_files": sum(len(report["retired_root_files"]) for report in agent_reports),
        "legacy_learnings": sum(len(report["legacy_learnings"]) for report in agent_reports),
        "source_file_count": source_file_count,
        "quarantine_file_count": quarantine_file_count,
        "source_bytes": source_bytes,
        "quarantine_bytes": quarantine_bytes,
        "verified_agents": sum(1 for report in agent_reports if report["reconciliation"]["verified"]),
        "agents": agent_reports,
    }
