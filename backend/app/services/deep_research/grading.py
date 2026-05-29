"""Deterministic, domain-general evidence grading for fetched sources.

Borrowed (domain-general) from academic-research-skills `source_quality_hierarchy`:
every source carries a tier + an A–F grade so the synthesizer can weight claims by
evidence quality instead of treating a blog equal to a regulator filing
(anti-pattern: "source tier inflation").

This is deterministic (no LLM, instant) so it does NOT reintroduce the per-source
LLM cost that made runs slow. The mapping keys off the already-inferred SourceType
(domain-neutral) plus light domain heuristics for UNKNOWN sources. Tiers are
adaptive in meaning per domain: Tier 1 = primary/authoritative (official filings,
regulator, datasets, peer-reviewed, on-chain), down to Tier 4 = weak (blog/social).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.services.deep_research.schemas import SourceType

# Known low-authority hosts → Tier 4 unless an explicit SourceType says otherwise.
_WEAK_DOMAINS: tuple[str, ...] = (
    "medium.com",
    "substack.com",
    "x.com",
    "twitter.com",
    "reddit.com",
    "t.me",
    "telegram.org",
    "telegram.me",
    "blogspot.",
    "wordpress.com",
    "facebook.com",
    "quora.com",
    "tumblr.com",
    "weibo.com",
    "zhihu.com",
    "bilibili.com",
    "youtube.com",
)
# Authoritative TLD/host fragments → Tier 1 even when SourceType is UNKNOWN.
_AUTHORITATIVE_FRAGMENTS: tuple[str, ...] = (
    ".gov",
    ".gov.",
    ".edu",
    ".edu.",
    ".int",
    "europa.eu",
    ".mil",
    ".ac.",
)
# Domain-general authoritative institutions (international bodies, standards, academic publishers).
# Deliberately NOT a domain-specific allowlist — these are universal high-authority hosts.
_AUTHORITATIVE_DOMAINS: tuple[str, ...] = (
    "imf.org",
    "worldbank.org",
    "un.org",
    "who.int",
    "oecd.org",
    "bis.org",
    "wto.org",
    "iea.org",
    "arxiv.org",
    "doi.org",
    "ssrn.com",
    "nature.com",
    "science.org",
)


def _domain(record: Any) -> str:
    publisher = str(getattr(record, "publisher", "") or "").casefold().strip()
    if publisher and "." in publisher:
        return publisher.removeprefix("www.")
    netloc = urlparse(str(getattr(record, "url", "") or "")).netloc.casefold()
    return netloc.removeprefix("www.")


def grade_source(record: Any) -> tuple[str, str]:
    """Return (tier, grade) for a SourceRecord. tier in tier1..tier4, grade in A..F."""
    source_type = getattr(record, "source_type", SourceType.UNKNOWN)
    if source_type in (SourceType.PRIMARY, SourceType.REGULATORY):
        return "tier1", "A"
    if source_type in (SourceType.DATASET, SourceType.TECHNICAL):
        return "tier2", "B"
    if source_type == SourceType.SECONDARY:
        return "tier3", "C"

    domain = _domain(record)
    if domain and (
        any(fragment in domain for fragment in _AUTHORITATIVE_FRAGMENTS)
        or any(known in domain for known in _AUTHORITATIVE_DOMAINS)
    ):
        return "tier1", "B"
    if domain and any(weak in domain for weak in _WEAK_DOMAINS):
        return "tier4", "D"
    return "tier3", "C"
