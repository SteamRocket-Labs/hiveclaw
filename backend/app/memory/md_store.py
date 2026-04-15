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


# Similarity thresholds for detecting near-duplicate T3 entries / skills.
# Uses max(word-token Jaccard, char-bigram Jaccard) so both English and
# Chinese paraphrases are caught. Thresholds are tuned empirically:
#   - English paraphrase "user likes short replies" vs "user prefers short replies" → ~0.45-0.55
#   - Chinese paraphrase "用户偏好简短回复" vs "用户喜欢简短回复" → char-bigram ~0.30-0.45
#   - Genuinely distinct facts → typically <0.10
# Since the handler returns a soft "[Skipped]" hint (not a hard error), the LLM
# can re-phrase and retry, so we err on catching more paraphrases.
MEMORY_DEDUP_THRESHOLD = 0.45
SKILL_DEDUP_THRESHOLD = 0.50


def _token_set(text: str) -> frozenset[str]:
    normalized = _normalize_entry_content(text)
    if not normalized:
        return frozenset()
    return frozenset(t for t in re.split(r"\W+", normalized) if t)


def _char_bigram_set(text: str) -> frozenset[str]:
    """Character-level bigrams — robust to CJK (no whitespace tokenization)."""
    stripped = re.sub(r"\s+", "", _normalize_entry_content(text))
    if len(stripped) < 2:
        return frozenset([stripped] if stripped else [])
    return frozenset(stripped[i : i + 2] for i in range(len(stripped) - 1))


def jaccard_similarity(a: str, b: str) -> float:
    """Jaccard similarity — returns the MAX of word-token and char-bigram scores.

    Word tokens catch English paraphrases ("user likes" vs "the user likes")
    where word boundaries are reliable. Character bigrams catch Chinese near-
    duplicates ("用户偏好简短回复" vs "用户喜欢简短回复") where whitespace
    tokenization produces near-empty intersection. Taking the max means
    either script pattern can trigger dedup.
    """
    tokens_a = _token_set(a)
    tokens_b = _token_set(b)
    if tokens_a and tokens_b:
        token_score = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    else:
        token_score = 0.0

    bigrams_a = _char_bigram_set(a)
    bigrams_b = _char_bigram_set(b)
    if bigrams_a and bigrams_b:
        bigram_score = len(bigrams_a & bigrams_b) / len(bigrams_a | bigrams_b)
    else:
        bigram_score = 0.0

    return max(token_score, bigram_score)


def find_similar_t3_entries(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    content: str,
    category: str | None = None,
    threshold: float = MEMORY_DEDUP_THRESHOLD,
    limit: int = 3,
) -> list[dict]:
    """Return T3 facts whose Jaccard similarity to `content` exceeds threshold.

    When `category` is given, limits to facts routed to the same T3 file
    (so 'feedback' content is only compared against feedback.md entries).
    """
    if not content.strip():
        return []
    target_filename: str | None = None
    if category:
        spec = t3_spec_for_category(category)
        target_filename = spec["filename"]

    hits: list[tuple[float, dict]] = []
    mem_dir = ensure_t3_layout(data_root, agent_id)
    for spec in T3_FILE_SPECS:
        if target_filename and spec["filename"] != target_filename:
            continue
        path = mem_dir / spec["filename"]
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        for line in extract_entry_lines(body):
            existing_content, timestamp = parse_entry_line(line)
            if not existing_content:
                continue
            sim = jaccard_similarity(content, existing_content)
            if sim >= threshold:
                hits.append((sim, {
                    "content": existing_content,
                    "category": spec["shadow_category"],
                    "timestamp": timestamp or "",
                    "similarity": round(sim, 3),
                }))

    hits.sort(key=lambda item: item[0], reverse=True)
    return [fact for _sim, fact in hits[:limit]]


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


