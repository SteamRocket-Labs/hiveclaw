# Topic Deep Dive Reference

## Source Quality Ladder

Prefer sources in this order:

1. Primary documents, official filings, regulator pages, court records, company
   releases, standards, and original datasets.
2. Reputable specialist publications with clear authorship and dates.
3. Established news sources with named reporting.
4. Aggregators and summaries only for discovery, not final proof.

## Research Loop

1. Plan: write the exact question, date sensitivity, and likely source classes.
2. Discover: use `web_search` for multiple query phrasings.
3. Fetch: use `web_fetch` for every candidate source before treating it as
   evidence.
4. Escalate: use `firecrawl_fetch` or `xcrawl_scrape` if a configured provider
   can retrieve cleaner page content.
5. Ledger: track source title, URL, publisher, date, fetched status, and claims.
6. Synthesize: compare claims, call out contradictions, and mark inference.

## Citation Rules

- Never rely on a title or snippet alone.
- Never hide missing dates. If a source has no date, mark it explicitly.
- Keep claims narrow. If the source says revenue grew in one quarter, do not
  generalize to a long-term trend without more evidence.

