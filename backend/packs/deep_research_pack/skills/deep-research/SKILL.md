---
name: Deep Research
description: Single source-attributed deep research entrypoint for topic, industry, and source-ledger audit workflows.
tools:
  - web_search
  - web_fetch
  - firecrawl_fetch
  - xcrawl_scrape
  - read_file
  - write_file
  - edit_file
  - delegate_to_agent
  - send_channel_file
metadata:
  version: "0.2"
  category: research
  hive.pack: deep_research_pack
---

# Deep Research

Use this skill as the only Deep Research entrypoint. Pick one internal mode:

- `topic_deep_dive`: bounded question, company, product, issue, policy, or event.
- `industry_research`: market map, value chain, competitors, regulation, and adoption signals.
- `source_ledger_audit`: audit an existing draft against its source ledger.

## Workflow

1. State the research question, scope, time window, geography, and exclusion rules.
2. Collect primary or high-quality sources first. Use `web_search` for discovery, then fetch source pages with `web_fetch`.
3. Use `firecrawl_fetch` or `xcrawl_scrape` only when normal fetch is blocked or the tenant configured those providers.
4. Keep a source ledger while researching. Every material claim must map to a fetched source, not a search snippet.
5. Use `delegate_to_agent` only for independent bounded collection lanes.
6. Write a reusable packet with `write_file` when requested, and deliver with `send_channel_file` only when channel delivery is needed.

## Output Standard

- Executive answer first.
- Source ledger with publisher, date, URL, and claim usage.
- Separate confirmed facts, inference, contradictions, stale data, and unresolved questions.
- For audits, lead with blocking citation gaps and unsupported claims.
