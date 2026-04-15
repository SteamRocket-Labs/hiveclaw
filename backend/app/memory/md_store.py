"""Helpers for MD-first T3 memory files and shadow index rebuilding."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


T3_FILE_SPECS = (
    {
        "filename": "feedback.md",
        "header": "# Feedback",
        "categories": ("feedback", "constraint"),
        "load": "P0 always",
        "shadow_category": "feedback",
    },
    {
        "filename": "knowledge.md",
        "header": "# Knowledge",
        "categories": ("project", "reference", "general"),
        "load": "P1 on-demand",
        "shadow_category": "project",
    },
    {
        "filename": "strategies.md",
        "header": "# Strategies",
        "categories": ("strategy",),
        "load": "P1 on-demand",
        "shadow_category": "strategy",
    },
    {
        "filename": "blocked.md",
        "header": "# Blocked Patterns",
        "categories": ("blocked_pattern",),
        "load": "P0 always",
        "shadow_category": "blocked_pattern",
    },
    {
        "filename": "user.md",
        "header": "# User Profile",
        "categories": ("user",),
        "load": "P2 optional",
        "shadow_category": "user",
    },
)

_CATEGORY_TO_SPEC = {
    category: spec
    for spec in T3_FILE_SPECS
    for category in spec["categories"]
}

_ENTRY_WITH_DATE_RE = re.compile(r"^- \[(?P<timestamp>[^\]]+)\]\s+(?P<content>.+?)\s*$")
_ENTRY_BARE_RE = re.compile(r"^- (?P<content>.+?)\s*$")


def memory_dir(data_root: Path, agent_id: uuid.UUID) -> Path:
    return Path(data_root) / str(agent_id) / "memory"


def t3_spec_for_category(category: str) -> dict:
    return _CATEGORY_TO_SPEC.get(category, _CATEGORY_TO_SPEC["general"])


def ensure_t3_layout(data_root: Path, agent_id: uuid.UUID) -> Path:
    mem_dir = memory_dir(data_root, agent_id)
    mem_dir.mkdir(parents=True, exist_ok=True)
    for spec in T3_FILE_SPECS:
        path = mem_dir / spec["filename"]
        if not path.exists():
            path.write_text(f"{spec['header']}\n\n", encoding="utf-8")
    return mem_dir


def extract_entry_lines(content: str) -> list[str]:
    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("- [") or line.startswith("- "):
            lines.append(line)
    return lines


def parse_entry_line(line: str) -> tuple[str, str | None]:
    match = _ENTRY_WITH_DATE_RE.match(line)
    if match:
        return match.group("content").strip(), match.group("timestamp").strip()

    match = _ENTRY_BARE_RE.match(line)
    if match:
        return match.group("content").strip(), None

    return line.strip().lstrip("-").strip(), None


def _normalize_entry_content(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip().lower()


def append_t3_entry(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    category: str,
    content: str,
    timestamp: str | None = None,
) -> Path:
    spec = t3_spec_for_category(category)
    mem_dir = ensure_t3_layout(data_root, agent_id)
    path = mem_dir / spec["filename"]
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else f"{spec['header']}\n\n"
    normalized_new = _normalize_entry_content(content)
    for line in extract_entry_lines(existing):
        existing_content, _ = parse_entry_line(line)
        if _normalize_entry_content(existing_content) == normalized_new:
            rebuild_index(data_root, agent_id)
            return path

    date_label = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"- [{date_label}] {content.strip()}"
    updated = existing.rstrip()
    if updated:
        updated += "\n"
    updated += entry + "\n"
    path.write_text(updated, encoding="utf-8")
    rebuild_index(data_root, agent_id)
    return path


def rebuild_index(data_root: Path, agent_id: uuid.UUID) -> Path:
    mem_dir = ensure_t3_layout(data_root, agent_id)
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Memory Index",
        f"Updated: {updated_at}",
        "",
        "| File | Category | Items | Updated | Load |",
        "|------|----------|-------|---------|------|",
    ]

    for spec in T3_FILE_SPECS:
        path = mem_dir / spec["filename"]
        content = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        entries = extract_entry_lines(content)
        last_updated = "-"
        if entries:
            _entry_content, parsed_ts = parse_entry_line(entries[-1])
            last_updated = parsed_ts or updated_at.split(" ")[0]
        lines.append(
            f"| {spec['filename']} | {', '.join(spec['categories'])} | {len(entries)} | {last_updated} | {spec['load']} |"
        )

    index_path = mem_dir / "INDEX.md"
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


def parse_t3_facts(data_root: Path, agent_id: uuid.UUID) -> list[dict]:
    mem_dir = ensure_t3_layout(data_root, agent_id)
    facts: list[dict] = []
    for spec in T3_FILE_SPECS:
        path = mem_dir / spec["filename"]
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for line in extract_entry_lines(content):
            entry_content, timestamp = parse_entry_line(line)
            if not entry_content:
                continue
            fact = {
                "content": entry_content,
                "category": spec["shadow_category"],
            }
            if timestamp:
                fact["timestamp"] = timestamp
            facts.append(fact)
    return facts


def search_t3_facts(data_root: Path, agent_id: uuid.UUID, query: str, *, limit: int = 5) -> list[dict]:
    """Search T3 md files directly with token-frequency scoring."""
    needle = (query or "").strip()
    if not needle:
        return parse_t3_facts(data_root, agent_id)[:limit]

    normalized_query = _normalize_entry_content(needle)
    query_terms = [term for term in re.split(r"\s+", normalized_query) if term]
    ranked: list[tuple[int, int, dict]] = []

    for index, fact in enumerate(parse_t3_facts(data_root, agent_id)):
        content = str(fact.get("content", "")).strip()
        if not content:
            continue
        haystack = _normalize_entry_content(content)
        if normalized_query in haystack:
            score = 100 + len(query_terms)
        else:
            matched_terms = sum(1 for term in query_terms if term in haystack)
            if matched_terms == 0:
                continue
            score = matched_terms * 10

        # Prefer facts whose category/subject also overlaps with the query.
        bonus_source = " ".join(
            str(fact.get(key, "")).strip().lower()
            for key in ("category", "subject")
            if fact.get(key)
        )
        if bonus_source:
            overlap = Counter(query_terms) & Counter(re.split(r"\s+", bonus_source))
            score += sum(overlap.values()) * 2

        ranked.append((score, -index, fact))

    ranked.sort(reverse=True)
    return [fact for _score, _neg_index, fact in ranked[:limit]]


