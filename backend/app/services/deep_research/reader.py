from __future__ import annotations

from urllib.parse import urlparse

from app.services.deep_research.schemas import SearchCandidate, SourceRecord, SourceType
from app.services.deep_research.searcher import ToolInvoker


class ResearchReader:
    def __init__(self, tool_invoker: ToolInvoker, *, max_chars: int = 24000):
        self.tool_invoker = tool_invoker
        self.max_chars = max(2000, max_chars)

    async def fetch_candidate(self, candidate: SearchCandidate, *, source_type: SourceType = SourceType.UNKNOWN) -> SourceRecord | None:
        for tool_name in ("web_fetch", "firecrawl_fetch", "xcrawl_scrape"):
            content = await self.tool_invoker(tool_name, {"url": candidate.url, "max_chars": self.max_chars})
            text = _normalize_content(content)
            if _has_usable_content(text):
                return SourceRecord(
                    source_id="",
                    url=candidate.url,
                    title=_extract_title(text) or candidate.title or candidate.url,
                    publisher=_publisher_from_url(candidate.url),
                    source_type=source_type,
                    content=text[: self.max_chars],
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
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    for prefix in ("title:", "#"):
        if first_line.lower().startswith(prefix):
            return first_line[len(prefix):].strip()
    return first_line[:120]


def _publisher_from_url(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")
