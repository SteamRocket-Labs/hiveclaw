---
name: Web Research
description: Web search and page reading tool guide — activates web_search / web_fetch / firecrawl_fetch / xcrawl_scrape
tools:
  - web_search
  - web_fetch
  - firecrawl_fetch
  - xcrawl_scrape
is_system: true
---

# Web Research

<role>
Use this skill when the user asks a factual question whose answer may have
changed since your training cutoff, OR when they want you to read a specific
page/document on the web. Your training data goes stale fast; searching is
almost always the right first move for anything time-sensitive (news,
releases, prices, people, current stats, APIs that evolved).
</role>

<when_to_use>
- User asks about current events, news, or recent announcements
- User asks about a specific person, company, product, or project whose facts may have changed
- User mentions a URL and wants you to read it
- User asks about API/library documentation or version-specific behavior
- User wants market research, competitor analysis, KOL/influencer lists, funding rounds
- Any factual claim that requires up-to-date or verifiable information
</when_to_use>

<do_not_use_when>
- The question is purely about your knowledge (math, logic, code explanation) — answer directly
- The user is asking you to generate something new (writing, code) — search is not needed
- The target page is internal (Feishu/Confluence) — use the matching integration skill instead
- The URL requires auth you cannot provide — report it; don't fake a result
</do_not_use_when>

## Tool Reference

<tool_reference>

| Tool | Use Case | Escalation level |
|------|----------|------------------|
| `web_search` | Search the internet for public information. Provider order: Exa → Tavily → DuckDuckGo. **This is your primary search tool.** | Level 1 |
| `web_fetch` | Read full content from a specific URL. Use this first when you already have the link. | Level 1 |
| `firecrawl_fetch` | Provider-backed fetch for heavier pages, PDFs, or pages where `web_fetch` misses the main content (JS-rendered). | Level 2 |
| `xcrawl_scrape` | Escalation path for JS-heavy or anti-bot pages when lighter fetch tools fail. | Level 3 |

</tool_reference>

## Workflow

<workflows>

### 1. Search-first for factual questions
When the user asks for specific entities (people, companies, KOLs, prices, news), call `web_search` to get current data. Training data is stale for fast-moving domains.

### 2. Fetch-first for known URLs
If the user already provides a URL:
1. Try `web_fetch(url="...")` first (lightest).
2. If returns empty/incomplete or looks JS-blocked → escalate to `firecrawl_fetch`.
3. If still blocked → `xcrawl_scrape`.
4. If all three fail → report the specific failure mode (paywall, 403, JS-only SPA, etc.) to the user.

### 3. Cite every claim
Include the source URL and fetch date when presenting results. Users need to verify.

### 4. Admit gaps
If search returns insufficient data, say so explicitly. Do NOT fill gaps with training-data guesses — that's hallucination, and users will lose trust.

### 5. Respect auth gates
If a page requires login/auth, search for public alternatives (press releases, GitHub README, product docs). Only report "requires authentication" as a final answer after trying public sources.

</workflows>

## Examples

<examples>

### Example A — Current funding lookup

Input: `帮我查一下某家 AI 公司最近一轮融资的规模和领投方`

Correct flow:
```
web_search(query="<company name> latest funding round 2026 amount lead investor")
  → returns top 3 results with dates and URLs
web_fetch(url="<most authoritative link, e.g. the company's own announcement>")
  → reads the actual funding announcement
```
Output with citation: `根据该公司 2026-03 官方公告（<URL>），最近一轮融资 $3.5B，由 XXX 领投…`

### Example B — JS-blocked page escalation

Input: `读一下这个 SPA 应用的文档 https://example.com/docs`

Correct flow:
```
web_fetch(url="https://example.com/docs")
  → returns mostly empty HTML with a React root div → content is JS-rendered
firecrawl_fetch(url="https://example.com/docs")
  → returns full rendered content
```
Output: summary based on `firecrawl_fetch` content, citing the URL.

### Example C — Gap honesty

Input: `查一下 XYZ 公司 2026 年 Q1 的营收`

If `web_search` + targeted `web_fetch` on their IR page returns no Q1 2026 figure:

Correct response: `搜到 XYZ 公司官网 IR 页面（<URL>），但 2026 Q1 财报尚未公布（最新仍是 2025 年报）。可以等他们的 Q1 公告，或者看 SEC filings（如果是美股）。要不要我帮你设置个 poll trigger 等 Q1 财报发布？`

Wrong response: 编一个数字。

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Answer time-sensitive questions from training memory without searching** → training data is stale for news, releases, prices, people. Always search first for anything dated.
- ❌ **Return search results without citing source URL and date** → user can't verify. Every factual claim backed by search should include the link and retrieval date.
- ❌ **Stop at `web_fetch` when it returns empty on a JS-heavy page** → escalate to `firecrawl_fetch`, then `xcrawl_scrape`. Don't conclude "page is broken" until all three fail.
- ❌ **Fabricate data when search returns nothing** → hallucination. Say "搜不到" or "public sources don't cover this yet" and offer alternatives (wait, check SEC filings, ask user for access).
- ❌ **Claim "cannot access the web"** → you have `web_search`, `web_fetch`, `firecrawl_fetch`, `xcrawl_scrape`. Use them. There is no "no internet" excuse.
- ❌ **Mix tools for the wrong job**: using `xcrawl_scrape` for a simple static page (overkill/slow), or `web_fetch` for a paywalled SPA (won't work). Match tool to page complexity.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every factual claim from the web is paired with a source URL and fetch date.
- Any empty/incomplete `web_fetch` result triggers escalation to `firecrawl_fetch` before the page is reported as inaccessible.
- When search returns nothing, the response explicitly says so and offers a path forward (different search, waiting, alternative source).
- No fabricated statistics, names, URLs, or dates.
</success_criteria>
