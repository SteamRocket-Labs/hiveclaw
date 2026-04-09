"""Helpers for weighted T2 episodic memory stored in markdown files."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


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
    r"(?:\[w=(?P<weight>\d+(?:\.\d+)?)\])?"
    r"(?:\[src=(?P<source>[^\]]+)\])?"
    r"(?:\[cat=(?P<category>[^\]]+)\])?"
    r"\s+(?P<content>.+?)\s*$"
)
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


def format_t2_entry(
    *,
    category: str,
    content: str,
    source: str,
    timestamp: str | None = None,
    weight: float | None = None,
) -> str:
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    final_weight = compute_t2_weight(category, source) if weight is None else round(weight, 2)
    normalized_source = (source or "runtime").strip().lower() or "runtime"
    normalized_category = (category or "general").strip().lower() or "general"
    return f"- [{ts}][w={final_weight:.2f}][src={normalized_source}][cat={normalized_category}] {content.strip()}"


def parse_t2_entry_line(line: str, *, fallback_category: str | None = None, fallback_source: str | None = None) -> dict | None:
    match = _ENTRY_RE.match(line.strip())
    if not match:
        return None

    category = (match.group("category") or fallback_category or "general").strip().lower()
    source = (match.group("source") or fallback_source or "runtime").strip().lower()
    weight_raw = match.group("weight")
    weight = compute_t2_weight(category, source) if weight_raw is None else round(float(weight_raw), 2)

    return {
        "timestamp": match.group("timestamp").strip(),
        "weight": weight,
        "source": source,
        "category": category,
        "content": match.group("content").strip(),
    }


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
        grouped.setdefault(t2_target_file(category), []).append(
            format_t2_entry(
                category=category,
                content=content,
                source=source,
                timestamp=timestamp,
            )
        )

    for filename, lines in grouped.items():
        path = root / filename
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else f"{T2_FILE_HEADERS[filename]}\n"
        existing_contents = {
            _normalize_content(parsed["content"])
            for parsed in (
                parse_t2_entry_line(line, fallback_category="general")
                for line in existing.splitlines()
            )
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
        written += len(new_lines)

    return written


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
    for bucket_name, title in (("high", "## High Priority"), ("medium", "## Medium Priority"), ("low", "## Low Priority")):
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
            lines.append(
                "- "
                f"[{entry.get('timestamp', '?')}]"
                f"[w={float(entry.get('weight', 0.0)):.2f}]"
                f"[repeat={int(entry.get('repeat', 1))}]"
                f"[src={entry.get('source', 'runtime')}]"
                f"[cat={entry.get('category', 'general')}] "
                f"{entry.get('content', '').strip()}"
            )
        lines.append("")

    return "\n".join(lines).strip()
