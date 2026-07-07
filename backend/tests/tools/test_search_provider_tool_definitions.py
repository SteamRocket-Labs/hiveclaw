from __future__ import annotations


def _function_schema(tool_name: str) -> dict:
    from app.tools.collector import collect_tools

    for tool in collect_tools().openai_tools:
        fn = tool["function"]
        if fn["name"] == tool_name:
            return fn
    raise AssertionError(f"Tool {tool_name} not found")


def test_exa_search_definition_exposes_ai_native_search_surface() -> None:
    fn = _function_schema("exa_search")
    description = fn["description"]
    properties = fn["parameters"]["properties"]

    assert "AI-native search" in description
    assert "no API key" in description
    assert "Exa MCP" in description
    assert "search type" in description
    assert "category verticals" in description
    assert properties["search_type"]["enum"] == ["auto", "instant", "fast", "deep-lite", "deep", "deep-reasoning"]
    assert properties["category"]["enum"] == [
        "company",
        "research paper",
        "news",
        "personal site",
        "financial report",
        "people",
    ]
    assert "include_domains" in properties
    assert "start_published_date" in properties


def test_tavily_search_definition_exposes_agent_realtime_search_surface() -> None:
    fn = _function_schema("tavily_search")
    description = fn["description"]
    properties = fn["parameters"]["properties"]

    assert "real-time web access layer for AI agents" in description
    assert "no API key" in description
    assert "topic" in description
    assert "search_depth" in description
    assert properties["search_depth"]["enum"] == ["basic", "advanced", "fast", "ultra-fast"]
    assert properties["topic"]["enum"] == ["general", "news", "finance"]
    assert "time_range" in properties
    assert "include_answer" in properties
    assert "include_raw_content" in properties


def test_web_search_definition_exposes_basic_provider_surface_without_anysearch_primary() -> None:
    fn = _function_schema("web_search")
    description = fn["description"]

    assert "built-in basic provider chain" in description
    assert "AnySearch API first" not in description
    assert "SearXNG" in description
    assert "DuckDuckGo" not in description
    assert "web_fetch" in description
    assert "tool_search" in description


def test_anysearch_vertical_tools_expose_mcp_search_surface() -> None:
    get_sub_domains = _function_schema("anysearch_get_sub_domains")
    search = _function_schema("anysearch_search")
    batch_search = _function_schema("anysearch_batch_search")
    extract = _function_schema("anysearch_extract")

    assert "AnySearch MCP" in get_sub_domains["description"]
    assert "before vertical search" in get_sub_domains["description"]
    assert "search/discovery surface" in get_sub_domains["description"]
    assert {"domain", "domains"} <= set(get_sub_domains["parameters"]["properties"])

    search_properties = search["parameters"]["properties"]
    assert "AnySearch MCP" in search["description"]
    assert "search/discovery surface" in search["description"]
    assert "domain" in search_properties
    assert "sub_domain" in search_properties
    assert "sub_domain_params" in search_properties
    assert search_properties["sub_domain_params"]["type"] == "object"

    assert "2-5" in batch_search["description"]
    assert batch_search["parameters"]["properties"]["queries"]["type"] == "array"
    assert (
        batch_search["parameters"]["properties"]["queries"]["items"]["properties"]["sub_domain_params"]["type"]
        == "object"
    )

    assert "full page content" in extract["description"]
    assert "read/extract surface" in extract["description"]
    assert "not a keyword-search tool" in extract["description"]
    assert extract["parameters"]["required"] == ["url"]


def test_advanced_web_search_definition_exposes_router_surface() -> None:
    fn = _function_schema("advanced_web_search")
    description = fn["description"]
    properties = fn["parameters"]["properties"]

    assert "Advanced web search router" in description
    assert "no-key providers by default" in description
    assert "AnySearch" in description
    assert "Exa" in description
    assert "Tavily" in description
    assert "Firecrawl Search" in description
    assert properties["intent"]["enum"] == [
        "auto",
        "vertical",
        "current",
        "news",
        "finance",
        "semantic",
        "company",
        "research_paper",
        "content",
    ]
    assert properties["provider"]["enum"] == ["auto", "anysearch", "exa", "tavily", "firecrawl"]
    assert "include_content" in properties
    assert "sub_domain_params" in properties


def test_advanced_web_fetch_definition_exposes_router_surface() -> None:
    fn = _function_schema("advanced_web_fetch")
    description = fn["description"]
    properties = fn["parameters"]["properties"]

    assert "Advanced web fetch router" in description
    assert "web_fetch" in description
    assert "Firecrawl" in description
    assert "Tavily Extract" in description
    assert "Exa Fetch" in description
    assert "AnySearch Extract" in description
    assert "XCrawl only when configured" in description
    assert properties["provider"]["enum"] == ["auto", "web_fetch", "firecrawl", "tavily", "exa", "anysearch", "xcrawl"]
    assert "prefer_rendered" in properties
    assert "skip_core" in properties


def test_provider_fetch_and_search_definitions_expose_no_key_surfaces() -> None:
    firecrawl_search = _function_schema("firecrawl_search")
    tavily_extract = _function_schema("tavily_extract")
    exa_fetch = _function_schema("exa_fetch")

    assert "Firecrawl `/search`" in firecrawl_search["description"]
    assert "no API key" in firecrawl_search["description"]
    assert "include_content" in firecrawl_search["parameters"]["properties"]
    assert firecrawl_search["parameters"]["required"] == ["query"]

    assert "Tavily Extract" in tavily_extract["description"]
    assert "no API key" in tavily_extract["description"]
    assert tavily_extract["parameters"]["required"] == ["url"]
    assert tavily_extract["parameters"]["properties"]["extract_depth"]["enum"] == ["basic", "advanced"]

    assert "Exa Fetch" in exa_fetch["description"]
    assert "Exa MCP" in exa_fetch["description"]
    assert "no API key" in exa_fetch["description"]
    assert exa_fetch["parameters"]["required"] == ["url"]


def test_firecrawl_fetch_definition_exposes_current_scrape_surface() -> None:
    fn = _function_schema("firecrawl_fetch")
    description = fn["description"]
    properties = fn["parameters"]["properties"]

    assert "Firecrawl `/scrape`" in description
    assert "no API key" in description
    assert "LLM-ready markdown" in description
    assert "formats" in description
    assert properties["formats"]["items"]["enum"] == [
        "markdown",
        "summary",
        "html",
        "rawHtml",
        "links",
        "screenshot",
        "json",
    ]
    assert "only_clean_content" in properties
    assert "wait_for_ms" in properties
    assert "include_tags" in properties
    assert "exclude_tags" in properties


def test_xcrawl_scrape_definition_exposes_official_scrape_surface() -> None:
    fn = _function_schema("xcrawl_scrape")
    description = fn["description"]
    properties = fn["parameters"]["properties"]

    assert "XCrawl Scrape API" in description
    assert "single-page extraction" in description
    assert "output.formats" in description
    assert properties["output_formats"]["items"]["enum"] == [
        "markdown",
        "html",
        "raw_html",
        "links",
        "summary",
        "screenshot",
        "json",
    ]
    assert "only_main_content" in properties
    assert "wait_until" in properties
    assert properties["wait_until"]["enum"] == ["load", "domcontentloaded", "networkidle"]
    assert properties["device"]["enum"] == ["desktop", "mobile"]
