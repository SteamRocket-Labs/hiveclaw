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
`topic_deep_dive`, `industry_research`, or `source_ledger_audit`.

Every material claim must map to a fetched source. Use `web_search` for
discovery, `web_fetch` for evidence, and heavier fetch tools only when needed.
