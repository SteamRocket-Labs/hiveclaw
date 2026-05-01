# Web Research Source Quality

Use source quality rules whenever facts may be current, disputed, or important
to a user decision.

## Source Preference

1. Primary source: official docs, filings, press releases, standards, source repositories.
2. Reputable secondary source: established media, analyst reports, academic publishers.
3. Aggregators: useful for discovery, not final evidence.
4. Anonymous posts: only use as anecdotal signals and label them clearly.

## Fetch Escalation

1. `web_search` to discover current candidate sources.
2. `web_fetch` for known URLs or top results.
3. `firecrawl_fetch` when pages are JS-heavy, PDF-like, or incomplete.
4. `xcrawl_scrape` for difficult pages when available.

## Citation Rules

- Cite the URL for every material claim.
- Include dates for time-sensitive facts.
- Separate verified facts from inference.
- Do not fill gaps with memory when search was required.
