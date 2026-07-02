"""Helpers for MD-first T3 memory files and shadow index rebuilding."""

from __future__ import annotations

import re
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_T3_XML_BLOCK_RE = re.compile(
    r"<(?P<tag>t3_[A-Za-z0-9_]+)\b[^>]*>.*?</(?P=tag)>|<(?P<tag_self>t3_[A-Za-z0-9_]+)\b[^>]*/>",
    re.DOTALL,
)
_CJK_RANGE_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")


@dataclass(frozen=True, slots=True)
class ParsedT3XmlBlock:
    block_id: str
    content: str
    metadata: dict[str, str]
    raw: str


def memory_dir(data_root: Path, agent_id: uuid.UUID) -> Path:
    return Path(data_root) / str(agent_id) / "memory"


TWO_PLANE_DIRS: tuple[str, ...] = ("self", "profiles", "knowledge", "milestones")


def ensure_t3_layout(data_root: Path, agent_id: uuid.UUID) -> Path:
    mem_dir = memory_dir(data_root, agent_id)
    mem_dir.mkdir(parents=True, exist_ok=True)
    # Legacy flat-T3 four-file skeleton retired at the C7 cutover; existing
    # files are reorganized by app.scripts.migrate_memory_two_planes.
    # Two-plane layout (spec §1.1/§5): profile plane (self/profiles) +
    # knowledge plane (knowledge/milestones). Directories only — files are
    # created by governed writers (Platform Gate / HR bootstrap), not here.
    for subdir in TWO_PLANE_DIRS:
        (mem_dir / subdir).mkdir(parents=True, exist_ok=True)
    return mem_dir


def extract_t3_xml_blocks(content: str) -> list[str]:
    return [match.group(0).strip() for match in _T3_XML_BLOCK_RE.finditer(content or "")]


def parse_t3_xml_block(block: str) -> ParsedT3XmlBlock | None:
    raw = (block or "").strip()
    if not raw:
        return None
    try:
        import xml.etree.ElementTree as ET

        node = ET.fromstring(raw)
    except Exception:
        return None
    attrs = {str(key): str(value) for key, value in node.attrib.items() if value is not None}
    block_id = attrs.get("id") or _stable_entry_id("t3_xml_block", raw)
    source_refs = [(ref.text or "").strip() for ref in node.findall(".//source_ref") if (ref.text or "").strip()]
    if source_refs:
        attrs["source_refs"] = ",".join(source_refs)
        attrs.setdefault("evidence_refs", ",".join(source_refs))
    text = _xml_block_content(node)
    return ParsedT3XmlBlock(
        block_id=block_id,
        content=text or block_id,
        metadata=attrs,
        raw=raw,
    )


def _xml_block_content(node) -> str:
    texts: list[str] = []
    priority_tags = (
        "title",
        "claim",
        "rule",
        "name",
        "scene_context",
        "what_happened",
        "why_it_matters",
        "when_to_use",
        "applies_when",
    )
    for tag in priority_tags:
        found = node.find(f".//{tag}")
        if found is not None and "".join(found.itertext()).strip():
            texts.append(" ".join("".join(found.itertext()).split()))
    if not texts:
        texts.append(" ".join(" ".join(node.itertext()).split()))
    return " ".join(part for part in texts if part).strip()


def _normalize_entry_content(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip().lower()


def _stable_entry_id(filename: str, content: str) -> str:
    normalized = _normalize_entry_content(content)
    digest = hashlib.sha256(f"{filename}\0{normalized}".encode("utf-8")).hexdigest()[:16]
    return f"mem_{digest}"


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


def list_retirement_candidates(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    limit: int = 10,
    protected_markers: list[str] | None = None,
) -> list[dict]:
    """Retired at the C7 cutover: entry-level decay retirement was a flat-T3
    mechanism. The convergence loop (工序 4) now owns pruning — retired
    entries are marked by the gate and physically removed by governed
    full-file rewrites. Dream receives an empty shortlist."""
    del data_root, agent_id, limit, protected_markers
    return []


def rebuild_index(data_root: Path, agent_id: uuid.UUID) -> Path:
    """Regenerate memory/indexes/wiki_map.md over the two-plane layout.

    Navigation artifact only (spec: wiki_map is a generated map, not prompt
    memory): profile-plane entries (id/heading/source) + knowledge-plane pages
    (slug/title/status/relations count). Rebuilt after every gate commit.
    """
    from app.memory.plane_read import list_knowledge_pages, list_profile_entries

    mem_dir = ensure_t3_layout(data_root, agent_id)
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Memory Wiki Map",
        f"Updated: {updated_at}",
        "",
        "## Profile Plane",
        "",
        "| Entry ID | Heading | File |",
        "|----------|---------|------|",
    ]
    profile_entries = list_profile_entries(data_root, agent_id)
    for entry in profile_entries:
        lines.append(f"| {entry['id']} | {entry['heading']} | {entry['source']} |")
    if not profile_entries:
        lines.append("| _none yet_ | | |")

    lines.extend(
        [
            "",
            "## Knowledge Plane",
            "",
            "| Page | Kind | Title | Status |",
            "|------|------|-------|--------|",
        ]
    )
    pages = list_knowledge_pages(data_root, agent_id)
    for page in pages:
        lines.append(f"| {page['id']} | {page['kind']} | {page['title']} | {page['status']} |")
    if not pages:
        lines.append("| _none yet_ | | | |")

    index_path = mem_dir / "indexes" / "wiki_map.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return index_path


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
