---
name: Deep Research
description: "Use when Codex needs to run source-attributed deep research end to end, including scoping, source collection, claim ledger maintenance, contradiction analysis, and report production."
tools:
  - web_search
  - web_fetch
  - firecrawl_fetch
  - xcrawl_scrape
  - read_file
  - list_files
  - grep_search
  - glob_search
  - write_file
  - edit_file
  - delegate_to_agent
  - send_channel_file
metadata:
  version: '0.2'
  category: research
  hive.pack: deep_research_pack
---

# Deep Research

<role>
Use this skill when the user asks for serious research that must survive
source review: market reports, company/product deep dives, policy or event
analysis, industry maps, competitive landscapes, or audits of an existing
draft against its cited sources. The job is not to summarize search results;
the job is to build a defensible answer where every material claim traces to
retrieved evidence.
</role>

<when_to_use>
- The user asks for "deep research", "source-backed", "with citations", "market map", "industry research", "competitor research", "due diligence", or "audit this draft".
- The request has multiple factual claims, current facts, market sizing, regulatory facts, competitor positioning, or source quality risk.
- The user provides files or a draft and asks whether the claims are supported.
</when_to_use>

<do_not_use_when>
- The user wants a quick definition, rewrite, brainstorming pass, or uncited opinion.
- The answer depends on private systems that are not accessible through the available tools.
- The user explicitly requests no browsing and no file writes; then answer from available context and label limits.
</do_not_use_when>

## Mode Selection

Pick exactly one primary mode, then follow the shared workflow.

| Mode | Use When | Final Shape |
|---|---|---|
| `topic_deep_dive` | Bounded question, product, company, policy, event, or technical topic | Direct answer, findings, evidence ledger, contradictions, next checks |
| `industry_research` | Market landscape, value chain, competitors, adoption, regulation, funding, or GTM | Market map, segments, players, trends, risks, source-backed conclusions |
| `source_ledger_audit` | Existing draft, memo, deck, or claims list needs citation verification | Unsupported claims first, stale/weak sources, replacement sources, patch suggestions |

For detailed mode-specific checklists, read `references/playbooks.md` only
when the user asks for one of those modes or the research scope is large.

## Hard Rules

- Do not cite search snippets as evidence. Use `web_search` only for discovery; use `web_fetch`, `firecrawl_fetch`, or `xcrawl_scrape` to retrieve source content.
- Prefer primary sources: official filings, company pages, regulator pages, standards bodies, project docs, credible datasets, and original announcements.
- Label secondary sources as secondary. Use them for context, not as the sole basis for high-stakes claims.
- Every material claim in the final answer must have one of these ledger statuses: `verified`, `inferred`, `contradicted`, `stale`, or `unsupported`.
- Dates matter. Preserve publication dates and event dates separately when available.
- Do not hide uncertainty. Put contradictions, missing data, and weak evidence in their own section.
- Do not use `firecrawl_fetch` or `xcrawl_scrape` unless `web_fetch` is blocked, incomplete, or the page needs heavier extraction.
- Use `delegate_to_agent` only for independent lanes with clear boundaries, such as "collect regulator sources" or "map competitor pricing"; reconcile results yourself.
- Use `write_file` or `edit_file` only when the user requested a reusable report, packet, audit artifact, or update to an existing file.
- Use `send_channel_file` only when the user asks to deliver the artifact through the current channel.

## Tool Reference

| Need | Tool |
|---|---|
| Discover candidate sources | `web_search` |
| Fetch and verify source text | `web_fetch` |
| Recover blocked or dynamic source pages | `firecrawl_fetch`, `xcrawl_scrape` |
| Inspect local drafts, source ledgers, or prior artifacts | `read_file`, `list_files`, `grep_search`, `glob_search` |
| Save or update a report | `write_file`, `edit_file` |
| Split independent evidence collection | `delegate_to_agent` |
| Deliver a completed artifact | `send_channel_file` |

## End-to-End Workflow

### 1. Scope the research

Before collecting sources, write down the working scope in the response or
working notes:

- Research question and expected decision/use.
- Geography, time window, companies/products, and excluded areas.
- Required depth: quick brief, full report, investor memo, audit, or source ledger.
- Evidence standard: primary-source only, primary preferred, or mixed with secondary context.
- Output target: inline answer, Markdown report, or existing file update.

