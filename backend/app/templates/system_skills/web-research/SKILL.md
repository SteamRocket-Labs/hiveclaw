---
name: Advanced Web Research
description: "Use when core web_search/web_fetch is insufficient and the task needs vertical search, source extraction, crawler escalation, contradiction checks, or exact date handling."
tools:
  - web_search
  - web_fetch
  - anysearch_get_sub_domains
  - anysearch_search
  - anysearch_batch_search
  - anysearch_extract
  - exa_search
  - tavily_search
  - firecrawl_fetch
  - xcrawl_scrape
is_system: true
---

# Advanced Web Research

<role>
Use this skill after the core `web_search` / `web_fetch` path is not enough:
results are sparse, stale, contradictory, too broad, or require a vertical
provider, source extraction, crawler handling, or date-sensitive evidence plan.
Basic public lookup remains a Core tool behavior and does not require this
skill.
</role>

<when_to_use>
- Core `web_search` returned weak, sparse, contradictory, or stale results.
- The task needs AnySearch vertical routing, Exa/Tavily source discovery, or crawler extraction.
- The user wants market research, competitor analysis, KOL/influencer lists, funding rounds, or other multi-source synthesis.
- The task requires explicit source-quality checks, contradiction handling, or exact date handling.
</when_to_use>

<do_not_use_when>
- The question is purely about your knowledge (math, logic, code explanation) — answer directly
- The user is asking you to generate something new (writing, code) — search is not needed
- Basic `web_search` or a known-URL `web_fetch` is enough — call the Core tool directly
- The target page is internal or authenticated (for example Feishu) — use the matching configured integration skill instead
- The URL requires auth you cannot provide — report it; don't fake a result
</do_not_use_when>

## Credential Boundary

- Web/search provider credentials are managed by tool config or platform config.
- Do not inspect environment variables or use `run_command` to look for Exa, Tavily, Firecrawl, XCrawl, Google, or API key values.
- If a web tool reports auth/config failure, report the configuration gap and use another available public-source path; do not switch to shell/env probing.

## Tool Reference

<tool_reference>

| Tool | Use Case | Escalation level |
|------|----------|------------------|
| `web_search` | Basic public web search using Hive's built-in basic provider chain: SearXNG when configured, with legacy HTML fallback only for manual/debug use. **Start here for normal lookup.** | Level 1 |
| `web_fetch` | Read full content from a specific URL. Use this first when you already have the link. | Level 1 |
| `anysearch_get_sub_domains` | AnySearch search/discovery surface schema directory. Call before vertical `anysearch_search` to discover valid sub-domain routes and required sub-domain parameters. | Level 2 |
| `anysearch_search` | AnySearch search/discovery surface for vertical or general search. Use for precise finance, social media, academic, legal, health, business, security, code, energy, environment, agriculture, travel, film, and gaming sources when basic search is too broad. | Level 2 |
| `anysearch_batch_search` | AnySearch search/discovery surface for parallel 2-5 query search. Use for hybrid general + vertical coverage or multi-domain intersections. | Level 2 |
| `anysearch_extract` | AnySearch read/extract surface for known URLs. It extracts full page Markdown from a URL returned by search. Prefer `web_fetch` for ordinary known URLs. | Level 2 |
| `exa_search` | Exa AI-native search for LLM agents. Use when you need search type control, category verticals (company, research paper, news, personal site, financial report, people), semantic/source discovery, or dense extracted result text. | Level 2 |
| `tavily_search` | Tavily real-time web access layer for AI agents/RAG. Use when you need current factual retrieval, topic routing (`general`/`news`/`finance`), freshness filters, provider answers, or RAG-friendly content/chunks. | Level 2 |
| `firecrawl_fetch` | Firecrawl `/scrape` for a known URL when you need single-page extraction into LLM-ready markdown or richer formats. Use `onlyMainContent`, clean-content, tags, and wait options to reduce page chrome or handle slow rendering. Discover with `tool_search` if not visible. Do not use crawler fetch tools for keyword search. | Level 2 |
| `xcrawl_scrape` | XCrawl Scrape API for single-page extraction using official `output.formats`, `request`, and `js_render.enabled` options. Use for hard JS-rendered/proxy/device/locale cases after lighter readers fail. Discover with `tool_search` if not visible. Do not use crawler fetch tools for keyword search. | Level 3 |

</tool_reference>

## Workflow

<workflows>

### 1. Search-first for factual questions
When the user asks for specific entities (people, companies, KOLs, prices, news), call `web_search` to get current data. Training data is stale for fast-moving domains.

