"""Search tools — web search, direct fetch, and advanced page extraction."""

from __future__ import annotations

from app.tools.decorator import RESULT_CHARS_UNLIMITED, ToolMeta, tool

# ── web_search ───────────────────────────────────────────────────────


@tool(
    ToolMeta(
        name="web_search",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Basic internet search for public information using Hive's built-in basic provider chain "
            "(SearXNG when configured, with legacy HTML fallback only for manual/debug use).\n\n"
            "Usage:\n"
            "- Use specific, well-formed search queries — not full sentences. Good: 'Python pandas groupby multiple columns'. Bad: 'How do I group by multiple columns in pandas?'\n"
            "- Results include titles, URLs, and snippets. To read full page content, follow up with `web_fetch` after you pick the best URL.\n"
            "- Start here for normal lookup. AnySearch is an L2 add-on, not the CORE provider for this tool; if basic results are too shallow, stale, sparse, or ambiguous, use `tool_search` to discover advanced search tools such as `anysearch_search`, `exa_search`, or `tavily_search`.\n"
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
        aliases=("bing_search",),
        adapter="args_only",
        config={
            "search_engine": "auto",
            "max_results": 5,
            "language": "en",
        },
        config_schema={
            "fields": [
                {
                    "key": "search_engine",
                    "label": "Search Engine",
                    "type": "select",
                    "options": [
                        {"value": "auto", "label": "Auto (SearXNG when configured)"},
                        {"value": "searxng", "label": "SearXNG fallback (platform configured)"},
                        {"value": "duckduckgo_legacy", "label": "Legacy HTML fallback (manual/debug only)"},
                    ],
                    "default": "auto",
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
                        {"value": "zh-CN", "label": "Chinese (zh-CN)"},
                        {"value": "ja", "label": "Japanese"},
                    ],
                    "default": "en",
                },
            ]
        },
    )
)
async def web_search(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _web_search

    return await _web_search(arguments)


# ── AnySearch MCP vertical tools ─────────────────────────────────────

_ANYSEARCH_DOMAINS = [
    "general",
    "resource",
    "social_media",
    "finance",
    "academic",
    "legal",
    "health",
    "business",
    "security",
    "ip",
    "code",
    "energy",
    "environment",
    "agriculture",
    "travel",
    "film",
    "gaming",
]


@tool(
    ToolMeta(
        name="anysearch_get_sub_domains",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Discover AnySearch MCP vertical sub-domains and required parameters before vertical search. "
            "This is part of the AnySearch search/discovery surface, not a page reader.\n\n"
            "Usage:\n"
            "- Call this before vertical search whenever the query is domain-specific, structured, real-time, or ambiguous.\n"
            "- Use `domain` for one domain or `domains` for up to five domains. Cache the returned schema within the session.\n"
            "- Then call `anysearch_search` with the selected `domain`, `sub_domain`, and required `sub_domain_params`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": _ANYSEARCH_DOMAINS,
                    "description": "Single AnySearch vertical domain to inspect, e.g. finance or social_media.",
                },
                "domains": {
                    "type": "array",
                    "items": {"type": "string", "enum": _ANYSEARCH_DOMAINS},
                    "minItems": 1,
                    "maxItems": 5,
                    "description": "Optional batch of up to five domains. Takes precedence over domain.",
                },
            },
        },
        category="search",
        display_name="AnySearch Domains",
        icon="\U0001f5c2\ufe0f",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
    )
)
async def anysearch_get_sub_domains(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _anysearch_get_sub_domains

    return await _anysearch_get_sub_domains(arguments)


@tool(
    ToolMeta(
        name="anysearch_search",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Search with the AnySearch MCP search/discovery surface, including vertical domains such as finance, "
            "social_media, academic, legal, health, business, security, code, energy, and government-adjacent data.\n\n"
            "Usage:\n"
            "- For pure general knowledge, omit domain and sub_domain.\n"
            "- For vertical or structured data, call `anysearch_get_sub_domains` first, then pass `domain`, "
            "`sub_domain`, and all required `sub_domain_params`.\n"
            "- Use this for precise data APIs and domain-specific public sources when basic `web_search` is too broad.\n"
            "- To read the full page for a returned URL, use `web_fetch` or `anysearch_extract` after selecting the URL."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "domain": {
                    "type": "string",
                    "enum": _ANYSEARCH_DOMAINS,
                    "description": "Optional AnySearch vertical domain.",
                },
                "sub_domain": {
                    "type": "string",
                    "description": "Sub-domain key discovered through anysearch_get_sub_domains, e.g. finance.quote.",
                },
                "sub_domain_params": {
                    "type": "object",
                    "description": "Parameters required by the selected sub_domain. Include required params even with empty values.",
                    "additionalProperties": {"type": "string"},
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return, 1-10.",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
        category="search",
        display_name="AnySearch Search",
        icon="\U0001f50d",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
    )
)
async def anysearch_search(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _anysearch_search

    return await _anysearch_search(arguments)


@tool(
    ToolMeta(
        name="anysearch_batch_search",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Run 2-5 AnySearch MCP searches in parallel through the AnySearch search/discovery surface, useful "
            "for hybrid general + vertical coverage or multi-domain intersection research.\n\n"
            "Usage:\n"
            "- Provide 2-5 query objects. Each item supports query, domain, sub_domain, sub_domain_params, and max_results.\n"
            "- Use one general query plus one or more vertical queries when you are unsure which domain route will be best."
        ),
        parameters={
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query."},
                            "domain": {"type": "string", "enum": _ANYSEARCH_DOMAINS},
                            "sub_domain": {"type": "string"},
                            "sub_domain_params": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                                "description": "Parameters required by the selected sub_domain.",
                            },
                            "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        "required": ["query"],
                    },
                    "description": "2-5 AnySearch query objects.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Default max results to inject into items that omit max_results, 1-10.",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["queries"],
        },
        category="search",
        display_name="AnySearch Batch",
        icon="\U0001f9f5",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
    )
)
async def anysearch_batch_search(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _anysearch_batch_search

    return await _anysearch_batch_search(arguments)


@tool(
    ToolMeta(
        name="anysearch_extract",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Extract full page content as Markdown through the AnySearch read/extract surface for a known URL "
            "returned by search. This is not a keyword-search tool.\n\n"
            "Usage:\n"
            "- Use this after AnySearch search results when you need full page content beyond snippets.\n"
            "- Prefer `web_fetch` first for normal known URLs; use this when keeping the AnySearch vertical workflow together helps."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to extract as Markdown."},
            },
            "required": ["url"],
        },
        category="search",
        display_name="AnySearch Extract",
        icon="\U0001f4c4",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
    )
)
async def anysearch_extract(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _anysearch_extract

    return await _anysearch_extract(arguments)


# ── advanced web routers ─────────────────────────────────────────────


@tool(
    ToolMeta(
        name="advanced_web_search",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Advanced web search router for no-key providers by default. It chooses among AnySearch vertical "
            "search, Exa AI-native search, Tavily real-time search, and Firecrawl Search when core `web_search` "
            "is too shallow, stale, sparse, or needs result content.\n\n"
            "Usage:\n"
            "- Use this after `web_search` is insufficient, or when you already know the query needs an advanced provider.\n"
            "- Set `intent=vertical` with domain/sub_domain data for AnySearch; use `intent=news/current/finance` for Tavily; use `intent=semantic/company/research_paper` for Exa; use `include_content=true` or `intent=content` for Firecrawl Search.\n"
            "- Leave `provider=auto` unless you need a deterministic provider override."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Concise search query."},
                "intent": {
                    "type": "string",
                    "enum": [
                        "auto",
                        "vertical",
                        "current",
                        "news",
                        "finance",
                        "semantic",
                        "company",
                        "research_paper",
                        "content",
                    ],
                    "description": "Routing hint for the advanced search provider.",
                },
                "provider": {
                    "type": "string",
                    "enum": ["auto", "anysearch", "exa", "tavily", "firecrawl"],
                    "description": "Optional deterministic provider override.",
                },
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                "include_content": {
                    "type": "boolean",
                    "description": "Use Firecrawl Search when result body content is needed with search results.",
                },
                "domain": {"type": "string", "enum": _ANYSEARCH_DOMAINS},
                "sub_domain": {"type": "string"},
                "sub_domain_params": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "AnySearch vertical sub-domain parameters discovered through anysearch_get_sub_domains.",
                },
            },
            "required": ["query"],
        },
        category="search",
        display_name="Advanced Web Search",
        icon="\U0001f9ed",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
    )
)
async def advanced_web_search(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _advanced_web_search

    return await _advanced_web_search(arguments)


@tool(
    ToolMeta(
        name="advanced_web_fetch",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Advanced web fetch router for known URLs. It tries direct `web_fetch`, Firecrawl, Tavily Extract, "
            "Exa Fetch, AnySearch Extract, and XCrawl only when configured.\n\n"
            "Usage:\n"
            "- Use this when a known URL needs stronger extraction than direct `web_fetch`, or when you want one router to try alternate readers.\n"
            "- Set `prefer_rendered=true` for JS-rendered pages so Firecrawl runs before direct `web_fetch`.\n"
            "- Set `provider` only for deterministic overrides; `provider=xcrawl` requires a configured XCrawl API key."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to read or extract."},
                "provider": {
                    "type": "string",
                    "enum": ["auto", "web_fetch", "firecrawl", "tavily", "exa", "anysearch", "xcrawl"],
                    "description": "Optional deterministic provider override.",
                },
                "prefer_rendered": {
                    "type": "boolean",
                    "description": "Run Firecrawl before direct web_fetch for JS-rendered or blocked pages.",
                },
                "skip_core": {
                    "type": "boolean",
                    "description": "Skip direct web_fetch in auto mode and start with provider extractors.",
                },
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 30000},
            },
            "required": ["url"],
        },
        category="search",
        display_name="Advanced Web Fetch",
        icon="\U0001f310",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
    )
)
async def advanced_web_fetch(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _advanced_web_fetch

    return await _advanced_web_fetch(arguments)


# ── advanced search providers ────────────────────────────────────────


@tool(
    ToolMeta(
        name="exa_search",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Exa AI-native search for LLM agents: search the web and extract contents from results, with search "
            "type controls and category verticals such as company, research paper, news, personal site, "
            "financial report, and people.\n\n"
            "Usage:\n"
            "- This is a provider-backed escalation tool discovered through `tool_search`; start with `web_search` unless you already know Exa is needed.\n"
            "- Use Exa when you need AI-native search, semantic/source discovery, dense extracted text, or a known retrieval category such as research papers, companies, people, news, or financial reports.\n"
            "- `search_type` controls latency vs. reasoning depth: auto/instant/fast for quick retrieval; deep-lite/deep/deep-reasoning for harder synthesized research.\n"
            "- Results include titles, URLs, and extracted text. Follow up with `web_fetch`, `firecrawl_fetch`, or `xcrawl_scrape` for full page content when needed.\n"
            "- Works with no API key through Exa MCP by default; an Exa API key upgrades the direct API path and production limits."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for Exa's AI-native web index."},
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return.",
                    "minimum": 1,
                    "maximum": 20,
                },
                "search_type": {
                    "type": "string",
                    "enum": ["auto", "instant", "fast", "deep-lite", "deep", "deep-reasoning"],
                    "description": "Exa search type: auto default; instant/fast for low latency; deep variants for harder research.",
                },
                "category": {
                    "type": "string",
                    "enum": ["company", "research paper", "news", "personal site", "financial report", "people"],
                    "description": "Optional Exa category vertical when you know the retrieval surface.",
                },
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional domains to restrict results to, e.g. ['arxiv.org'].",
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional domains to exclude from results.",
                },
                "start_published_date": {
                    "type": "string",
                    "description": "Optional ISO datetime; only return links published after this date.",
                },
                "end_published_date": {
                    "type": "string",
                    "description": "Optional ISO datetime; only return links published before this date.",
                },
            },
            "required": ["query"],
        },
        category="search",
        display_name="Exa Search",
        icon="\U0001f9ed",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
        config={"api_key": "", "auth_mode": "auto", "mcp_url": "https://mcp.exa.ai/mcp", "max_results": 5},
        config_schema={
            "fields": [
                {"key": "api_key", "label": "Exa API Key", "type": "password", "default": "", "placeholder": "exk_..."},
                {
                    "key": "auth_mode",
                    "label": "Auth mode",
                    "type": "select",
                    "options": [
                        {"value": "auto", "label": "Auto (API key when configured, otherwise keyless Exa MCP)"},
                        {"value": "keyless", "label": "Keyless Exa MCP"},
                        {"value": "api_key", "label": "API key required"},
                    ],
                    "default": "auto",
                },
                {
                    "key": "mcp_url",
                    "label": "Exa MCP URL",
                    "type": "text",
                    "default": "https://mcp.exa.ai/mcp",
                },
                {
                    "key": "max_results",
                    "label": "Default results count",
                    "type": "number",
                    "default": 5,
                    "min": 1,
                    "max": 20,
                },
            ]
        },
    )
)
async def exa_search(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _exa_search

    return await _exa_search(arguments)


@tool(
    ToolMeta(
        name="exa_fetch",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Exa Fetch for reading a known URL through Exa MCP. Works with no API key by default via Exa MCP; "
            "configured keys upgrade limits/control.\n\n"
            "Usage:\n"
            "- Use after Exa or other search returns a URL and you want Exa's fetched page content.\n"
            "- For ordinary static pages, `web_fetch` is still the lightest first reader; use `advanced_web_fetch` when unsure."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch through Exa MCP."},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 30000},
            },
            "required": ["url"],
        },
        category="search",
        display_name="Exa Fetch",
        icon="\U0001f4d6",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
        config={"api_key": "", "auth_mode": "auto", "mcp_url": "https://mcp.exa.ai/mcp"},
        config_schema={
            "fields": [
                {"key": "api_key", "label": "Exa API Key", "type": "password", "default": "", "placeholder": "exk_..."},
                {
                    "key": "auth_mode",
                    "label": "Auth mode",
                    "type": "select",
                    "options": [
                        {"value": "auto", "label": "Auto (API key when configured, otherwise keyless Exa MCP)"},
                        {"value": "keyless", "label": "Keyless Exa MCP"},
                        {"value": "api_key", "label": "API key required"},
                    ],
                    "default": "auto",
                },
                {"key": "mcp_url", "label": "Exa MCP URL", "type": "text", "default": "https://mcp.exa.ai/mcp"},
            ]
        },
    )
)
async def exa_fetch(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _exa_fetch

    return await _exa_fetch(arguments)


@tool(
    ToolMeta(
        name="tavily_search",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Tavily real-time web access layer for AI agents and RAG workflows: retrieve live web data, relevant "
            "content/chunks, optional generated answers, raw content, and topic-specific searches.\n\n"
            "Usage:\n"
            "- This is a provider-backed escalation tool discovered through `tool_search`; start with `web_search` unless Tavily is clearly needed.\n"
            "- Use Tavily when you need real-time web access, current factual retrieval, RAG-friendly content, or topic routing (`general`, `news`, `finance`).\n"
            "- `search_depth` controls latency vs. relevance: ultra-fast/fast for speed, basic for balanced lookup, advanced for high-relevance chunks.\n"
            "- Use `time_range`, `start_date`, or `end_date` for freshness; use `include_answer` for a sourced provider answer; use `include_raw_content` when the model needs cleaned page content.\n"
            "- Works with no API key by default via Tavily keyless search; a Tavily API key upgrades limits without changing result parsing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Concise web search query for Tavily."},
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return.",
                    "minimum": 1,
                    "maximum": 20,
                },
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced", "fast", "ultra-fast"],
                    "description": "Tavily search depth: basic balanced, advanced highest relevance, fast/ultra-fast lower latency.",
                },
                "topic": {
                    "type": "string",
                    "enum": ["general", "news", "finance"],
                    "description": "Tavily topic category. Use news for real-time mainstream updates; finance for finance-oriented retrieval.",
                },
                "time_range": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year", "d", "w", "m", "y"],
                    "description": "Optional recency window based on publish/update date.",
                },
                "start_date": {"type": "string", "description": "Optional YYYY-MM-DD start date."},
                "end_date": {"type": "string", "description": "Optional YYYY-MM-DD end date."},
                "include_answer": {
                    "description": "Optional provider-generated answer: true, false, 'basic', or 'advanced'.",
                    "anyOf": [
                        {"type": "boolean"},
                        {"type": "string", "enum": ["basic", "advanced"]},
                    ],
                },
                "include_raw_content": {
                    "description": "Optional cleaned raw content: true, false, 'markdown', or 'text'.",
                    "anyOf": [
                        {"type": "boolean"},
                        {"type": "string", "enum": ["markdown", "text"]},
                    ],
                },
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional domains to restrict results to.",
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional domains to exclude from results.",
                },
            },
            "required": ["query"],
        },
        category="search",
        display_name="Tavily Search",
        icon="\U0001f50e",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
        config={"api_key": "", "auth_mode": "auto", "max_results": 5},
        config_schema={
            "fields": [
                {
                    "key": "api_key",
                    "label": "Tavily API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "tvly-...",
                },
                {
                    "key": "auth_mode",
                    "label": "Auth mode",
                    "type": "select",
                    "options": [
                        {"value": "auto", "label": "Auto (API key when configured, otherwise keyless)"},
                        {"value": "keyless", "label": "Keyless"},
                        {"value": "api_key", "label": "API key required"},
                    ],
                    "default": "auto",
                },
                {
                    "key": "max_results",
                    "label": "Default results count",
                    "type": "number",
                    "default": 5,
                    "min": 1,
                    "max": 20,
                },
            ]
        },
    )
)
async def tavily_search(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _tavily_search

    return await _tavily_search(arguments)


@tool(
    ToolMeta(
        name="tavily_extract",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Tavily Extract for reading a known URL with Tavily's extraction API. Works with no API key by default; "
            "configured keys upgrade limits.\n\n"
            "Usage:\n"
            "- Use when you already have a URL and want Tavily Extract's cleaned content.\n"
            "- Use `extract_depth=advanced` for harder pages; keep `format=markdown` for LLM-ready reading."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to extract."},
                "extract_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "Tavily extraction depth. Default basic.",
                },
                "format": {"type": "string", "enum": ["markdown", "text"], "description": "Output format."},
                "include_images": {"type": "boolean", "description": "Include image metadata when supported."},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 30000},
            },
            "required": ["url"],
        },
        category="search",
        display_name="Tavily Extract",
        icon="\U0001f4c4",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
        config={"api_key": "", "auth_mode": "auto"},
        config_schema={
            "fields": [
                {
                    "key": "api_key",
                    "label": "Tavily API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "tvly-...",
                },
                {
                    "key": "auth_mode",
                    "label": "Auth mode",
                    "type": "select",
                    "options": [
                        {"value": "auto", "label": "Auto (API key when configured, otherwise keyless)"},
                        {"value": "keyless", "label": "Keyless"},
                        {"value": "api_key", "label": "API key required"},
                    ],
                    "default": "auto",
                },
            ]
        },
    )
)
async def tavily_extract(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _tavily_extract

    return await _tavily_extract(arguments)


# ── web_fetch ───────────────────────────────────────────────────────


@tool(
    ToolMeta(
        name="web_fetch",
        timeout_seconds=60.0,
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
    )
)
async def web_fetch(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _web_fetch

    return await _web_fetch(arguments)


# ── firecrawl_search ─────────────────────────────────────────────────


@tool(
    ToolMeta(
        name="firecrawl_search",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Firecrawl `/search` for keyword search with optional result content scraping. Works with no API key "
            "by default; configured keys or self-hosted base URLs upgrade limits/control.\n\n"
            "Usage:\n"
            "- Use when search results should include scraped LLM-ready content, or when `advanced_web_search` routes to Firecrawl.\n"
            "- Do not use this for a known URL; use `firecrawl_fetch` or `advanced_web_fetch` instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                "include_content": {
                    "type": "boolean",
                    "description": "Ask Firecrawl Search to scrape markdown content for results.",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional Firecrawl source filters when supported.",
                },
            },
            "required": ["query"],
        },
        category="search",
        display_name="Firecrawl Search",
        icon="\U0001f50d",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
        config={"api_key": "", "auth_mode": "auto", "base_url": "https://api.firecrawl.dev", "max_results": 5},
        config_schema={
            "fields": [
                {
                    "key": "api_key",
                    "label": "Firecrawl API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "fc-...",
                },
                {
                    "key": "auth_mode",
                    "label": "Auth mode",
                    "type": "select",
                    "options": [
                        {"value": "auto", "label": "Auto (API key when configured, otherwise keyless)"},
                        {"value": "keyless", "label": "Keyless"},
                        {"value": "api_key", "label": "API key required"},
                    ],
                    "default": "auto",
                },
                {
                    "key": "base_url",
                    "label": "Firecrawl API Base URL",
                    "type": "text",
                    "default": "https://api.firecrawl.dev",
                },
                {
                    "key": "max_results",
                    "label": "Default results count",
                    "type": "number",
                    "default": 5,
                    "min": 1,
                    "max": 20,
                },
            ]
        },
    )
)
async def firecrawl_search(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _firecrawl_search

    return await _firecrawl_search(arguments)


# ── firecrawl_fetch ──────────────────────────────────────────────────


@tool(
    ToolMeta(
        name="firecrawl_fetch",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Fetch a known URL with Firecrawl `/scrape` for single-page extraction into LLM-ready markdown, "
            "summary, HTML, links, screenshots, or structured JSON.\n\n"
            "Usage:\n"
            "- This is a provider-backed escalation tool discovered through `tool_search`; start with `web_fetch` for known URLs unless you already know the page needs rendering.\n"
            "- Use this when you already have a URL and `web_fetch` is incomplete, too noisy, blocked, or missing rendered content.\n"
            "- Prefer `formats=['markdown']` for normal reading; add `links`, `summary`, `html`, `screenshot`, or `json` only when the downstream task needs those formats.\n"
            "- Use `only_main_content`, `only_clean_content`, `include_tags`, `exclude_tags`, or `wait_for_ms` to reduce page chrome or wait for JS-rendered content.\n"
            "- Do NOT use this for keyword search. If you do not have a URL yet, search first.\n"
            "- Works with no API key by default on Firecrawl keyless `/scrape`; a Firecrawl API key or self-hosted base URL can be configured for production limits/control."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch and extract."},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 12000, max 30000)"},
                "formats": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["markdown", "summary", "html", "rawHtml", "links", "screenshot", "json"],
                    },
                    "description": "Firecrawl output formats. Default ['markdown']; request richer formats only when needed.",
                },
                "only_main_content": {
                    "type": "boolean",
                    "description": "Prefer extracting just the main article/body content. Default true.",
                },
                "only_clean_content": {
                    "type": "boolean",
                    "description": "Run Firecrawl's additional clean-content pass to remove residual boilerplate. Default false.",
                },
                "wait_for_ms": {
                    "type": "integer",
                    "description": "Optional milliseconds to wait before extracting content when the page renders slowly.",
                },
                "include_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional HTML tags/selectors Firecrawl should include.",
                },
                "exclude_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional HTML tags/selectors Firecrawl should exclude.",
                },
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
        config={"api_key": "", "auth_mode": "auto", "base_url": "https://api.firecrawl.dev"},
        config_schema={
            "fields": [
                {
                    "key": "api_key",
                    "label": "Firecrawl API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "fc-...",
                },
                {
                    "key": "auth_mode",
                    "label": "Auth mode",
                    "type": "select",
                    "options": [
                        {"value": "auto", "label": "Auto (API key when configured, otherwise keyless)"},
                        {"value": "keyless", "label": "Keyless"},
                        {"value": "api_key", "label": "API key required"},
                    ],
                    "default": "auto",
                },
                {
                    "key": "base_url",
                    "label": "Firecrawl API Base URL",
                    "type": "text",
                    "default": "https://api.firecrawl.dev",
                },
            ]
        },
    )
)
async def firecrawl_fetch(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _firecrawl_fetch

    return await _firecrawl_fetch(arguments)


# ── xcrawl_scrape ────────────────────────────────────────────────────


@tool(
    ToolMeta(
        name="xcrawl_scrape",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Scrape a known URL with the XCrawl Scrape API for single-page extraction using official "
            "`output.formats`, `request`, and `js_render` options.\n\n"
            "Usage:\n"
            "- This is a provider-backed escalation tool discovered through `tool_search`; use it only after lighter web readers are insufficient.\n"
            "- Use this for difficult or JS-rendered pages where you need XCrawl's JS rendering, proxy controls, device/locale options, or structured extraction formats.\n"
            "- Prefer `output_formats=['markdown']` for normal reading; add `links`, `summary`, `html`, `screenshot`, or `json` only when needed.\n"
            "- This tool runs the sync single-page scrape path; do not treat it as site map, site crawl, or keyword search.\n"
            "- Do NOT use this for keyword search. If you do not have a URL yet, search first.\n"
            "- This tool is provider-backed and requires XCrawl configuration."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to scrape."},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 12000, max 30000)"},
                "output_formats": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["markdown", "html", "raw_html", "links", "summary", "screenshot", "json"],
                    },
                    "description": "XCrawl output.formats for the sync scrape call. Default ['markdown'].",
                },
                "only_main_content": {
                    "type": "boolean",
                    "description": "XCrawl request.only_main_content. Default true.",
                },
                "block_ads": {
                    "type": "boolean",
                    "description": "XCrawl request.block_ads. Default true.",
                },
                "js_render": {"type": "boolean", "description": "Enable JS rendering. Default true."},
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle"],
                    "description": "XCrawl js_render.wait_until option.",
                },
                "device": {
                    "type": "string",
                    "enum": ["desktop", "mobile"],
                    "description": "XCrawl request.device option.",
                },
                "locale": {
                    "type": "string",
                    "description": "XCrawl request.locale / Accept-Language value.",
                },
                "proxy_location": {
                    "type": "string",
                    "description": "XCrawl proxy.location ISO country code, e.g. US, JP, or SG.",
                },
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
        config_schema={
            "fields": [
                {
                    "key": "api_key",
                    "label": "XCrawl API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "xcr_...",
                }
            ]
        },
    )
)
async def xcrawl_scrape(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _xcrawl_scrape

    return await _xcrawl_scrape(arguments)


# ── discover_resources ───────────────────────────────────────────────


@tool(
    ToolMeta(
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
        config_schema={
            "fields": [
                {
                    "key": "smithery_api_key",
                    "label": "Smithery API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get from smithery.ai/account/api-keys",
                },
                {
                    "key": "modelscope_api_token",
                    "label": "ModelScope API Token",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get from modelscope.cn",
                },
            ]
        },
    )
)
async def discover_resources(arguments: dict) -> str:
    from app.services.agent_tool_domains.web_mcp import _discover_resources

    return await _discover_resources(arguments)


# ── search_clawhub ──────────────────────────────────────────────────


@tool(
    ToolMeta(
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
    )
)
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