If the user did not specify a scope, proceed with the narrowest useful scope
and state the assumption.

### 2. Build the source plan

Create 3-6 source lanes before searching. Examples:

- Official/company/project sources.
- Regulatory or legal sources.
- Financial filings, datasets, or market data.
- Competitor/customer/adoption evidence.
- Reputable analyst, trade press, or technical secondary sources.
- Existing local draft and source ledger files.

For local artifacts, use `list_files`, `glob_search`, and `grep_search` to find
the draft or ledger, then `read_file` the specific files needed.

### 3. Collect sources

Use this sequence:

1. Run targeted `web_search` queries per lane.
2. Fetch each candidate with `web_fetch`; discard unfetched snippets.
3. Escalate to `firecrawl_fetch` or `xcrawl_scrape` only when normal fetch fails or misses the relevant body.
4. Record publisher, URL, publication date, retrieval date, and what claim each source can support.
5. Stop collecting when the core claims are covered by enough independent sources, not when you have many links.

### 4. Maintain the claim ledger

For every material claim, keep this ledger shape:

| Claim | Status | Source | Publisher | Date | Evidence Use | Notes |
|---|---|---|---|---|---|---|

Status definitions:

- `verified`: directly supported by fetched evidence.
- `inferred`: reasonable conclusion from verified facts; label the reasoning.
- `contradicted`: sources conflict; show both sides.
- `stale`: source is too old for the claim or newer information supersedes it.
- `unsupported`: no fetched source supports the claim.

### 5. Analyze, do not just collect

After evidence collection, answer these questions:

- What is actually confirmed?
- What changed over time?
- Which claims depend on assumptions?
- Which sources disagree, and why might they disagree?
- What matters for the user's likely decision?
- What should not be concluded from the available evidence?

For industry research, add market structure, value chain, segment map,
competitor groups, adoption signals, regulatory constraints, and risk drivers.
For source-ledger audits, lead with unsupported or stale claims before style
feedback.

### 6. Produce the final artifact

Use this order for normal reports:

1. Executive answer: 3-7 bullets with the direct conclusion.
2. Scope and method: what was included, excluded, and how evidence was gathered.
3. Key findings: grouped by theme, each tied to ledger entries.
4. Source ledger: full table of claims and source usage.
5. Contradictions and gaps: explicit blockers, stale data, and uncertainty.
6. Next checks: only the checks that would materially improve confidence.

Use this order for source-ledger audits:

1. Blocking issues: unsupported, contradicted, or stale claims.
2. Claim-by-claim audit table.
3. Source quality assessment.
4. Suggested replacement citations or wording changes.
5. Residual risks.

### 7. Save or deliver

If the user asked for a reusable file, write the report with `write_file`
using `templates/report.md` as the structure. If editing an existing draft, use
`edit_file` and keep a concise changelog. Deliver with `send_channel_file` only
when requested.

<anti_patterns>
- Do not produce a confident report from search snippets, summaries, or titles.
- Do not bury unsupported claims in prose; mark them `unsupported`.
- Do not mix publisher date, event date, and retrieval date.
- Do not treat one secondary article as enough evidence for a market, legal, or financial claim.
- Do not over-delegate broad research; delegate narrow source lanes only.
- Do not write a report file unless the user asked for an artifact or the workflow requires one.
</anti_patterns>

<examples>
Input: "做一个 AI code review agent 市场 deep research，重点是企业采购和竞品。"

Output: select `industry_research`, collect official product pages, pricing,
security docs, funding/company sources, and credible analyst/trade coverage;
return an executive answer, market map, competitor groups, evidence ledger,
contradictions, and next checks.

Input: "Audit this memo and tell me which claims are unsupported."

Output: select `source_ledger_audit`, read the memo and ledger with
`read_file`, fetch cited URLs with `web_fetch`, then lead with unsupported,
stale, or contradicted claims and suggested replacement wording.
</examples>

## Bundled Resources

Load resources by need, not by default:

- `references/playbooks.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/report.md`: use as the output scaffold when creating this artifact type.

## Quality Bar

- Do not invent facts, owners, dates, recipients, source evidence, or external system state.
- Prefer deterministic scripts or templates when the skill bundles them for this workflow.
- Keep the final output focused on the artifact or decision the user requested.
- Surface missing credentials, unavailable tools, stale data, and unsupported claims as blockers instead of silently working around them.
