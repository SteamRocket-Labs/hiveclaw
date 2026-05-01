---
name: Topic Deep Dive
description: Source-attributed deep research workflow for a bounded question, issue, product, company, or policy topic.
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

# Topic Deep Dive

Use this skill when the user asks for a careful research answer that needs
fresh source collection, citation tracking, contradiction checks, and a
deliverable research packet.

## Operating Contract

1. Define the question, scope, geography, time window, and exclusion rules.
2. Search broadly with `web_search`, then fetch primary or high-quality sources
   with `web_fetch`.
3. Use `firecrawl_fetch` or `xcrawl_scrape` only when the normal fetch path is
   blocked or the tenant has configured the paid provider.
4. Maintain a source ledger while researching. Every material claim in the final
   answer must point back to a fetched source.
5. For broad tasks, use `delegate_to_agent` only for bounded side work such as
   one geography, one competitor set, or one source class.
6. Write a Markdown packet with `write_file` when the user asks for a reusable
   artifact; send it with `send_channel_file` when channel delivery is needed.

## Output Standard

- Executive answer first.
- Evidence table with source title, publisher, date, URL, and how it was used.
- Separate confirmed facts from inference.
- Include contradictions, stale data warnings, and unresolved questions.
- Do not cite search result snippets as facts. Fetch the source first.

## Stop Conditions

- A source is paywalled and no configured provider can fetch it.
- The question requires private, customer, legal, medical, or investment advice
  beyond available evidence.
- The source set is too weak to support the requested confidence level.

