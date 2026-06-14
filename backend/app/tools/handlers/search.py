"""Search tools — web search, direct fetch, and advanced page extraction."""

from __future__ import annotations

from app.tools.decorator import RESULT_CHARS_UNLIMITED, ToolMeta, tool

# ── web_search ───────────────────────────────────────────────────────

@tool(ToolMeta(
    name="web_search",
    max_result_chars=RESULT_CHARS_UNLIMITED,
    description=(
        "Search the internet for public information. Prefer Exa when it is configured; otherwise fall back to Tavily or DuckDuckGo.\n\n"
        "Usage:\n"
        "- Use specific, well-formed search queries — not full sentences. Good: 'Python pandas groupby multiple columns'. Bad: 'How do I group by multiple columns in pandas?'\n"
        "- Results include titles, URLs, and snippets. To read full page content, follow up with `web_fetch` after you pick the best URL.\n"
        "- In cloud deployments, Exa is the preferred provider-backed search path. Tavily is the secondary provider-backed option.\n"
        "- Do NOT rely on provider-specific search tools first; use this generic search entrypoint unless you are debugging provider behavior.\n"
        "- May be unavailable on some networks. If search fails, retry with a narrower query or read a known URL with `web_fetch`.\n"
        "- Do NOT search for information already available in your workspace files or loaded skills."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results to return",
            },
        },
        "required": ["query"],
    },
    category="search",
    display_name="Web Search",
    icon="\U0001f986",
    is_default=True,
    read_only=True,
    parallel_safe=True,
    governance="safe",
    pack="web_pack",
    aliases=("bing_search",),
    adapter="args_only",
    config={
        "search_engine": "auto",
        "max_results": 5,
        "language": "en",
        "exa_api_key": "",
        "tavily_api_key": "",
    },
    config_schema={
        "fields": [
            {
                "key": "search_engine",
                "label": "Search Engine",
                "type": "select",
                "options": [
                    {"value": "auto", "label": "Auto (prefer Exa, then Tavily, then DuckDuckGo)"},
                    {"value": "exa", "label": "Exa (preferred search API, needs API key)"},
                    {"value": "tavily", "label": "Tavily (secondary search API, needs API key)"},
                    {"value": "duckduckgo", "label": "DuckDuckGo (free, no API key)"},
                ],
                "default": "auto",
            },
            {
                "key": "exa_api_key",
                "label": "Exa API Key",
                "type": "password",
                "default": "",
                "placeholder": "exk_...",
                "depends_on": {"search_engine": ["auto", "exa"]},
            },
            {
                "key": "tavily_api_key",
                "label": "Tavily API Key",
                "type": "password",
                "default": "",
                "placeholder": "tvly-...",
                "depends_on": {"search_engine": ["auto", "tavily"]},
            },
            {
                "key": "max_results",
                "label": "Default results count",
                "type": "number",
                "default": 5,
                "min": 1,
                "max": 20,
            },
            {
                "key": "language",
                "label": "Search language",
                "type": "select",
                "options": [
                    {"value": "en", "label": "English"},
                    {"value": "zh-CN", "label": "中文"},
                    {"value": "ja", "label": "日本語"},
                ],
                "default": "en",
            },
        ]
    },
))
async def web_search(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _web_search
    return await _web_search(arguments)


# ── web_fetch ───────────────────────────────────────────────────────

@tool(ToolMeta(
    name="web_fetch",
    description=(
        "Fetch and extract readable content directly from a specific URL without relying on third-party reader services.\n\n"
        "Usage:\n"
        "- Use this when you already have a URL and want a direct, deterministic fetch path.\n"
        "- Prefer this after `web_search` identifies the right page, or as the default known-URL path in cloud deployments.\n"
        "- Prefer this before heavier providers when the page is simple and directly fetchable.\n"
        "- This tool is for known URLs, not keyword search. Use `web_search` first if needed.\n"
        "- The result may be truncated for very long pages."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL to fetch, e.g. 'https://example.com/article' or 'example.com/article'",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max characters to return (default 8000, max 20000)",
            },
        },
        "required": ["url"],
    },
    category="search",
    display_name="Web Fetch",
    icon="\U0001f310",
    is_default=True,
    read_only=True,
    parallel_safe=True,
    governance="safe",
    aliases=("read_webpage",),
    adapter="args_only",
))
async def web_fetch(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _web_fetch
    return await _web_fetch(arguments)


# ── firecrawl_fetch ──────────────────────────────────────────────────

@tool(ToolMeta(
    name="firecrawl_fetch",
    max_result_chars=RESULT_CHARS_UNLIMITED,
    description=(
        "Fetch a known URL with Firecrawl for heavier page extraction, JS-heavy pages, or cleaner markdown than a raw fetch.\n\n"
        "Usage:\n"
        "- Use this after `web_search` or when you already have a specific URL and `web_fetch` is not sufficient.\n"
        "- Prefer this for complex pages, PDFs, or sites where a plain fetch misses the main content.\n"
        "- Do NOT use this for keyword search. If you do not have a URL yet, search first.\n"
        "- This tool is provider-backed and requires Firecrawl configuration."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch and extract."},
            "max_chars": {"type": "integer", "description": "Max characters to return (default 12000, max 30000)"},
            "only_main_content": {"type": "boolean", "description": "Prefer extracting just the main article/body content. Default true."},
        },
        "required": ["url"],
    },
    category="search",
    display_name="Firecrawl Fetch",
    icon="\U0001f525",
    read_only=True,
    parallel_safe=True,
    governance="safe",
    pack="web_pack",
    adapter="args_only",
    config={"api_key": ""},
    config_schema={"fields": [{"key": "api_key", "label": "Firecrawl API Key", "type": "password", "default": "", "placeholder": "fc-..."}]},
))
async def firecrawl_fetch(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _firecrawl_fetch
    return await _firecrawl_fetch(arguments)


# ── xcrawl_scrape ────────────────────────────────────────────────────

@tool(ToolMeta(
    name="xcrawl_scrape",
    max_result_chars=RESULT_CHARS_UNLIMITED,
    description=(
        "Scrape a known URL with XCrawl for JS-rendered, anti-bot, or otherwise difficult pages.\n\n"
        "Usage:\n"
        "- Use this when `web_fetch` and `firecrawl_fetch` are insufficient, especially for highly dynamic or anti-bot-heavy pages.\n"
        "- Prefer this only for hard pages because it is a heavier provider-backed path.\n"
        "- Do NOT use this for keyword search. If you do not have a URL yet, search first.\n"
        "- This tool is provider-backed and requires XCrawl configuration."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to scrape."},
            "max_chars": {"type": "integer", "description": "Max characters to return (default 12000, max 30000)"},
            "js_render": {"type": "boolean", "description": "Enable JS rendering. Default true."},
        },
        "required": ["url"],
    },
    category="search",
    display_name="XCrawl Scrape",
    icon="\U0001f577\ufe0f",
    read_only=True,
    parallel_safe=True,
    governance="safe",
    pack="web_pack",
    adapter="args_only",
    config={"api_key": ""},
    config_schema={"fields": [{"key": "api_key", "label": "XCrawl API Key", "type": "password", "default": "", "placeholder": "xcr_..."}]},
))
async def xcrawl_scrape(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _xcrawl_scrape
    return await _xcrawl_scrape(arguments)


# ── discover_resources ───────────────────────────────────────────────

@tool(ToolMeta(
    name="discover_resources",
    max_result_chars=RESULT_CHARS_UNLIMITED,
    description=(
        "Search public MCP registries (Smithery + ModelScope) for tools and capabilities that can extend your abilities.\n\n"
        "Usage:\n"
        "- Only use this after builtin tools, loaded skills, and direct web/file tools still cannot complete the task.\n"
        "- Treat this as an explicit platform-extension/admin workflow, not a normal task-execution path.\n"
        "- Describe the capability you need, not a vendor name unless that vendor is required.\n"
        "- Review discovered capabilities before importing them into your runtime.\n"
        "- Do NOT use this if an existing builtin tool, loaded skill, or active pack already solves the task."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Semantic description of the capability needed, e.g. 'send email', 'query SQL database', 'generate images'",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results to return (default 5, max 10)",
            },
        },
        "required": ["query"],
    },
    category="mcp",
    display_name="Discover Resources",
    icon="\U0001f50d",
    read_only=True,
    parallel_safe=True,
    governance="safe",
    pack="mcp_admin_pack",
    adapter="args_only",
    config={"smithery_api_key": "", "modelscope_api_token": ""},
    config_schema={"fields": [
        {"key": "smithery_api_key", "label": "Smithery API Key", "type": "password", "default": "", "placeholder": "Get from smithery.ai/account/api-keys"},
        {"key": "modelscope_api_token", "label": "ModelScope API Token", "type": "password", "default": "", "placeholder": "Get from modelscope.cn"},
    ]},
))
async def discover_resources(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _discover_resources
    return await _discover_resources(arguments)


# ── search_clawhub ──────────────────────────────────────────────────

@tool(ToolMeta(
    name="search_clawhub",
    description=(
        "Search the ClawHub skill marketplace for agent skills.\n\n"
        "Usage:\n"
        "- Return skill slugs that can be passed to `create_digital_employee(clawhub_slugs=[...])`.\n"
        "- Use this when hiring a new agent and you need installable marketplace skills.\n"
        "- Search with concise domain keywords rather than long natural-language requests.\n"
        "- Do NOT use this for local workspace skills — inspect the local skill catalog instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords in English, e.g. 'market research', 'web3 crypto', 'competitor analysis'",
            },
        },
        "required": ["query"],
    },
    category="search",
    display_name="Search ClawHub",
    icon="\U0001f3aa",
    read_only=True,
    parallel_safe=True,
    governance="safe",
    adapter="args_only",
))
async def search_clawhub(arguments: dict) -> str:
    import httpx

    query = arguments.get("query", "").strip()
    if not query:
        return "❌ Please provide search keywords"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://clawhub.ai/api/search",
                params={"q": query},
            )
            if resp.status_code != 200:
                return f"❌ ClawHub search failed: HTTP {resp.status_code}"
            data = resp.json()

        results = data.get("results", [])
        if not results:
            return f'🔍 No ClawHub skills found for "{query}"'

        lines = [f'🔍 ClawHub skills for "{query}" ({len(results)} results):\n']
        for r in results[:8]:
            slug = r.get("slug", "?")
            name = r.get("displayName", slug)
            summary = r.get("summary", "")[:100]
            lines.append(f"**{name}** (slug: `{slug}`)\n{summary}\n")

        lines.append("\n💡 Pass the `slug` values to `create_digital_employee(clawhub_slugs=[...])` to install.")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ ClawHub search error: {str(e)[:200]}"
