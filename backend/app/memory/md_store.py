"""Helpers for MD-first T3 memory files and shadow index rebuilding."""

from __future__ import annotations

import re
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.memory.lifecycle_store import record_active_memory_lifecycle


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

_CATEGORY_TO_SPEC = {category: spec for spec in T3_FILE_SPECS for category in spec["categories"]}

_ENTRY_WITH_DATE_RE = re.compile(
    r"^- \[(?P<timestamp>[^\]]+)\]"
    r"(?P<meta>(?:\[[^\]]+\])*)"
    r"\s+(?P<content>.+?)\s*$"
)
_ENTRY_BARE_RE = re.compile(r"^- (?P<content>.+?)\s*$")
_META_TOKEN_RE = re.compile(r"\[([^\]=]+)=([^\]]+)\]")


@dataclass(frozen=True, slots=True)
class ParsedMemoryEntry:
    content: str
    timestamp: str | None
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class T3MemoryEntry:
    entry_id: str
    content: str
    category: str
    timestamp: str
    metadata: dict[str, str]
    source: str
    filename: str
    load: str
    preview: str
    is_p0: bool


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


def parse_entry_record(line: str) -> ParsedMemoryEntry:
    match = _ENTRY_WITH_DATE_RE.match(line)
    if match:
        meta_raw = match.group("meta") or ""
        metadata = {
            key.strip(): value.strip()
            for key, value in _META_TOKEN_RE.findall(meta_raw)
            if key.strip() and value.strip()
        }
        return ParsedMemoryEntry(
            content=match.group("content").strip(),
            timestamp=match.group("timestamp").strip(),
            metadata=metadata,
        )

    match = _ENTRY_BARE_RE.match(line)
    if match:
        return ParsedMemoryEntry(content=match.group("content").strip(), timestamp=None, metadata={})

    return ParsedMemoryEntry(content=line.strip().lstrip("-").strip(), timestamp=None, metadata={})


def parse_entry_line(line: str) -> tuple[str, str | None]:
    record = parse_entry_record(line)
    return record.content, record.timestamp


def _normalize_entry_content(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip().lower()


def _stable_entry_id(filename: str, content: str) -> str:
    normalized = _normalize_entry_content(content)
    digest = hashlib.sha256(f"{filename}\0{normalized}".encode("utf-8")).hexdigest()[:16]
    return f"mem_{digest}"


def _entry_preview(content: str, max_chars: int = 160) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max(20, max_chars - 3)].rstrip() + "..."


def _escape_table_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


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
                hits.append(
                    (
                        sim,
                        {
                            "content": existing_content,
                            "category": spec["shadow_category"],
                            "timestamp": timestamp or "",
                            "similarity": round(sim, 3),
                        },
                    )
                )

    hits.sort(key=lambda item: item[0], reverse=True)
    return [fact for _sim, fact in hits[:limit]]