If `web_search` returns weak, sparse, contradictory, or obviously stale results, use:
1. `tool_search(query="advanced web search")` to discover AnySearch MCP vertical tools, `exa_search`, and `tavily_search`.
2. `anysearch_get_sub_domains` before vertical `anysearch_search` when the question is finance, social media, academic, legal, health, business, security, code, or another domain-specific data request.
3. `anysearch_batch_search` for hybrid general + vertical coverage when the best domain route is uncertain.
4. `exa_search` for AI-native source discovery, category-specific retrieval, research papers, company/person pages, financial reports, or deep search types.
5. `tavily_search` for real-time web retrieval, news/finance/current factual queries, freshness filters, provider answers, or RAG-friendly chunks.

### 2. AnySearch MCP vertical workflow
Use AnySearch MCP vertical tools when the request benefits from domain-specific API-like retrieval rather than broad web ranking. Examples include finance quotes and fundamentals, company data, SEC-style public filings, social media discovery, academic papers, patents, legal/regulatory materials, health/biomedical public sources, security/IP intelligence, code documentation, energy/environment/agriculture/travel/film/gaming data.

Treat AnySearch as two separate surfaces:
- AnySearch search/discovery surface: `anysearch_get_sub_domains`, `anysearch_search`, and `anysearch_batch_search`. Use these to discover schemas, run keyword or vertical searches, and collect result URLs/snippets.
- AnySearch read/extract surface: `anysearch_extract`. anysearch_extract is not a keyword-search tool; use it only after you already have a known URL and need full Markdown content.

Flow:
1. Identify the likely domain or domains, for example finance, social media, academic, legal, health, business, security, IP, or code.
2. Call `anysearch_get_sub_domains` before vertical `anysearch_search`. Cache the returned domain schema within the session; do not call repeatedly for the same domain unless the previous result is missing.
3. Choose the best sub-domain from the returned descriptions.
4. Pass all required sub-domain parameters. If a required parameter has no meaningful value, pass an empty string for that key instead of omitting it.
5. Use `anysearch_batch_search` when one topic crosses multiple domains or when you are unsure whether general search or vertical search will work better.
6. Read the selected URL with `web_fetch` by default. Use `anysearch_extract` only when an AnySearch result URL needs full page Markdown and keeping the AnySearch extraction path is useful or `web_fetch` is not enough.

### 3. Fetch-first for known URLs
If the user already provides a URL:
1. Try `web_fetch(url="...")` first (lightest).
2. If it returns empty/incomplete content or looks JS-blocked, use `tool_search(query="web crawl")` if needed, then escalate to `firecrawl_fetch`.
3. Use `firecrawl_fetch` as the default provider-backed scrape path for a known URL: request markdown first, add links/summary/html/screenshot/json only when the task needs those fields.
4. If Firecrawl is still blocked or misses content, use `xcrawl_scrape` for XCrawl's sync single-page scrape with `output.formats`, `request.only_main_content`, and `js_render.enabled`.
5. Do not use crawler fetch tools for keyword search. Search first, then fetch or scrape the selected URLs.
6. If all three fail, report the specific failure mode (paywall, 403, JS-only SPA, auth wall, provider quota, etc.) to the user.

### 4. Cite every claim
Include the source URL and fetch date when presenting results. Users need to verify.

### 5. Match output size to the ask
For simple "recent news", "related messages", or "what changed" requests, answer inline with concise bullets and source links. Do not create a report file, send an attachment, or call delivery tools unless the user explicitly asks for a report/file, the answer is too long for chat, or a durable artifact is clearly part of the task.

### 6. Admit gaps
If search returns insufficient data, say so explicitly. Do NOT fill gaps with training-data guesses — that's hallucination, and users will lose trust.

### 7. Respect auth gates
If a page requires login/auth, search for public alternatives (press releases, GitHub README, product docs). Only report "requires authentication" as a final answer after trying public sources.

</workflows>

## Examples

<examples>

### Example A — Current funding lookup

Input: `Find the latest funding round size and lead investor for a specific AI company.`

Correct flow:
```
web_search(query="<company name> latest funding round 2026 amount lead investor")
  → returns top 3 results with dates and URLs
if results are sparse or low quality:
tool_search(query="advanced web search")
tavily_search(query="<company name> latest funding round 2026 amount lead investor", topic="news", time_range="year")
exa_search(query="<company name> latest funding round 2026 amount lead investor", category="company")
web_fetch(url="<most authoritative link, e.g. the company's own announcement>")
  → reads the actual funding announcement
```
Output with citation: `According to the company's March 2026 announcement (<URL>), the latest round was $3.5B and was led by XXX...`

### Example B — JS-blocked page escalation

