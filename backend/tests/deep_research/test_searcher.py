from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_searcher_dedupes_canonical_urls_and_enforces_host_cap():
    from app.services.deep_research.schemas import ResearchLane, SearchQuery
    from app.services.deep_research.searcher import ResearchSearcher

    calls: list[tuple[str, dict]] = []

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        calls.append((tool_name, arguments))
        return """
        1. https://example.com/report?utm_source=news
        2. https://example.com/report#summary
        3. https://example.com/second
        4. https://another.example/rwa
        """

    searcher = ResearchSearcher(fake_tool, host_cap=1)
    results = await searcher.search_lane(
        ResearchLane(
            lane_id="official",
            label="Official sources",
            goal="Find official RWA sources",
            queries=[
                SearchQuery(query="RWA market report 2026"),
                SearchQuery(query="RWA market report 2026"),
            ],
        ),
        max_results=5,
    )

    assert calls == [("web_search", {"query": "RWA market report 2026"})]
    assert [item.url for item in results] == [
        "https://example.com/report",
        "https://another.example/rwa",
    ]
    assert all(item.discovery_only for item in results)


def test_canonicalize_url_removes_tracking_and_fragments():
    from app.services.deep_research.searcher import canonicalize_url

    assert canonicalize_url("https://example.com/a?utm_campaign=x&b=2#top") == "https://example.com/a?b=2"
    assert canonicalize_url("https://example.com/a/") == "https://example.com/a"