def append_t3_entry(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    category: str,
    content: str,
    timestamp: str | None = None,
    metadata: dict[str, str] | None = None,
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
    meta_text = "".join(f"[{key}={value}]" for key, value in (metadata or {}).items() if key and value)
    entry = f"- [{date_label}]{meta_text} {content.strip()}"
    updated = existing.rstrip()
    if updated:
        updated += "\n"
    updated += entry + "\n"
    path.write_text(updated, encoding="utf-8")
    if metadata and metadata.get("entry_id"):
        record_active_memory_lifecycle(
            data_root,
            agent_id,
            content=content.strip(),
            metadata={str(key): str(value) for key, value in metadata.items() if value is not None},
        )
    rebuild_index(data_root, agent_id)
    return path


def mark_t3_entry_promoted(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    entry_id: str,
    promoted_to: str,
    target: str = "",
) -> bool:
    """Stamp `[promoted_to=skill|workflow]` on the T3 line carrying entry_id.

    Spec §12 P4: promoted strategy entries keep their evidence in T3 but
    leave the candidate pool. Returns True when a line was stamped; False on
    missing entry, already-promoted entry, or invalid promoted_to value.
    """
    normalized_target_kind = (promoted_to or "").strip().lower()
    if normalized_target_kind not in {"skill", "workflow", "soul"}:
        return False
    needle = f"[entry_id={entry_id}]"

    mem_dir = memory_dir(data_root, agent_id)
    for spec in T3_FILE_SPECS:
        path = mem_dir / spec["filename"]
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            if needle not in line:
                continue
            if "[promoted_to=" in line:
                return False
            record = parse_entry_record(line)
            suffix = f"[promoted_to={normalized_target_kind}]"
            if target.strip():
                safe_target = " ".join(target.replace("[", "(").replace("]", ")").split())[:120]
                suffix += f"[promoted_target={safe_target}]"
            # Insert the markers right before the content, after existing metadata.
            content_start = line.find(record.content) if record.content else -1
            if content_start > 0:
                lines[index] = line[:content_start].rstrip() + suffix + " " + line[content_start:]
            else:
                lines[index] = line + " " + suffix
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            rebuild_index(data_root, agent_id)
            return True
    return False


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

    manifest = build_t3_entry_manifest(data_root, agent_id)
    lines.extend(
        [
            "",
            "## Entry Manifest",
            "",
            "| ID | File | Category | Date | Load | Preview |",
            "|----|------|----------|------|------|---------|",
        ]
    )
    for entry in manifest:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_table_cell(entry.entry_id),
                    _escape_table_cell(entry.filename),
                    _escape_table_cell(entry.category),
                    _escape_table_cell(entry.timestamp or "-"),
                    _escape_table_cell(entry.load),
                    _escape_table_cell(entry.preview),
                ]
            )
            + " |"
        )

    index_path = mem_dir / "INDEX.md"
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


def build_t3_entry_manifest(data_root: Path, agent_id: uuid.UUID) -> list[T3MemoryEntry]:
    mem_dir = ensure_t3_layout(data_root, agent_id)
    entries: list[T3MemoryEntry] = []
    for spec in T3_FILE_SPECS:
        path = mem_dir / spec["filename"]
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for line in extract_entry_lines(content):
            record = parse_entry_record(line)
            if not record.content:
                continue
            entry_id = record.metadata.get("entry_id") or _stable_entry_id(spec["filename"], record.content)
            timestamp = record.timestamp or ""
            source = f"memory/{spec['filename']}"
            entries.append(
                T3MemoryEntry(
                    entry_id=entry_id,
                    content=record.content,
                    category=spec["shadow_category"],
                    timestamp=timestamp,
                    metadata={**record.metadata, "entry_id": entry_id},
                    source=source,
                    filename=spec["filename"],
                    load=spec["load"],
                    preview=_entry_preview(record.content),
                    is_p0=spec["load"].startswith("P0"),
                )
            )
    return entries


def load_t3_entries_by_ids(data_root: Path, agent_id: uuid.UUID, ids: list[str]) -> list[T3MemoryEntry]:
    requested: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        entry_id = str(raw or "").strip()
        if entry_id and entry_id not in seen:
            requested.append(entry_id)
            seen.add(entry_id)
    if not requested:
        return []

    by_id = {entry.entry_id: entry for entry in build_t3_entry_manifest(data_root, agent_id)}
    return [by_id[entry_id] for entry_id in requested if entry_id in by_id]


def parse_t3_facts(data_root: Path, agent_id: uuid.UUID) -> list[dict]:
    facts: list[dict] = []
    for entry in build_t3_entry_manifest(data_root, agent_id):
        fact = {
            "id": entry.entry_id,
            "content": entry.content,
            "preview": entry.preview,
            "category": entry.category,
            "source": entry.source,
            "load": entry.load,
            "sensitivity": entry.metadata.get("sensitivity", "PL1_public"),
        }
        if entry.timestamp:
            fact["timestamp"] = entry.timestamp
        facts.append(fact)
    return facts


