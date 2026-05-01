---
name: Industry Research
description: Market and industry research workflow covering market structure, value chain, competitors, regulation, and adoption signals.
tools:
  - web_search
  - web_fetch
  - firecrawl_fetch
  - xcrawl_scrape
  - read_file
  - write_file
  - delegate_to_agent
  - send_channel_file
metadata:
  version: "0.1"
  category: research
---

# Industry Research

Use this skill for industry maps, market landscape research, sector briefings,
competitor scans, and customer-facing research packets.

## Workflow

1. Frame the industry boundary and segment taxonomy.
2. Collect primary sources first: regulator data, company reports, association
   data, filings, standards, pricing pages, and product docs.
3. Use `web_search` to discover sources and `web_fetch` to ground claims.
4. Use `delegate_to_agent` for parallel collection only when the subtasks are
   independent, such as one region, one competitor group, or one customer
   segment.
5. Produce a market map, key players table, demand drivers, constraints,
   regulation, and a source ledger.

## Required Sections

- Market definition and segmentation.
- Value chain and buyer groups.
- Competitor or provider landscape.
- Demand drivers and adoption blockers.
- Regulation and risk.
- Evidence ledger with source dates.

## Quality Bar

- Prefer data with dates, methodology, and publisher identity.
- Label estimates and ranges clearly.
- Separate market facts from strategic interpretation.
- Do not fabricate market size if the source set is weak.

