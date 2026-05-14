from __future__ import annotations

from urllib.parse import urlparse

from app.services.deep_research.extractor import clean_fetched_text
from app.services.deep_research.schemas import SearchCandidate, SourceRecord, SourceType
from app.services.deep_research.searcher import ToolInvoker


_DEFAULT_FETCH_CHAIN: tuple[str, ...] = ("jina_reader_fetch", "web_fetch", "firecrawl_fetch", "xcrawl_scrape")


class ResearchReader:
    def __init__(
        self,
        tool_invoker: ToolInvoker,
        *,
        max_chars: int = 200_000,
        fetch_chain: tuple[str, ...] | None = None,
    ):
        # Tier 3-3: default lifted from 24K → 200K so Jina Reader full-page markdown is
        # not artificially clipped. Synthesis still chooses its own per-call excerpt size.
        self.tool_invoker = tool_invoker
        self.max_chars = max(2000, max_chars)
        self.fetch_chain = fetch_chain or _DEFAULT_FETCH_CHAIN

    async def fetch_candidate(self, candidate: SearchCandidate, *, source_type: SourceType = SourceType.UNKNOWN) -> SourceRecord | None:
        for tool_name in self.fetch_chain:
            try:
                content = await self.tool_invoker(tool_name, {"url": candidate.url, "max_chars": self.max_chars})
            except Exception:
                # Tier 3-3: tool registry may not carry every fetch backend in every deployment;
                # fall through to the next tier rather than failing the lane.
                continue
            text = _normalize_content(content)
            cleaned = clean_fetched_text(text)
            if _has_usable_content(cleaned):
                return SourceRecord(
                    source_id="",
                    url=candidate.url,
                    title=_extract_title(text) or candidate.title or candidate.url,
                    publisher=_publisher_from_url(candidate.url),
                    source_type=source_type,
                    content=cleaned[: self.max_chars],
                    lane_id=candidate.lane_id,
                    query=candidate.query,
                    fetch_tool=tool_name,
                )
        return None


def _normalize_content(content: str) -> str:
    return "\n".join(line.strip() for line in (content or "").splitlines() if line.strip())


def _has_usable_content(text: str) -> bool:
    if len(text) < 80:
        return False
    lowered = text.lower()
    return not lowered.startswith(("❌", "[error]", "error:", "web_fetch failed"))


def _extract_title(text: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if "fetched content from:" in lowered:
            continue
        for prefix in ("title:", "#"):
            if lowered.startswith(prefix):
                return line[len(prefix):].strip()
        return line[:120]
    return ""


def _publisher_from_url(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")
