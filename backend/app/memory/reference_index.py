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
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.memory.explicit_overlay import build_explicit_overlay_activation_keys, load_explicit_overlay_entries
from app.memory.md_store import extract_t3_xml_blocks, parse_t3_xml_block
from app.memory.plane_read import list_knowledge_pages, list_profile_entries

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
    label_axis_rows: int = 0
    debt_history_rows: int = 0
    activation_key_rows: int = 0


@dataclass(frozen=True, slots=True)
class ResolvedRef:
    ref: str
    path: str
    archived_at: str


@dataclass(frozen=True, slots=True)
class ResolvedMemoryRef:
    ref: str
    kind: str  # t2_package | milestone | knowledge | explicit_entry
    path: str
    archived_at: str


TOMBSTONE_LOG_RELATIVE = Path("control") / "tombstones.jsonl"


def index_db_path(data_root: Path | str, agent_id: uuid.UUID | str) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / "indexes" / INDEX_DB_FILENAME


def rebuild_reference_index(*, agent_id: uuid.UUID | str, data_root: Path | str) -> ReferenceIndexRebuildReport:
    """Rebuild the whole index from Markdown truth. The only writer."""

    root = Path(data_root)
    packages = _scan_packages(root, agent_id)
    package_id_to_ref = {package["package_id"]: package["ref"] for package in packages if package["package_id"]}
    archive_times = _archive_times(root, agent_id)

    short_id_to_ref = {
        _short_id_from_package_id(package["package_id"]): package["ref"]
        for package in packages
        if _short_id_from_package_id(package["package_id"])
    }

    rows: list[tuple[str, str, str]] = []
    rows.extend(_t3_reference_rows(root, agent_id))
    rows.extend(_plane_reference_rows(root, agent_id, short_id_to_ref))
    rows.extend(_explicit_reference_rows(root, agent_id))
    rows.extend(_episode_reference_rows(packages, package_id_to_ref))

    label_rows = _label_axis_rows(packages)
    debt_rows = _debt_history_rows(root, agent_id)
    activation_key_rows = _activation_key_rows(root, agent_id, label_rows=label_rows)

    resolution_rows: list[tuple[str, str, str, str]] = []
    for package in packages:
        archived_at = archive_times.get(package["ref"], "") if package["archived"] else ""
        resolution_rows.append((package["ref"], "t2_package", package["path"], archived_at))
        short_id = _short_id_from_package_id(package["package_id"])
        if short_id:
            resolution_rows.append((short_id, "t2_package", package["path"], archived_at))
    resolution_rows.extend(_page_resolution_rows(root, agent_id))
    resolution_rows.extend(_explicit_resolution_rows(root, agent_id))
    tombstone_rows = _tombstone_rows(root, agent_id)

    db_path = index_db_path(root, agent_id)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS refs")
        conn.execute("DROP TABLE IF EXISTS id_resolution")
        conn.execute("DROP TABLE IF EXISTS tombstones")
        conn.execute("DROP TABLE IF EXISTS t2_label_axes")
        conn.execute("DROP TABLE IF EXISTS consolidation_debt_history")
        conn.execute("DROP TABLE IF EXISTS activation_keys")
        conn.execute(
            "CREATE TABLE refs (source_ref TEXT NOT NULL, referrer TEXT NOT NULL, referrer_kind TEXT NOT NULL,"
            " PRIMARY KEY (source_ref, referrer))"
        )
        conn.execute(
            "CREATE TABLE id_resolution (ref TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 't2_package',"
            " path TEXT NOT NULL, archived_at TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE tombstones (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, job_id TEXT)")
        conn.execute(
            "CREATE TABLE t2_label_axes (package_ref TEXT NOT NULL, session_id TEXT NOT NULL,"
            " axis TEXT NOT NULL, value TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (package_ref, axis, value))"
        )
        conn.execute(
            "CREATE TABLE consolidation_debt_history (assessed_at TEXT PRIMARY KEY,"
            " pending_packages INTEGER NOT NULL, pending_stitch_packages INTEGER NOT NULL,"
            " oldest_pending_age_hours REAL, held_jobs INTEGER NOT NULL, exhausted_jobs INTEGER NOT NULL,"
            " active_explicit_entries INTEGER NOT NULL, oldest_explicit_age_hours REAL,"
            " stalled INTEGER NOT NULL, stall_reasons TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE activation_keys (candidate_ref TEXT NOT NULL, candidate_kind TEXT NOT NULL,"
            " scope TEXT NOT NULL, key_axis TEXT NOT NULL, key_value TEXT NOT NULL,"
            " source_ref TEXT NOT NULL, confidence REAL NOT NULL, created_at TEXT NOT NULL,"
            " PRIMARY KEY (candidate_ref, key_axis, key_value, source_ref))"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT OR IGNORE INTO refs VALUES (?, ?, ?)", rows)
        conn.executemany("INSERT OR REPLACE INTO id_resolution VALUES (?, ?, ?, ?)", resolution_rows)
        conn.executemany("INSERT OR REPLACE INTO tombstones VALUES (?, ?, ?)", tombstone_rows)
        conn.executemany("INSERT OR IGNORE INTO t2_label_axes VALUES (?, ?, ?, ?, ?)", label_rows)
        conn.executemany(
            "INSERT OR REPLACE INTO consolidation_debt_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            debt_rows,
        )
        conn.executemany("INSERT OR IGNORE INTO activation_keys VALUES (?, ?, ?, ?, ?, ?, ?, ?)", activation_key_rows)
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('rebuilt_at', ?)",
            (datetime.now(UTC).isoformat(),),
        )
    return ReferenceIndexRebuildReport(
        agent_id=str(agent_id),
        referrers=len(rows),
        refs=len({row[0] for row in rows}),
        packages=len(packages),
        label_axis_rows=len(label_rows),
        debt_history_rows=len(debt_rows),
        activation_key_rows=len(activation_key_rows),
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


def query_activation_keys(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    candidate_kind: str | None = None,
    scope: str | None = None,
    key_axis: str | None = None,
    key_value: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    columns = "candidate_ref, candidate_kind, scope, key_axis, key_value, source_ref, confidence, created_at"
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("candidate_kind", candidate_kind),
        ("scope", scope),
        ("key_axis", key_axis),
        ("key_value", key_value),
    ):
        cleaned = str(value or "").strip()
        if cleaned:
            clauses.append(f"{column} = ?")
            params.append(cleaned)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = (
        f"SELECT {columns} FROM activation_keys{where} "
        "ORDER BY confidence DESC, created_at DESC, candidate_ref ASC LIMIT ?"
    )
    params.append(max(1, int(limit or 100)))
    try:
        with sqlite3.connect(db_path) as conn:
            if not _table_exists(conn, "activation_keys"):
                raise sqlite3.OperationalError("no such table: activation_keys")
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "activation_keys" not in str(exc):
            raise
        rebuild_reference_index(agent_id=agent_id, data_root=data_root)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
    return [
        {
            "candidate_ref": str(row[0]),
            "candidate_kind": str(row[1]),
            "scope": str(row[2]),
            "key_axis": str(row[3]),
            "key_value": str(row[4]),
            "source_ref": str(row[5]),
            "confidence": float(row[6]),
            "created_at": str(row[7] or ""),
        }
        for row in rows
    ]


def candidate_refs_for_keys(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    keys: dict[str, str | list[str] | tuple[str, ...] | set[str] | frozenset[str]],
    scope: str | None = None,
    candidate_kind: str | None = None,
    limit: int = 50,
) -> list[str]:
    groups: list[tuple[str, set[str]]] = []
    for axis, raw_values in keys.items():
        if isinstance(raw_values, str):
            values = {raw_values.strip()} if raw_values.strip() else set()
        else:
            values = {str(value).strip() for value in raw_values if str(value).strip()}
        cleaned_axis = str(axis or "").strip()
        if cleaned_axis and values:
            groups.append((cleaned_axis, values))
    if not groups:
        return []
    rows = query_activation_keys(
        agent_id=agent_id,
        data_root=data_root,
        candidate_kind=candidate_kind,
        scope=scope,
        limit=max(1000, int(limit or 50) * 20),
    )
    matched_groups: dict[str, set[int]] = {}
    confidence: dict[str, float] = {}
    for row in rows:
        for index, (axis, values) in enumerate(groups):
            if row["key_axis"] == axis and row["key_value"] in values:
                ref = row["candidate_ref"]
                matched_groups.setdefault(ref, set()).add(index)
                confidence[ref] = max(confidence.get(ref, 0.0), float(row["confidence"]))
    required = len(groups)
    refs = [ref for ref, indexes in matched_groups.items() if len(indexes) == required]
    refs.sort(key=lambda ref: (-confidence.get(ref, 0.0), ref))
    return refs[: max(1, int(limit or 50))]


def resolve_ref(*, agent_id: uuid.UUID | str, data_root: Path | str, ref: str) -> ResolvedRef | None:
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT ref, path, archived_at FROM id_resolution WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        return None
    return ResolvedRef(ref=str(row[0]), path=str(row[1]), archived_at=str(row[2] or ""))


def resolve_memory_ref(*, agent_id: uuid.UUID | str, data_root: Path | str, ref: str) -> ResolvedMemoryRef | None:
    """Resolve any id-family ref (spec §4.1): t2://, t2-, ms-, ex-/explicit ids.

    Evidence refs (t2-/ex-/fb-) must resolve for the agent's whole life —
    archived packages resolve to their archive location, never to nothing.
    """
    needle = (ref or "").strip()
    if not needle:
        return None
    if needle.startswith("explicit://"):
        needle = needle.removeprefix("explicit://memory/").removeprefix("explicit://").split("#", 1)[0]
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT ref, kind, path, archived_at FROM id_resolution WHERE ref = ?", (needle,)).fetchone()
    if row is None:
        return None
    return ResolvedMemoryRef(ref=str(row[0]), kind=str(row[1]), path=str(row[2]), archived_at=str(row[3] or ""))


def record_entry_tombstones(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    tombstones: list[tuple[str, str]],
    job_id: str,
) -> None:
    """Record live-ref merges (old entry id → surviving id, spec §4.1).

    The append-only jsonl is the truth source; SQLite is the rebuildable
    projection updated in the same call.
    """
    cleaned = [
        (old.strip(), new.strip())
        for old, new in tombstones
        if old and new and old.strip() and new.strip() and old.strip() != new.strip()
    ]
    if not cleaned:
        return
    log_path = Path(data_root) / str(agent_id) / "memory" / TOMBSTONE_LOG_RELATIVE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    with log_path.open("a", encoding="utf-8") as handle:
        for old_id, new_id in cleaned:
            handle.write(
                json.dumps(
                    {"old_id": old_id, "new_id": new_id, "job_id": job_id, "created_at": now},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tombstones (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, job_id TEXT)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO tombstones VALUES (?, ?, ?)",
            [(old_id, new_id, job_id) for old_id, new_id in cleaned],
        )


def resolve_entry_id(*, agent_id: uuid.UUID | str, data_root: Path | str, entry_id: str) -> str:
    """Follow the tombstone chain to the surviving entry id (cycle-safe)."""
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    current = (entry_id or "").strip()
    visited: set[str] = set()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tombstones (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, job_id TEXT)"
        )
        while current and current not in visited:
            visited.add(current)
            row = conn.execute("SELECT new_id FROM tombstones WHERE old_id = ?", (current,)).fetchone()
            if row is None:
                return current
            current = str(row[0])
    return current


def mark_ref_archived(
    *, agent_id: uuid.UUID | str, data_root: Path | str, ref: str, path: str, archived_at: str
) -> None:
    """Point one ref at its archived location (called by the retention executor).

    The short evidence id (`t2-<hash>`) shares the package's previous path —
    move it together so 证据永不悬空 holds for both id forms.
    """
    db_path = _ensure_index(agent_id=agent_id, data_root=data_root)
    with sqlite3.connect(db_path) as conn:
        previous = conn.execute("SELECT path FROM id_resolution WHERE ref = ?", (ref,)).fetchone()
        previous_path = str(previous[0]) if previous else ""
        conn.execute(
            "INSERT OR REPLACE INTO id_resolution VALUES"
            " (?, COALESCE((SELECT kind FROM id_resolution WHERE ref = ?), 't2_package'), ?, ?)",
            (ref, ref, path, archived_at),
        )
        if previous_path:
            conn.execute(
                "UPDATE id_resolution SET path = ?, archived_at = ? WHERE path = ? AND ref != ?",
                (path, archived_at, previous_path, ref),
            )


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


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()
    return row is not None


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


_EVIDENCE_REF_RE = re.compile(r"t2://[^\s\)\]\"',，;；]+|\b(?:t2|ex|fb)-[0-9a-zA-Z][0-9a-zA-Z_-]{3,}\b")
_ENTRY_ANCHOR_RE = re.compile(r"<!--\s*id:\s*([^\s>]+)\s*-->")

_PROFILE_PLANE_FILES = (
    Path("memory") / "self" / "self.md",
    Path("memory") / "profiles" / "owner.md",
    Path("memory") / "profiles" / "collaborators.md",
    Path("memory") / "profiles" / "domain.md",
)


def _plane_reference_rows(
    root: Path,
    agent_id: uuid.UUID | str,
    short_id_to_ref: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Evidence refs from the two-plane surfaces (spec §4.1: entries cite
    immutable t2-/ex-/fb- ids). Each hit lands twice when a short id maps to a
    package: once under the short id and once under the canonical ``t2://``
    ref, so retention's URI-keyed reference counts see plane citations."""
    agent_root = root / str(agent_id)
    rows: list[tuple[str, str, str]] = []

    def _emit(refs: set[str], referrer: str, kind: str) -> None:
        for ref in sorted(refs):
            rows.append((ref, referrer, kind))
            canonical = short_id_to_ref.get(ref)
            if canonical:
                rows.append((canonical, referrer, kind))

    for relative in _PROFILE_PLANE_FILES:
        path = agent_root / relative
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Reference index skipped unreadable plane file %s: %s", path, exc)
            continue
        for entry_id, body in _iter_anchored_entries(content):
            _emit(set(_EVIDENCE_REF_RE.findall(body)), f"profile:{relative.name}#{entry_id}", "profile_entry")

    mem_dir = agent_root / "memory"
    for subdir, kind in (("knowledge", "knowledge_page"), ("milestones", "milestone_page")):
        directory = mem_dir / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            _emit(set(_EVIDENCE_REF_RE.findall(content)), f"page:{subdir}/{path.stem}", kind)
    return rows


def _iter_anchored_entries(content: str) -> list[tuple[str, str]]:
    """Split a profile-plane file into (entry_id, body) chunks by ``### `` +
    ``<!-- id: -->`` anchors; anchor-less text folds into the previous entry."""
    entries: list[tuple[str, str]] = []
    current_id = ""
    current_lines: list[str] = []
    for line in content.splitlines():
        if line.startswith("### "):
            if current_id and current_lines:
                entries.append((current_id, "\n".join(current_lines)))
            current_id = ""
            current_lines = [line]
            continue
        anchor = _ENTRY_ANCHOR_RE.search(line)
        if anchor and not current_id:
            current_id = anchor.group(1)
        current_lines.append(line)
    if current_id and current_lines:
        entries.append((current_id, "\n".join(current_lines)))
    return entries


_LABEL_SINGLE_AXES = ("continuity_state", "confidence", "source_integrity")
_LABEL_MULTI_AXES = (("risk_flag", ".//risk_flag"), ("system", ".//system"), ("memory_domain", ".//memory_domain"))


def _label_axis_rows(packages: list[dict]) -> list[tuple[str, str, str, str, str]]:
    """Split each live package's labels.md into per-axis rows. Missing axes
    stay absent — derived observability never guesses (evidence-gap rule)."""
    rows: list[tuple[str, str, str, str, str]] = []
    for package in packages:
        if package["archived"] or package["kind"] != "segment":
            continue
        labels_path = package["dir"] / "labels.md"
        if not labels_path.exists():
            continue
        try:
            content = labels_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Reference index skipped unreadable labels %s: %s", labels_path, exc)
            continue
        node = _parse_labels_block(content)
        if node is None:
            continue
        package_ref = _short_id_from_package_id(package["package_id"]) or package["ref"]
        session_id = str(package["manifest"].get("session_id") or "")
        created_at = str(package["manifest"].get("created_at") or "")

        def _add(axis: str, value: str) -> None:
            cleaned = " ".join(str(value or "").split())
            if cleaned:
                rows.append((package_ref, session_id, axis, cleaned, created_at))

        for axis in _LABEL_SINGLE_AXES:
            found = node.find(f".//{axis}")
            if found is not None:
                _add(axis, "".join(found.itertext()))
        for axis, xpath in _LABEL_MULTI_AXES:
            for found in node.findall(xpath):
                _add(axis, "".join(found.itertext()))
        self_signal = node.find(".//self_signal")
        if self_signal is not None:
            _add("self_signal", self_signal.get("present") or "true")
        for nutrient in node.findall(".//nutrient"):
            _add("nutrient_plane", nutrient.get("plane") or "")
        milestone = node.find(".//milestone_signal")
        if milestone is not None:
            _add("milestone_criteria", milestone.get("criteria") or "unspecified")
        # J2 growth-report axes: failure-mode recurrence/avoidance + rework
        for signal in node.findall(".//failure_signal"):
            ref = (signal.get("ref") or "").strip()
            outcome = (signal.get("outcome") or "").strip().lower()
            if ref and outcome in {"recurred", "avoided"}:
                _add(f"failure_mode_{outcome}", ref)
        rework = node.find(".//rework")
        if rework is not None and (rework.get("present") or "").strip().lower() == "true":
            _add("rework", "true")
    return rows


def _parse_labels_block(markdown: str) -> ET.Element | None:
    match = re.search(r"<t2_labels\b.*?</t2_labels>", markdown, re.DOTALL)
    if match is None:
        return None
    try:
        return ET.fromstring(match.group(0))
    except ET.ParseError as exc:
        logger.warning("Reference index skipped unparseable t2_labels block: %s", exc)
        return None


DEBT_HISTORY_RELATIVE = Path("control") / "consolidation_debt_history.jsonl"


def _debt_history_rows(root: Path, agent_id: uuid.UUID | str) -> list[tuple]:
    """Replay the append-only debt observation log into the derived table."""
    history_path = root / str(agent_id) / "memory" / DEBT_HISTORY_RELATIVE
    if not history_path.exists():
        return []
    rows: list[tuple] = []
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("Reference index skipped unreadable debt history %s: %s", history_path, exc)
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            logger.warning("Reference index skipped malformed debt history line for %s", agent_id)
            continue
        if not isinstance(record, dict) or not record.get("assessed_at"):
            continue
        rows.append(
            (
                str(record.get("assessed_at")),
                int(record.get("pending_packages") or 0),
                int(record.get("pending_stitch_packages") or 0),
                record.get("oldest_pending_age_hours"),
                int(record.get("held_jobs") or 0),
                int(record.get("exhausted_jobs") or 0),
                int(record.get("active_explicit_entries") or 0),
                record.get("oldest_explicit_age_hours"),
                1 if record.get("stalled") else 0,
                json.dumps(record.get("stall_reasons") or [], ensure_ascii=False),
            )
        )
    return rows


_ACTIVATION_AXIS_ALIASES = {
    "categories": "category",
    "concepts": "concept",
    "target_hints": "target_hint",
    "statuses": "status",
    "names": "name",
    "risk_flags": "risk_flag",
    "aliases": "alias",
    "tags": "tag",
}


def _activation_key_rows(
    root: Path,
    agent_id: uuid.UUID | str,
    *,
    label_rows: list[tuple[str, str, str, str, str]] | None = None,
) -> list[tuple[str, str, str, str, str, str, float, str]]:
    rows: list[tuple[str, str, str, str, str, str, float, str]] = []
    rows.extend(_legacy_t2_activation_key_rows(label_rows or []))
    rows.extend(_t3_activation_key_rows(root, agent_id))
    for entry in load_explicit_overlay_entries(root, agent_id):
        if entry.status != "active":
            continue
        rows.extend(_flatten_activation_keys(build_explicit_overlay_activation_keys(entry)))
    return rows


def _legacy_t2_activation_key_rows(
    label_rows: list[tuple[str, str, str, str, str]],
) -> list[tuple[str, str, str, str, str, str, float, str]]:
    rows: list[tuple[str, str, str, str, str, str, float, str]] = []
    for package_ref, _session_id, axis, value, created_at in label_rows:
        cleaned_ref = str(package_ref or "").strip()
        cleaned_axis = str(axis or "").strip()
        cleaned_value = str(value or "").strip()
        if not cleaned_ref or not cleaned_axis or not cleaned_value:
            continue
        rows.append(
            (
                f"agent_memory:t2_package:{cleaned_ref}",
                "agent_memory",
                "t2_package",
                cleaned_axis,
                cleaned_value,
                cleaned_ref,
                0.55,
                str(created_at or ""),
            )
        )
    return rows


def _t3_activation_key_rows(root: Path, agent_id: uuid.UUID | str) -> list[tuple[str, str, str, str, str, str, float, str]]:
    rows: list[tuple[str, str, str, str, str, str, float, str]] = []
    for entry in list_profile_entries(root, agent_id):
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id:
            continue
        source = str(entry.get("source") or "").strip()
        rows.extend(
            _flatten_activation_keys(
                {
                    "candidate_kind": "agent_memory",
                    "candidate_ref": {
                        "candidate_id": f"agent_memory:t3_profile:{entry_id}",
                        "kind": "agent_memory",
                        "source_type": "t3_profile",
                    },
                    "key_features": {
                        "entry_id": [entry_id],
                        "heading": [str(entry.get("heading") or "").strip()],
                        "aliases": entry.get("aliases") or [],
                        "tags": entry.get("tags") or [],
                        "lifecycle": [str(entry.get("lifecycle") or "active").strip()],
                    },
                    "source_refs": [source],
                },
                confidence=0.7,
            )
        )
    for page in list_knowledge_pages(root, agent_id):
        page_id = str(page.get("id") or "").strip()
        if not page_id:
            continue
        source = str(page.get("source") or "").strip()
        scope = "t3_milestone" if page.get("kind") == "milestone" else "t3_knowledge"
        rows.extend(
            _flatten_activation_keys(
                {
                    "candidate_kind": "agent_memory",
                    "candidate_ref": {
                        "candidate_id": f"agent_memory:{scope}:{page_id}",
                        "kind": "agent_memory",
                        "source_type": scope,
                    },
                    "key_features": {
                        "page_id": [page_id],
                        "title": [str(page.get("title") or page_id).strip()],
                        "aliases": page.get("aliases") or [],
                        "tags": page.get("tags") or [],
                        "lifecycle": [str(page.get("lifecycle") or "active").strip()],
                    },
                    "source_refs": [source],
                },
                confidence=0.7,
            )
        )
    return rows


def _flatten_activation_keys(keys: dict, *, confidence: float = 1.0) -> list[tuple[str, str, str, str, str, str, float, str]]:
    candidate_ref = keys.get("candidate_ref") if isinstance(keys.get("candidate_ref"), dict) else {}
    candidate_id = str(candidate_ref.get("candidate_id") or "").strip()
    candidate_kind = str(keys.get("candidate_kind") or candidate_ref.get("kind") or "").strip()
    scope = str(candidate_ref.get("source_type") or candidate_ref.get("kind") or "").strip()
    if not candidate_id or not candidate_kind or not scope:
        return []

    key_features = keys.get("key_features") if isinstance(keys.get("key_features"), dict) else {}
    created_at = str(key_features.get("created_at") or keys.get("created_at") or "").strip()
    source_refs = [str(ref).strip() for ref in keys.get("source_refs") or () if str(ref).strip()] or [""]
    rows: list[tuple[str, str, str, str, str, str, float, str]] = []
    for raw_axis, raw_values in key_features.items():
        axis = _activation_axis(raw_axis)
        if axis in {"created_at"}:
            continue
        for value in _activation_values(raw_values):
            for source_ref in source_refs:
                rows.append((candidate_id, candidate_kind, scope, axis, value, source_ref, confidence, created_at))
    return rows


def _activation_axis(raw_axis: object) -> str:
    axis = str(raw_axis or "").strip()
    return _ACTIVATION_AXIS_ALIASES.get(axis, axis)


def _activation_values(raw_values: object) -> list[str]:
    if raw_values is None:
        return []
    if isinstance(raw_values, list | tuple | set | frozenset):
        values = list(raw_values)
    else:
        values = [raw_values]
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            text = str(value or "").strip()
        if text:
            result.append(text)
    return result


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


def _short_id_from_package_id(package_id: str) -> str:
    """`t2pkg-<hash>` → the spec §4.1 short evidence id `t2-<hash>`."""
    cleaned = (package_id or "").strip()
    if cleaned.startswith("t2pkg-"):
        return "t2-" + cleaned.removeprefix("t2pkg-")
    return ""


def _page_resolution_rows(root: Path, agent_id: uuid.UUID | str) -> list[tuple[str, str, str, str]]:
    """Register knowledge/milestone page slugs for navigation resolution."""
    mem_dir = root / str(agent_id) / "memory"
    rows: list[tuple[str, str, str, str]] = []
    for subdir, kind in (("milestones", "milestone"), ("knowledge", "knowledge")):
        directory = mem_dir / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            rows.append((path.stem, kind, f"memory/{subdir}/{path.name}", ""))
    return rows


def _explicit_resolution_rows(root: Path, agent_id: uuid.UUID | str) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for entry in load_explicit_overlay_entries(Path(root), agent_id):
        rows.append((entry.entry_id, "explicit_entry", f"memory/explicit/entries/{entry.entry_id}.md", ""))
    return rows


def _tombstone_rows(root: Path, agent_id: uuid.UUID | str) -> list[tuple[str, str, str]]:
    """Rebuild the tombstone projection from its append-only jsonl truth."""
    log_path = root / str(agent_id) / "memory" / TOMBSTONE_LOG_RELATIVE
    if not log_path.exists():
        return []
    latest: dict[str, tuple[str, str, str]] = {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        old_id = str(record.get("old_id") or "").strip()
        new_id = str(record.get("new_id") or "").strip()
        if old_id and new_id:
            latest[old_id] = (old_id, new_id, str(record.get("job_id") or ""))
    return list(latest.values())


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
