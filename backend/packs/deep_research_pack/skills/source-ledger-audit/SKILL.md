---
name: Source Ledger Audit
description: Audit a research draft against its source ledger and flag unsupported claims, stale sources, and citation gaps.
tools:
  - read_file
  - web_fetch
  - firecrawl_fetch
  - xcrawl_scrape
  - write_file
  - edit_file
  - send_channel_file
metadata:
  version: "0.1"
  category: research
---

# Source Ledger Audit

Use this skill when the user already has a draft, report, memo, or research
packet and wants evidence quality checked before sharing it.

## Audit Steps

1. Load the draft and source ledger with `read_file`.
2. Re-fetch material sources with `web_fetch` when URLs are available.
3. Use `firecrawl_fetch` or `xcrawl_scrape` only if configured and normal fetch
   is blocked.
4. Mark each material claim as supported, partially supported, contradicted, or
   unsupported.
5. Write an audit report with `write_file`. Use `edit_file` only if the user
   asks to patch the original draft.

## Finding Categories

- Unsupported claim.
- Source does not say what the draft claims.
- Stale source for a time-sensitive point.
- Missing publisher, date, or URL.
- Contradiction between sources.
- Citation overreach.

## Output Standard

Lead with blocking findings, then provide a claim table. Keep line references or
section names when available so the author can fix the draft quickly.