Input: `Read the documentation for this SPA app: https://example.com/docs`

Correct flow:
```
web_fetch(url="https://example.com/docs")
  → returns mostly empty HTML with a React root div → content is JS-rendered
firecrawl_fetch(url="https://example.com/docs")
  → returns full rendered content
```
Output: summary based on `firecrawl_fetch` content, citing the URL.

### Example C — Vertical finance lookup

Input: `Find recent AAPL quote and fundamentals data.`

Correct flow:
```
tool_search(query="AnySearch finance vertical search")
anysearch_get_sub_domains(domain="finance")
anysearch_search(
  query="AAPL",
  domain="finance",
  sub_domain="<best finance sub-domain from the directory>",
  sub_domain_params={"symbol": "AAPL", "cn_code": "", "type": "<required value>"},
  max_results=5
)
```
Output: concise finance summary with returned source links and retrieval date.

### Example D — Hybrid social media discovery

Input: `Find public reactions to a product launch on X, Reddit, and video platforms.`

Correct flow:
```
tool_search(query="AnySearch social media vertical search")
anysearch_get_sub_domains(domain="social_media")
anysearch_batch_search(queries=[
  {"query": "<product> launch public reactions"},
  {"query": "<product> launch public reactions", "domain": "social_media", "sub_domain": "<selected social media sub-domain>", "sub_domain_params": {}}
])
```
Use AnySearch for public discovery. For account-scoped actions, exact posts, or authenticated exports, use the dedicated authenticated tool after approval.

### Example E — Gap honesty

Input: `Find XYZ Company's Q1 2026 revenue.`

If `web_search` + targeted `web_fetch` on their IR page returns no Q1 2026 figure:

Correct response: `I found XYZ Company's official IR page (<URL>), but Q1 2026 results have not been published yet; the latest available report is still the 2025 annual report. The next path is to monitor the IR page or check SEC filings if the company is US-listed.`

Wrong response: inventing a number.

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Answer time-sensitive questions from training memory without searching** → training data is stale for news, releases, prices, people. Always search first for anything dated.
- ❌ **Return search results without citing source URL and date** → user can't verify. Every factual claim backed by search should include the link and retrieval date.
- ❌ **Stop at `web_fetch` when it returns empty on a JS-heavy page** → escalate to `firecrawl_fetch`, then `xcrawl_scrape`. Don't conclude "page is broken" until all three fail.
- ❌ **Fabricate data when search returns nothing** → hallucination. Say "public sources do not cover this yet" and offer alternatives (wait, check SEC filings, ask user for access).
- ❌ **Claim "cannot access the web"** → you have `web_search`, `web_fetch`, and deferred advanced tools (`anysearch_get_sub_domains`, `anysearch_search`, `anysearch_batch_search`, `anysearch_extract`, `exa_search`, `tavily_search`, `firecrawl_fetch`, `xcrawl_scrape`) discoverable through `tool_search`. Use them. There is no "no internet" excuse.
- ❌ **Mix tools for the wrong job**: using `xcrawl_scrape` for a simple static page (overkill/slow), or `web_fetch` for a paywalled SPA (won't work). Match tool to page complexity.
- ❌ **Skip `anysearch_get_sub_domains` before vertical `anysearch_search`** → AnySearch MCP vertical routes have required parameters. Discover the route first.
- ❌ **Omit required sub-domain parameters** → include every required parameter returned by `anysearch_get_sub_domains`, using an empty string when necessary.
- ❌ **Use `anysearch_extract` as a search tool** → it is a read/extract tool for known URLs. Search with `anysearch_search`, `anysearch_batch_search`, `exa_search`, `tavily_search`, or `web_search` first.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every factual claim from the web is paired with a source URL and fetch date.
- Simple lookup requests are answered inline; report/file artifacts are reserved for explicit report/file asks, long outputs, or durable deliverables.
- Any empty/incomplete `web_fetch` result triggers escalation to `firecrawl_fetch` before the page is reported as inaccessible.
- Weak basic search results trigger `tool_search` discovery of advanced search providers, including AnySearch MCP vertical tools, before you conclude no public information exists.
- Vertical searches call `anysearch_get_sub_domains` before `anysearch_search` and pass required sub-domain parameters.
- AnySearch extraction happens only after a known URL has been selected; `anysearch_extract` is never used for keyword search.
- When search returns nothing, the response explicitly says so and offers a path forward (different search, waiting, alternative source).
- No fabricated statistics, names, URLs, or dates.
</success_criteria>

## Bundled Resources

Load resources by need, not by default:

- `references/source-quality.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/research-brief.md`: use as the output scaffold when creating this artifact type.
