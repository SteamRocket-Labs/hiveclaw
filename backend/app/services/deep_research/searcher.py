from __future__ import annotations

import re
from collections import Counter
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.services.deep_research.schemas import ResearchLane, SearchCandidate


ToolInvoker = Callable[[str, dict], Awaitable[str]]
_URL_PATTERN = re.compile(r"https?://[^\s<>)\"']+")
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref"}


class ResearchSearcher:
    def __init__(self, tool_invoker: ToolInvoker, *, host_cap: int = 3):
        self.tool_invoker = tool_invoker
        self.host_cap = max(1, host_cap)

    async def search_lane(self, lane: ResearchLane, *, max_results: int) -> list[SearchCandidate]:
        seen_queries: set[str] = set()
        seen_urls: set[str] = set()
        host_counts: Counter[str] = Counter()
        candidates: list[SearchCandidate] = []

        for search_query in lane.queries:
            query = search_query.query.strip()
            query_key = query.casefold()
            if not query or query_key in seen_queries:
                continue
            seen_queries.add(query_key)
            raw = await self.tool_invoker("web_search", {"query": query})
            for raw_url in _extract_urls(raw):
                url = canonicalize_url(raw_url)
                if not url or url in seen_urls:
                    continue
                host = urlparse(url).netloc.casefold()
                if host_counts[host] >= self.host_cap:
                    continue
                seen_urls.add(url)
                host_counts[host] += 1
                candidates.append(
                    SearchCandidate(
                        url=url,
                        lane_id=lane.lane_id,
                        query=query,
                        rank=len(candidates) + 1,
                        discovery_only=True,
                    )
                )
                if len(candidates) >= max_results:
                    return candidates
        return candidates


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip().rstrip(".,);]"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.startswith(_TRACKING_PREFIXES) and key not in _TRACKING_KEYS
    ]
    path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", urlencode(query_pairs), ""))


def _extract_urls(raw: str) -> list[str]:
    return _URL_PATTERN.findall(raw or "")