# ── T3 format validator + self-healing (PR-9) ──
#
# heartbeat writes T3 via LLM following HEARTBEAT.md's "- [YYYY-MM-DD] desc"
# rule, but nothing stops the model from drifting to `* desc`, `1. desc`, or
# dateless bullets. dream's parser (extract_entry_lines) only recognizes
# `- [` / `- ` prefixes, so any drifted row becomes invisible — dedup misses
# it, soul promotion misses it, and the cap-by-count cleanup misses it.
# This module re-canonicalizes what it can and surfaces what it can't.

_STAR_BULLET_RE = re.compile(r"^\*\s+(?P<content>.+?)\s*$")
_NUMBERED_BULLET_RE = re.compile(r"^\d+\.\s+(?P<content>.+?)\s*$")
_DASH_WITH_DATE_RE = re.compile(r"^- \[\d{4}-\d{2}-\d{2}[^\]]*\]")
_DASH_BARE_RE = re.compile(r"^- (?P<content>.+?)\s*$")
_T3_HEADER_PREFIXES = tuple(spec["header"] for spec in T3_FILE_SPECS)


def validate_and_normalize_t3(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    recent_window_seconds: int = 3600,
) -> dict:
    """Auto-fix drifted T3 lines; flag lines we can't safely repair.

    Rewrites are purely syntactic (bullet marker, missing date); semantic
    content is never altered. Files untouched inside `recent_window_seconds`
    are skipped so the common no-op case is cheap.

    Returns:
      {
        "fixed": int,                 # lines rewritten
        "warnings": list[str],        # unfixable plain-text lines (truncated)
        "files_touched": list[str],   # filenames with any change
      }
    """
    import time as _time

    report: dict = {"fixed": 0, "warnings": [], "files_touched": []}
    mem_dir = memory_dir(data_root, agent_id)
    if not mem_dir.exists():
        return report

    now_ts = _time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for spec in T3_FILE_SPECS:
        path = mem_dir / spec["filename"]
        if not path.exists():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if now_ts - mtime > recent_window_seconds:
            continue

        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        new_lines: list[str] = []
        fixed_in_file = 0
        for raw_line in original.splitlines():
            stripped = raw_line.rstrip()
            if not stripped:
                new_lines.append(raw_line)
                continue

            # Preserve file headers verbatim.
            if stripped.startswith(_T3_HEADER_PREFIXES) or stripped.startswith("##"):
                new_lines.append(raw_line)
                continue

            # Already canonical "- [date] ..." — keep.
            if _DASH_WITH_DATE_RE.match(stripped):
                new_lines.append(raw_line)
                continue

            # "* content" → "- [today] content"
            star = _STAR_BULLET_RE.match(stripped)
            if star:
                new_lines.append(f"- [{today}] {star.group('content').strip()}")
                fixed_in_file += 1
                continue

            # "1. content" / "2. content" → "- [today] content"
            numbered = _NUMBERED_BULLET_RE.match(stripped)
            if numbered:
                new_lines.append(f"- [{today}] {numbered.group('content').strip()}")
                fixed_in_file += 1
                continue

            # "- content" without date → "- [today] content"
            dash_bare = _DASH_BARE_RE.match(stripped)
            if dash_bare:
                content = dash_bare.group("content").strip()
                if len(content) >= 10:
                    new_lines.append(f"- [{today}] {content}")
                    fixed_in_file += 1
                    continue
                # Too short to canonicalize safely — keep original.
                new_lines.append(raw_line)
                continue

            # Plain text line that isn't a bullet or heading — can't repair.
            report["warnings"].append(f"{spec['filename']}: {stripped[:100]}")
            new_lines.append(raw_line)

        if fixed_in_file:
            new_content = "\n".join(new_lines)
            if not new_content.endswith("\n"):
                new_content += "\n"
            path.write_text(new_content, encoding="utf-8")
            report["fixed"] += fixed_in_file
            report["files_touched"].append(spec["filename"])

    return report


