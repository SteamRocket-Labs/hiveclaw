# Web Research Source Quality

Use source quality rules whenever facts may be current, disputed, or important
to a user decision.

## Source Preference

1. Primary source: official docs, filings, press releases, standards, source repositories.
2. Reputable secondary source: established media, analyst reports, academic publishers.
3. Aggregators: useful for discovery, not final evidence.
4. Anonymous posts: only use as anecdotal signals and label them clearly.

## Fetch Escalation

1. `web_search` to discover current candidate sources with built-in basic search.
2. `advanced_web_search` when basic results are weak, sparse, stale, vertical, or need result content.
3. `web_fetch` for known URLs or top results.
4. `advanced_web_fetch` when direct fetch is incomplete, blocked, JS-heavy, or needs alternate extraction.
5. Provider-specific override only when a route is clear: `firecrawl_search`, `firecrawl_fetch`, `tavily_extract`, `exa_fetch`, or `anysearch_extract`.
6. `xcrawl_scrape` for difficult pages only when configured. XCrawl only when configured.

## Citation Rules

- Cite the URL for every material claim.
- Include dates for time-sensitive facts.
- Separate verified facts from inference.
- Do not fill gaps with memory when search was required.