def search_t3_facts(
    data_root: Path,
    agent_id: uuid.UUID,
    query: str,
    *,
    limit: int = 5,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Search T3 MD files using BM25 scoring with optional temporal filtering.

    Args:
        date_from: ISO date string (YYYY-MM-DD). Only include facts on or after this date.
        date_to: ISO date string (YYYY-MM-DD). Only include facts on or before this date.
    """
    needle = (query or "").strip()

    all_facts = parse_t3_facts(data_root, agent_id)
    if not all_facts:
        return []

    # Apply temporal filter if specified
    if date_from or date_to:
        all_facts = _filter_facts_by_date(all_facts, date_from=date_from, date_to=date_to)

    if not needle:
        return all_facts[:limit]

    # Build corpus + tokenize
    corpus_tokens: list[list[str]] = []
    for fact in all_facts:
        content = str(fact.get("content", ""))
        # Include category as searchable text
        category = str(fact.get("category", ""))
        corpus_tokens.append(_bm25_tokenize(f"{category} {content}"))

    query_tokens = _bm25_tokenize(needle)
    if not query_tokens:
        return all_facts[:limit]

    # BM25 scoring
    scores = _bm25_score(query_tokens, corpus_tokens)

    ranked: list[tuple[float, int, dict]] = []
    for i, (score, fact) in enumerate(zip(scores, all_facts)):
        if score > 0:
            ranked.append((score, -i, fact))

    ranked.sort(reverse=True)
    return [fact for _score, _neg_index, fact in ranked[:limit]]


# ── Temporal filtering ──


def _filter_facts_by_date(
    facts: list[dict],
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Filter facts by their [YYYY-MM-DD] timestamp tag."""
    filtered: list[dict] = []
    for fact in facts:
        ts = (fact.get("timestamp") or "")[:10]  # "2026-04-16" portion
        if not ts or len(ts) < 10:
            # No date tag → include (don't drop undated facts silently)
            filtered.append(fact)
            continue
        if date_from and ts < date_from:
            continue
        if date_to and ts > date_to:
            continue
        filtered.append(fact)
    return filtered


# ── BM25 implementation (pure Python, zero dependencies) ──

_CJK_RANGE_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF]")


def _bm25_tokenize(text: str) -> list[str]:
    """Tokenize for BM25: word tokens + CJK character bigrams.

    Latin text is split on non-word boundaries. CJK text is converted to
    overlapping character bigrams (same strategy as jaccard_similarity).
    Both are lowercased and merged into one token list.
    """
    normalized = _normalize_entry_content(text)
    if not normalized:
        return []

    # Word tokens (works for English, numbers, mixed)
    word_tokens = [t for t in re.split(r"\W+", normalized) if t and len(t) > 1]

    # CJK bigrams
    cjk_chars = "".join(c for c in normalized if _CJK_RANGE_RE.match(c))
    bigrams = [cjk_chars[i : i + 2] for i in range(len(cjk_chars) - 1)] if len(cjk_chars) >= 2 else []

    return word_tokens + bigrams


def _bm25_score(
    query_tokens: list[str],
    corpus_tokens: list[list[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """BM25 scoring for a query against a pre-tokenized corpus.

    Returns one score per document. Standard BM25 with Okapi weighting.
    """
    import math

    n = len(corpus_tokens)
    if n == 0:
        return []

    # Average document length
    avg_dl = sum(len(doc) for doc in corpus_tokens) / n

    # Document frequency for each term
    df: dict[str, int] = {}
    for doc in corpus_tokens:
        seen: set[str] = set()
        for token in doc:
            if token not in seen:
                df[token] = df.get(token, 0) + 1
                seen.add(token)

    # IDF (with +0.5 smoothing to avoid negative values for very common terms)
    idf: dict[str, float] = {}
    for term, freq in df.items():
        idf[term] = math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)

    # Score each document
    scores: list[float] = []
    for doc in corpus_tokens:
        doc_len = len(doc)
        # Term frequency in this document
        tf: dict[str, int] = {}
        for token in doc:
            tf[token] = tf.get(token, 0) + 1

        score = 0.0
        for qt in query_tokens:
            if qt not in tf:
                continue
            term_freq = tf[qt]
            term_idf = idf.get(qt, 0.0)
            # BM25 TF component
            numerator = term_freq * (k1 + 1)
            denominator = term_freq + k1 * (1 - b + b * doc_len / avg_dl)
            score += term_idf * (numerator / denominator)

        scores.append(score)

    return scores


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
