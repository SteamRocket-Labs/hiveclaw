---
name: Industry Research
description: "Use when Codex needs to build an industry landscape, market map, competitor structure, adoption signal review, regulatory scan, and source-attributed research report."
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
  version: '0.1'
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

## Bundled Resources

Load resources by need, not by default:

- `references/playbook.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/report.md`: use as the output scaffold when creating this artifact type.

## Anti-patterns

- Do not treat a search result, filename, or prior memory as proof without reading the underlying source or file.
- Do not load every reference file by default; use progressive disclosure and read only the relevant resource.
- Do not call destructive or externally visible tools unless the user asked for that action and required confirmation is satisfied.

## Examples

- Input: "Create the requested artifact from these notes." Output: inspect the inputs, load the relevant template/reference, call the declared tools, save the artifact, and report validation notes.
- Input: "Check whether this is safe / supported / current." Output: gather evidence first, classify unsupported or stale claims, and give a direct recommendation with source or file references.
