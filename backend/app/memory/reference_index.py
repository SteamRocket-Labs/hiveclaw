"""Reverse-reference index over T2 evidence (C9-3, spec §4.1; C8 minimal set).

SQLite is a derived accelerator ONLY: every row is rebuilt from Markdown/JSONL
truth (T3 accepted blocks, active explicit overlay entries, episode manifests,
live + archived package directories, the archive log). Deleting
``memory/indexes/index.sqlite`` loses nothing — ``rebuild_reference_index`` is
the single writer.

The reverse-index count is the retention basis (spec §4.1): a ``t2://`` ref
with zero live referrers is archive-eligible; resolution keeps working after
archival because archived package directories are scanned back into the
``id_resolution`` table ("id 永远可解析", spec §3.6).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.memory.explicit_overlay import load_explicit_overlay_entries
from app.memory.md_store import extract_t3_xml_blocks, parse_t3_xml_block

logger = logging.getLogger(__name__)

INDEX_DB_FILENAME = "index.sqlite"
ARCHIVE_LOG_RELATIVE = Path(".archive") / "t2" / "archive_log.jsonl"

_T3_FILENAMES = ("episodes.md", "user.md", "worker.md", "capabilities.md")


@dataclass(frozen=True, slots=True)
class ReferenceIndexRebuildReport:
    agent_id: str
    referrers: int
    refs: int
    packages: int


@dataclass(frozen=True, slots=True)
class ResolvedRef:
    ref: str
    path: str
    archived_at: str


def index_db_path(data_root: Path | str, agent_id: uuid.UUID | str) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / "indexes" / INDEX_DB_FILENAME


def rebuild_reference_index(*, agent_id: uuid.UUID | str, data_root: Path | str) -> ReferenceIndexRebuildReport:
    """Rebuild the whole index from Markdown truth. The only writer."""

    root = Path(data_root)
    packages = _scan_packages(root, agent_id)
    package_id_to_ref = {package["package_id"]: package["ref"] for package in packages if package["package_id"]}
    archive_times = _archive_times(root, agent_id)

    rows: list[tuple[str, str, str]] = []
    rows.extend(_t3_reference_rows(root, agent_id))
    rows.extend(_explicit_reference_rows(root, agent_id))
    rows.extend(_episode_reference_rows(packages, package_id_to_ref))

    db_path = index_db_path(root, agent_id)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS refs")
        conn.execute("DROP TABLE IF EXISTS id_resolution")
        conn.execute(
            "CREATE TABLE refs (source_ref TEXT NOT NULL, referrer TEXT NOT NULL, referrer_kind TEXT NOT NULL,"
            " PRIMARY KEY (source_ref, referrer))"
        )
        conn.execute("CREATE TABLE id_resolution (ref TEXT PRIMARY KEY, path TEXT NOT NULL, archived_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT OR IGNORE INTO refs VALUES (?, ?, ?)", rows)
        conn.executemany(
            "INSERT OR REPLACE INTO id_resolution VALUES (?, ?, ?)",
            [
                (
                    package["ref"],
                    package["path"],
                    archive_times.get(package["ref"], "") if package["archived"] else "",
                )
                for package in packages
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('rebuilt_at', ?)",
            (datetime.now(UTC).isoformat(),),
        )
    return ReferenceIndexRebuildReport(
        agent_id=str(agent_id),
        referrers=len(rows),
        refs=len({row[0] for row in rows}),
        packages=len(packages),
    )


def reference_count(*, agent_id: uuid.UUID | str, data_root: Path | str, ref: str) -> int:
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(DISTINCT referrer) FROM refs WHERE source_ref = ?", (ref,)).fetchone()
    return int(row[0] or 0)


def reference_counts(*, agent_id: uuid.UUID | str, data_root: Path | str) -> dict[str, int]:
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT source_ref, COUNT(DISTINCT referrer) FROM refs GROUP BY source_ref").fetchall()
    return {str(ref): int(count) for ref, count in rows}


def resolve_ref(*, agent_id: uuid.UUID | str, data_root: Path | str, ref: str) -> ResolvedRef | None:
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT ref, path, archived_at FROM id_resolution WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        return None
    return ResolvedRef(ref=str(row[0]), path=str(row[1]), archived_at=str(row[2] or ""))


def mark_ref_archived(
    *, agent_id: uuid.UUID | str, data_root: Path | str, ref: str, path: str, archived_at: str
) -> None:
    """Point one ref at its archived location (called by the retention executor)."""
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO id_resolution VALUES (?, ?, ?)", (ref, path, archived_at))


def package_ref(*, session_id: str, source_id: str, kind: str) -> str:
    ref_kind = "episode" if kind == "episode" else "segment"
    return f"t2://session/{session_id}/{ref_kind}/{source_id}"


def scan_t2_packages(root: Path, agent_id: uuid.UUID | str) -> list[dict]:
    """Enumerate live + archived T2 packages with ref, kind, manifest, and path."""
    return _scan_packages(Path(root), agent_id)


def _ensure_index(*, agent_id: uuid.UUID | str, data_root: Path | str) -> Path:
    db_path = index_db_path(data_root, agent_id)
    if not db_path.exists():
        rebuild_reference_index(agent_id=agent_id, data_root=data_root)
    return db_path


def _scan_packages(root: Path, agent_id: uuid.UUID | str) -> list[dict]:
    agent_root = root / str(agent_id)
    scan_roots = (
        (agent_root / "memory" / "t2" / "sessions", False),
        (agent_root / "memory" / "sessions", False),
        (agent_root / "memory" / ".archive" / "t2" / "sessions", True),
    )
    packages: list[dict] = []
    for sessions_dir, archived in scan_roots:
        if not sessions_dir.exists():
            continue
        for kind, pattern in (("segment", "*/segments/*/manifest.json"), ("episode", "*/episodes/*/manifest.json")):
            for manifest_path in sorted(sessions_dir.glob(pattern)):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError) as exc:
                    logger.warning("Reference index skipped unreadable manifest %s: %s", manifest_path, exc)
                    continue
                if not isinstance(manifest, dict):
                    continue
                package_dir = manifest_path.parent
                session_id = package_dir.parent.parent.name
                source_id = package_dir.name
                packages.append(
                    {
                        "ref": package_ref(session_id=session_id, source_id=source_id, kind=kind),
                        "kind": kind,
                        "package_id": str(manifest.get("package_id") or manifest.get("episode_id") or "").strip(),
                        "manifest": manifest,
                        "dir": package_dir,
                        "path": package_dir.relative_to(agent_root).as_posix(),
                        "archived": archived,
                    }
                )
    return packages


def _t3_reference_rows(root: Path, agent_id: uuid.UUID | str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    t3_dir = root / str(agent_id) / "memory" / "t3"
    if not t3_dir.exists():
        return rows
    for filename in _T3_FILENAMES:
        path = t3_dir / filename
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Reference index skipped unreadable T3 file %s: %s", path, exc)
            continue
        for block in extract_t3_xml_blocks(content):
            parsed = parse_t3_xml_block(block)
            if parsed is None:
                continue
            referrer = f"t3:{filename}#{parsed.block_id}"
            for ref in _split_refs(parsed.metadata.get("source_refs")):
                rows.append((ref, referrer, "t3_block"))
    return rows


def _explicit_reference_rows(root: Path, agent_id: uuid.UUID | str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for entry in load_explicit_overlay_entries(root, agent_id):
        if entry.status != "active":
            continue
        for ref in entry.source_refs:
            rows.append((ref, f"explicit:{entry.entry_id}", "explicit_entry"))
    return rows


def _episode_reference_rows(packages: list[dict], package_id_to_ref: dict[str, str]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for package in packages:
        if package["kind"] != "episode" or package["archived"]:
            continue
        manifest = package["manifest"]
        for source_package_id in manifest.get("source_packages") or []:
            source_ref = package_id_to_ref.get(str(source_package_id).strip())
            if source_ref:
                rows.append((source_ref, package["ref"], "episode_package"))
    return rows


def _archive_times(root: Path, agent_id: uuid.UUID | str) -> dict[str, str]:
    log_path = root / str(agent_id) / "memory" / ARCHIVE_LOG_RELATIVE
    if not log_path.exists():
        return {}
    times: dict[str, str] = {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        ref = str(record.get("ref") or "").strip()
        if ref:
            times[ref] = str(record.get("archived_at") or "").strip() or times.get(ref, "")
    return times


def _split_refs(raw: object) -> list[str]:
    return [ref.strip() for ref in str(raw or "").split(",") if ref.strip()]
