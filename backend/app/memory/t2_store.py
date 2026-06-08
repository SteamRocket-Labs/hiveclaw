"""Helpers for weighted T2 episodic memory stored in markdown files."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from app.memory.lifecycle_store import record_active_memory_lifecycle
from app.memory.types import CONTAINER_CANDIDATES
from app.memory.write_gate import prepare_memory_write


T2_FILE_MAP: dict[str, str] = {
    "feedback": "insights.md",
    "user": "insights.md",
    "reference": "insights.md",
    "error": "errors.md",
    "project": "insights.md",
    "request": "requests.md",
    "constraint": "insights.md",
    "strategy": "insights.md",
    "blocked_pattern": "errors.md",
    "general": "insights.md",
}

T2_FILE_HEADERS: dict[str, str] = {
    "insights.md": "# Insights",
    "errors.md": "# Errors",
    "requests.md": "# Requests",
}

_ENTRY_RE = re.compile(
    r"^- \[(?P<timestamp>[^\]]+)\]"
    r"(?P<meta>(?:\[[^\]]+\])*)"
    r"\s+(?P<content>.+?)\s*$"
)
_META_RE = re.compile(r"\[(?P<key>[a-zA-Z_]+)=(?P<value>[^\]]*)\]")
_WHITESPACE_RE = re.compile(r"\s+")

_HUMAN_SOURCES = {
    "web",
    "websocket",
    "runtime",
    "gateway",
    "feishu",
    "slack",
    "wecom",
    "dingtalk",
    "teams",
    "discord",
    "discord_bot",
    "compaction",
    "invoke_complete",
    # Backfill replays originally-human sessions from behavior T0 MD files;
    # the provenance is still the same user conversation, just delayed.
    "t0_backfill",
}
_AUTONOMOUS_SOURCES = {
    "trigger",
    "delegation",
    "scheduler",
    "cron",
    "interval",
    "poll",
    "event",
}

_SOURCE_BUCKET_WEIGHTS: dict[str, dict[str, float]] = {
    "human": {
        "feedback": 1.00,
        "constraint": 1.00,
        "reference": 0.80,
        "strategy": 0.80,
        "error": 0.70,
        "blocked_pattern": 0.70,
        "user": 0.60,
        "project": 0.60,
        "request": 0.30,
        "general": 0.40,
    },
    "autonomous": {
        "feedback": 0.70,
        "constraint": 0.70,
        "reference": 0.50,
        "strategy": 0.50,
        "error": 0.70,
        "blocked_pattern": 0.70,
        "user": 0.50,
        "project": 0.50,
        "request": 0.30,
        "general": 0.40,
    },
    "system": {
        "feedback": 0.85,
        "constraint": 0.85,
        "reference": 0.65,
        "strategy": 0.65,
        "error": 0.70,
        "blocked_pattern": 0.70,
        "user": 0.55,
        "project": 0.55,
        "request": 0.30,
        "general": 0.40,
    },
}

HIGH_PRIORITY_THRESHOLD = 0.85
MEDIUM_PRIORITY_THRESHOLD = 0.50


def t2_dir(data_root: Path, agent_id: uuid.UUID) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / "learnings"


def ensure_t2_layout(data_root: Path, agent_id: uuid.UUID) -> Path:
    root = t2_dir(data_root, agent_id)
    root.mkdir(parents=True, exist_ok=True)
    for filename, header in T2_FILE_HEADERS.items():
        path = root / filename
        if not path.exists():
            path.write_text(f"{header}\n", encoding="utf-8")
    return root


def t2_target_file(category: str) -> str:
    return T2_FILE_MAP.get(category, "insights.md")


def _source_bucket(source: str | None) -> str:
    normalized = (source or "runtime").strip().lower()
    if normalized in _AUTONOMOUS_SOURCES:
        return "autonomous"
    if normalized in _HUMAN_SOURCES:
        return "human"
    return "system"


def compute_t2_weight(category: str, source: str | None) -> float:
    normalized_category = (category or "general").strip().lower()
    bucket = _source_bucket(source)
    weight = _SOURCE_BUCKET_WEIGHTS[bucket].get(normalized_category, _SOURCE_BUCKET_WEIGHTS[bucket]["general"])
    return round(weight, 2)


def _normalize_content(content: str) -> str:
    return _WHITESPACE_RE.sub(" ", content).strip().lower()


def _clamp_score(value: float | str | None, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    return round(min(1.0, max(0.0, score)), 2)


def _normalize_refs(source_refs: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if source_refs is None:
        return []
    if isinstance(source_refs, str):
        raw = source_refs.split(",")
    else:
        raw = list(source_refs)
    return [str(ref).strip() for ref in raw if str(ref).strip()]


def _parse_meta(meta: str) -> dict[str, str]:
    return {match.group("key").strip().lower(): match.group("value").strip() for match in _META_RE.finditer(meta or "")}


def format_t2_entry(
    *,
    category: str,
    content: str,
    source: str,
    timestamp: str | None = None,
    weight: float | None = None,
    evidence: str | None = None,
    confidence: float | str | None = None,
    volatility: str | None = None,
    source_refs: list[str] | tuple[str, ...] | str | None = None,
    novelty: float | str | None = None,
    reusability: float | str | None = None,
    concept: str | None = None,
    container_candidate: str | None = None,
    discovery_tokens: int | str | None = None,
    metadata: dict[str, str] | None = None,
) -> str:
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    final_weight = compute_t2_weight(category, source) if weight is None else round(weight, 2)
    normalized_source = (source or "runtime").strip().lower() or "runtime"
    normalized_category = (category or "general").strip().lower() or "general"
    meta_parts = [
        f"[w={final_weight:.2f}]",
        f"[src={normalized_source}]",
        f"[cat={normalized_category}]",
    ]
    for key, value in (metadata or {}).items():
        if key and value and key not in {"evidence_refs", "concept", "discovery_tokens", "container_candidate"}:
            meta_parts.append(f"[{key}={value}]")
    normalized_concept = _sanitize_meta(str(concept or (metadata or {}).get("concept") or "")).strip().lower()
    if normalized_concept:
        meta_parts.append(f"[concept={normalized_concept}]")
    normalized_container = str(container_candidate or (metadata or {}).get("container_candidate") or "").strip().lower()
    if normalized_container in CONTAINER_CANDIDATES:
        meta_parts.append(f"[container={normalized_container}]")
    token_count = _positive_int(discovery_tokens or (metadata or {}).get("discovery_tokens"))
    if token_count is not None:
        meta_parts.append(f"[discovery_tokens={token_count}]")
    normalized_evidence = (evidence or "").strip().lower()
    if normalized_evidence:
        meta_parts.append(f"[ev={normalized_evidence}]")
    confidence_score = _clamp_score(confidence)
    if confidence_score is not None:
        meta_parts.append(f"[conf={confidence_score:.2f}]")
    normalized_volatility = (volatility or "").strip().lower()
    if normalized_volatility:
        meta_parts.append(f"[vol={normalized_volatility}]")
    refs = _normalize_refs(source_refs)
    if refs:
        meta_parts.append(f"[refs={','.join(refs)}]")
    novelty_score = _clamp_score(novelty)
    if novelty_score is not None:
        meta_parts.append(f"[nov={novelty_score:.2f}]")
    reusability_score = _clamp_score(reusability)
    if reusability_score is not None:
        meta_parts.append(f"[reuse={reusability_score:.2f}]")
    return f"- [{ts}]{''.join(meta_parts)} {content.strip()}"


def parse_t2_entry_line(
    line: str, *, fallback_category: str | None = None, fallback_source: str | None = None
) -> dict | None:
    match = _ENTRY_RE.match(line.strip())
    if not match:
        return None

    metadata = _parse_meta(match.group("meta") or "")
    category = (metadata.get("cat") or fallback_category or "general").strip().lower()
    source = (metadata.get("src") or fallback_source or "runtime").strip().lower()
    weight_raw = metadata.get("w")
    weight = compute_t2_weight(category, source) if weight_raw is None else round(float(weight_raw), 2)
    evidence = (metadata.get("ev") or metadata.get("evidence") or "").strip().lower()
    confidence = _clamp_score(metadata.get("conf"))
    volatility = (metadata.get("vol") or metadata.get("volatility") or "").strip().lower()
    source_refs = _normalize_refs(metadata.get("refs") or metadata.get("source_refs"))
    novelty = _clamp_score(metadata.get("nov") or metadata.get("novelty"))
    reusability = _clamp_score(metadata.get("reuse") or metadata.get("reusability"))
    concept = (metadata.get("concept") or "").strip().lower()
    container_candidate = (metadata.get("container") or metadata.get("container_candidate") or "").strip().lower()
    discovery_tokens = _positive_int(metadata.get("discovery_tokens"))

    parsed = {
        "timestamp": match.group("timestamp").strip(),
        "weight": weight,
        "source": source,
        "category": category,
        "content": match.group("content").strip(),
    }
    if evidence:
        parsed["evidence"] = evidence
    if confidence is not None:
        parsed["confidence"] = confidence
    if volatility:
        parsed["volatility"] = volatility
    if source_refs:
        parsed["source_refs"] = source_refs
    if novelty is not None:
        parsed["novelty"] = novelty
    if reusability is not None:
        parsed["reusability"] = reusability
    if concept:
        parsed["concept"] = concept
    if container_candidate in CONTAINER_CANDIDATES:
        parsed["container_candidate"] = container_candidate
    if discovery_tokens is not None:
        parsed["discovery_tokens"] = discovery_tokens
    for key in (
        "entry_id",
        "sensitivity",
        "status",
        "version",
        "parent_id",
        "supersedes",
        "superseded_by",
        "expires_at",
        "access_count",
        "last_accessed",
        "retention_score",
        "reaction",
        "polarity",
        "feedback_source",
        "rationale_from_owner",
        "decision_ref",
        "absorbed_at",
    ):
        if metadata.get(key):
            parsed[key] = metadata[key]
    return parsed


def append_t2_entries(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    extractions: list[dict[str, str]],
    source: str,
    timestamp: str | None = None,
) -> int:
    if not extractions:
        return 0

    root = ensure_t2_layout(data_root, agent_id)
    written = 0
    grouped: dict[str, list[str]] = {}

    for extraction in extractions:
        category = extraction.get("category", "general")
        content = (extraction.get("content") or "").strip()
        if not content:
            continue
        source_refs = extraction.get("source_refs") or extraction.get("refs")
        decision = prepare_memory_write(
            content,
            category=category,
            evidence_refs=source_refs,
            parent_id=extraction.get("parent_id"),
            supersedes=extraction.get("supersedes"),
            superseded_by=extraction.get("superseded_by"),
            expires_at=extraction.get("expires_at"),
        )
        if decision.rejected:
            continue
        metadata = {**decision.metadata, **_feedback_metadata(extraction)}
        if extraction.get("concept"):
            metadata["concept"] = _sanitize_meta(str(extraction["concept"]))
        if extraction.get("discovery_tokens"):
            metadata["discovery_tokens"] = str(_positive_int(extraction.get("discovery_tokens")) or 0)
        grouped.setdefault(t2_target_file(category), []).append(
            format_t2_entry(
                category=decision.category,
                content=decision.content,
                source=source,
                timestamp=timestamp,
                evidence=extraction.get("evidence") or extraction.get("ev"),
                confidence=extraction.get("confidence") or extraction.get("conf"),
                volatility=extraction.get("volatility") or extraction.get("vol"),
                source_refs=source_refs,
                novelty=extraction.get("novelty") or extraction.get("nov"),
                reusability=extraction.get("reusability") or extraction.get("reuse"),
                concept=extraction.get("concept"),
                container_candidate=extraction.get("container_candidate") or extraction.get("container"),
                discovery_tokens=extraction.get("discovery_tokens"),
                metadata=metadata,
            )
        )

    for filename, lines in grouped.items():
        path = root / filename
        existing = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else f"{T2_FILE_HEADERS[filename]}\n"
        )
        existing_contents = {
            _normalize_content(parsed["content"])
            for parsed in (parse_t2_entry_line(line, fallback_category="general") for line in existing.splitlines())
            if parsed
        }
        new_lines: list[str] = []
        for line in lines:
            parsed = parse_t2_entry_line(line)
            if not parsed:
                continue
            normalized = _normalize_content(parsed["content"])
            if normalized in existing_contents:
                continue
            existing_contents.add(normalized)
            new_lines.append(line)

        if not new_lines:
            continue

        updated = existing.rstrip()
        if updated:
            updated += "\n"
        updated += "\n".join(new_lines) + "\n"
        path.write_text(updated, encoding="utf-8")
        for line in new_lines:
            parsed = parse_t2_entry_line(line)
            if parsed and parsed.get("entry_id"):
                record_active_memory_lifecycle(
                    data_root,
                    agent_id,
                    content=parsed["content"],
                    metadata={str(key): str(value) for key, value in parsed.items() if value is not None},
                )
        written += len(new_lines)

    return written


def _feedback_metadata(extraction: dict[str, str]) -> dict[str, str]:
    if (extraction.get("category") or "").strip().lower() != "feedback":
        return {}
    keys = ("reaction", "polarity", "feedback_source", "rationale_from_owner", "decision_ref")
    return {key: _sanitize_meta(str(extraction[key])) for key in keys if extraction.get(key)}


def _sanitize_meta(value: str) -> str:
    return " ".join(value.replace("[", "(").replace("]", ")").split())


def _positive_int(value: int | str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _infer_category_from_file(filename: str) -> str:
    if filename == "errors.md":
        return "error"
    if filename == "requests.md":
        return "request"
    return "general"


def load_t2_entries(data_root: Path, agent_id: uuid.UUID) -> tuple[list[dict], dict[str, float]]:
    root = t2_dir(data_root, agent_id)
    if not root.exists():
        return [], {}

    entries: list[dict] = []
    mtimes: dict[str, float] = {}

    for filename in ("insights.md", "errors.md", "requests.md"):
        path = root / filename
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            mtimes[filename] = path.stat().st_mtime
        except OSError:
            continue

        fallback_category = _infer_category_from_file(filename)
        for raw_line in text.splitlines():
            parsed = parse_t2_entry_line(raw_line, fallback_category=fallback_category)
            if not parsed:
                continue
            parsed["file"] = filename
            entries.append(parsed)

    counts = Counter(_normalize_content(entry["content"]) for entry in entries)
    for entry in entries:
        entry["repeat"] = counts[_normalize_content(entry["content"])]
    return entries, mtimes


def load_incremental_t2_entries(
    data_root: Path,
    agent_id: uuid.UUID,
    known_mtimes: dict[str, float] | None,
) -> tuple[list[dict], dict[str, float]]:
    all_entries, current_mtimes = load_t2_entries(data_root, agent_id)
    if not current_mtimes:
        return [], {}

    changed_files = {
        filename
        for filename, mtime in current_mtimes.items()
        if filename not in (known_mtimes or {}) or mtime > float((known_mtimes or {}).get(filename, 0.0))
    }
    if not changed_files:
        return [], current_mtimes

    selected: list[dict] = []
    for filename in changed_files:
        file_entries = [entry for entry in all_entries if entry.get("file") == filename]
        selected.extend(file_entries[-10:])
    return selected, current_mtimes


def render_t2_snapshot(entries: list[dict]) -> str:
    if not entries:
        return ""

    def _bucket(weight: float) -> str:
        if weight >= HIGH_PRIORITY_THRESHOLD:
            return "high"
        if weight >= MEDIUM_PRIORITY_THRESHOLD:
            return "medium"
        return "low"

    grouped: dict[str, list[dict]] = {"high": [], "medium": [], "low": []}
    for entry in entries:
        grouped[_bucket(float(entry.get("weight", 0.0)))].append(entry)

    lines: list[str] = []
    for bucket_name, title in (
        ("high", "## High Priority"),
        ("medium", "## Medium Priority"),
        ("low", "## Low Priority"),
    ):
        bucket_entries = grouped[bucket_name]
        if not bucket_entries:
            continue
        lines.append(title)
        ordered = sorted(
            bucket_entries,
            key=lambda entry: (
                float(entry.get("weight", 0.0)),
                int(entry.get("repeat", 1)),
                str(entry.get("timestamp", "")),
            ),
            reverse=True,
        )
        for entry in ordered:
            extra_meta = ""
            if entry.get("concept"):
                extra_meta += f"[concept={entry.get('concept')}]"
            if entry.get("container_candidate"):
                extra_meta += f"[container={entry.get('container_candidate')}]"
            if entry.get("discovery_tokens") is not None:
                extra_meta += f"[discovery_tokens={int(entry.get('discovery_tokens', 0))}]"
            lines.append(
                "- "
                f"[{entry.get('timestamp', '?')}]"
                f"[w={float(entry.get('weight', 0.0)):.2f}]"
                f"[repeat={int(entry.get('repeat', 1))}]"
                f"[src={entry.get('source', 'runtime')}]"
                f"[cat={entry.get('category', 'general')}]"
                f"{extra_meta} "
                f"{entry.get('content', '').strip()}"
            )
        lines.append("")

    return "\n".join(lines).strip()


# ── Retention / reversible archival ────────────────────────────


def mark_t2_entries_absorbed(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    filenames: list[str] | tuple[str, ...] | None = None,
    absorbed_at: str | None = None,
) -> int:
    """Mark active T2 rows as absorbed after heartbeat has consumed them."""
    root = t2_dir(data_root, agent_id)
    if not root.exists():
        return 0

    day = absorbed_at or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    changed = 0
    target_filenames = tuple(filenames or T2_FILE_HEADERS.keys())
    for filename in target_filenames:
        if filename not in T2_FILE_HEADERS:
            continue
        path = root / filename
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        updated_lines: list[str] = []
        file_changed = False
        for line in lines:
            parsed = parse_t2_entry_line(line, fallback_category=_infer_category_from_file(filename))
            if not parsed or parsed.get("status") == "absorbed":
                updated_lines.append(line)
                continue
            updated_lines.append(_rewrite_t2_entry_metadata(line, {"status": "absorbed", "absorbed_at": day}))
            changed += 1
            file_changed = True
        if file_changed:
            path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")
    return changed


def archive_absorbed_t2_entries(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    keep_per_file: int = 10,
    min_age_days: int = 30,
    archived_at: str | None = None,
) -> int:
    """Archive absorbed T2 rows into memory/archive.md.

    Active rows are never archived. Absorbed rows may be archived by age/cap
    even if downstream summaries mention them; archive rows preserve the
    original T2 line, entry id, source file, and original timestamp so evidence
    remains recoverable without letting consumed T2 grow forever.
    """
    root = t2_dir(data_root, agent_id)
    if not root.exists():
        return 0

    archived_total = 0
    archive_day = archived_at or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for filename in T2_FILE_HEADERS:
        path = root / filename
        if not path.exists():
            continue
        original_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        entry_indices = [
            index
            for index, line in enumerate(original_lines)
            if parse_t2_entry_line(line, fallback_category=_infer_category_from_file(filename))
        ]
        if not entry_indices:
            continue

        absorbed_indices = [
            index
            for index in entry_indices
            if _is_absorbed_t2_entry(original_lines[index], fallback_category=_infer_category_from_file(filename))
        ]
        archive_indices: set[int] = set()

        if min_age_days > 0:
            archive_indices.update(
                index
                for index in absorbed_indices
                if _t2_entry_age_days(original_lines[index]) >= min_age_days
            )

        remaining_absorbed = [index for index in absorbed_indices if index not in archive_indices]
        if keep_per_file >= 0 and len(remaining_absorbed) > keep_per_file:
            archive_indices.update(remaining_absorbed[: len(remaining_absorbed) - keep_per_file])

        if not archive_indices:
            continue

        archived_lines = [original_lines[index] for index in sorted(archive_indices)]
        kept_lines = [line for index, line in enumerate(original_lines) if index not in archive_indices]
        path.write_text("\n".join(kept_lines).rstrip() + "\n", encoding="utf-8")
        archived_total += _append_t2_archive_rows(
            data_root,
            agent_id,
            filename=filename,
            lines=archived_lines,
            reason="t2_retention_cap",
            archived_at=archive_day,
        )

    return archived_total


def _rewrite_t2_entry_metadata(line: str, updates: dict[str, str]) -> str:
    match = _ENTRY_RE.match(line.strip())
    if not match:
        return line

    ordered_keys: list[str] = []
    metadata: dict[str, str] = {}
    for meta_match in _META_RE.finditer(match.group("meta") or ""):
        key = meta_match.group("key").strip().lower()
        if key not in metadata:
            ordered_keys.append(key)
        metadata[key] = meta_match.group("value").strip()
    for key, value in updates.items():
        normalized_key = key.strip().lower()
        if normalized_key not in metadata:
            ordered_keys.append(normalized_key)
        metadata[normalized_key] = _sanitize_meta(str(value))

    meta = "".join(f"[{key}={metadata[key]}]" for key in ordered_keys if metadata.get(key))
    return f"- [{match.group('timestamp').strip()}]{meta} {match.group('content').strip()}"


def _is_absorbed_t2_entry(line: str, *, fallback_category: str) -> bool:
    parsed = parse_t2_entry_line(line, fallback_category=fallback_category)
    return bool(parsed and parsed.get("status") == "absorbed")


def _t2_entry_age_days(line: str) -> int:
    parsed = parse_t2_entry_line(line)
    if not parsed:
        return 0
    try:
        ts = date.fromisoformat(str(parsed.get("timestamp", ""))[:10])
    except ValueError:
        return 0
    return max((datetime.now(timezone.utc).date() - ts).days, 0)


def _append_t2_archive_rows(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    filename: str,
    lines: list[str],
    reason: str,
    archived_at: str,
) -> int:
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return 0

    memory_root = Path(data_root) / str(agent_id) / "memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    archive_path = memory_root / "archive.md"
    existing = (
        archive_path.read_text(encoding="utf-8", errors="replace")
        if archive_path.exists()
        else "# Memory Archive\n\nRetired entries — de-indexed from active recall, preserved as evidence.\n"
    )
    if "## T2 Retention Archive" not in existing:
        existing = existing.rstrip() + "\n\n## T2 Retention Archive\n"

    rows: list[str] = []
    for line in cleaned:
        parsed = parse_t2_entry_line(line, fallback_category=_infer_category_from_file(filename))
        if not parsed:
            continue
        meta_parts = [
            f"[from=learnings/{filename}]",
            f"[reason={_sanitize_meta(reason)}]",
        ]
        if parsed.get("entry_id"):
            meta_parts.append(f"[entry_id={_sanitize_meta(str(parsed['entry_id']))}]")
        if parsed.get("timestamp"):
            meta_parts.append(f"[orig_date={_sanitize_meta(str(parsed['timestamp']))}]")
        rows.append(f"- [{archived_at}]{''.join(meta_parts)} {line}")

    if not rows:
        return 0
    archive_path.write_text(existing.rstrip() + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return len(rows)
