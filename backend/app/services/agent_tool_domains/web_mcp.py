from __future__ import annotations

import html
from html.parser import HTMLParser
import itertools
import json
import logging
from pathlib import Path
import re
import uuid
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session, tenant_scoped_session
from app.services.document_conversion import (
    DocumentConversionResult,
    DocumentConversionService,
    render_conversion_preview,
)
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.tools.result_envelope import classify_http_status, render_tool_error, render_tool_fallback

logger = logging.getLogger(__name__)


def _safe_int(value, default: int) -> int:
    """Safely cast LLM-provided value to int, falling back to *default*."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _string_list(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _split_config_list(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[\n,;]+", value) if part.strip()]
    return []


def _optional_enum(value: object, allowed: set[str], default: str | None = None) -> str | None:
    text = str(value or "").strip().lower()
    if text in allowed:
        return text
    return default


def _enum_list(value: object, allowed: tuple[str, ...], default: list[str]) -> list[str]:
    requested = _string_list(value)
    if not requested:
        return list(default)
    canonical = {item.lower(): item for item in allowed}
    selected: list[str] = []
    for item in requested:
        mapped = canonical.get(item.lower())
        if mapped and mapped not in selected:
            selected.append(mapped)
    return selected or list(default)


def _optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    return bool(value)


def _provider_auth_mode(config: dict, *, key: str = "auth_mode", default: str = "auto") -> str:
    return _optional_enum(config.get(key), _WEB_PROVIDER_AUTH_MODES, default=default) or default


def _anysearch_auth_mode(config: dict) -> str:
    mode = config.get("anysearch_auth_mode")
    if mode is None:
        mode = config.get("auth_mode")
    return _optional_enum(mode, _WEB_PROVIDER_AUTH_MODES, default="auto") or "auto"


def _truncate_result_text(text: object, max_chars: int) -> str:
    result = str(text or "").strip()
    if len(result) > max_chars:
        return result[:max_chars] + f"\n\n[... truncated at {max_chars} chars]"
    return result


_URL_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/.*)?$")
_SKIP_CRAWLER_FALLBACK = "_skip_crawler_fallback"
_SKIP_WEB_FETCH_FALLBACK = "_skip_web_fetch_fallback"
_SKIP_FIRECRAWL_FALLBACK = "_skip_firecrawl_fallback"
_EXA_SEARCH_TYPES = {"auto", "instant", "fast", "deep-lite", "deep", "deep-reasoning"}
_EXA_CATEGORIES = {"company", "research paper", "news", "personal site", "financial report", "people"}
_TAVILY_SEARCH_DEPTHS = {"basic", "advanced", "fast", "ultra-fast"}
_TAVILY_TOPICS = {"general", "news", "finance"}
_TAVILY_TIME_RANGES = {"day", "week", "month", "year", "d", "w", "m", "y"}
_TAVILY_EXTRACT_DEPTHS = {"basic", "advanced"}
_WEB_PROVIDER_AUTH_MODES = {"auto", "api_key", "keyless"}
_ADVANCED_WEB_SEARCH_INTENTS = {
    "auto",
    "vertical",
    "current",
    "news",
    "finance",
    "semantic",
    "company",
    "research_paper",
    "content",
}
_ADVANCED_WEB_SEARCH_PROVIDERS = {"auto", "anysearch", "exa", "tavily", "firecrawl"}
_ADVANCED_WEB_FETCH_PROVIDERS = {"auto", "web_fetch", "firecrawl", "tavily", "exa", "anysearch", "xcrawl"}
_ANYSEARCH_API_URL = "https://api.anysearch.com/v1/search"
_ANYSEARCH_MCP_URL = "https://api.anysearch.com/mcp"
_ANYSEARCH_ZONES = {"intl", "cn"}
_ANYSEARCH_LOCAL_COUNTER = itertools.count()
_EXA_MCP_URL = "https://mcp.exa.ai/mcp"
_FIRECRAWL_API_BASE_URL = "https://api.firecrawl.dev"
_FIRECRAWL_FORMATS = ("markdown", "summary", "html", "rawHtml", "links", "screenshot", "json")
_XCRAWL_OUTPUT_FORMATS = ("markdown", "html", "raw_html", "links", "summary", "screenshot", "json")
_XCRAWL_WAIT_UNTIL = {"load", "domcontentloaded", "networkidle"}
_XCRAWL_DEVICES = {"desktop", "mobile"}
_WEB_FETCH_CONVERSION_ROOT = Path(get_settings().AGENT_DATA_DIR) / ".hive" / "web_fetch"


def _tool_visible_to_agent_tenant(tool, agent) -> bool:
    tool_tenant_id = getattr(tool, "tenant_id", None)
    if tool_tenant_id is None:
        return True
    agent_tenant_id = getattr(agent, "tenant_id", None)
    return bool(agent_tenant_id) and str(tool_tenant_id) == str(agent_tenant_id)


def _result_scalars_or_one(result) -> list:
    try:
        values = list(result.scalars().all())
        if values:
            return values
    except Exception:
        pass
    try:
        value = result.scalar_one_or_none()
    except Exception:
        return []
    return [value] if value is not None else []


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in {"p", "div", "section", "article", "main", "h1", "h2", "h3", "li", "br"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def get_text(self) -> str:
        raw = " ".join(self._parts)
        raw = html.unescape(raw)
        return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", raw)).strip()


def _looks_like_url(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate or " " in candidate:
        return False
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    return bool(parsed.netloc and "." in parsed.netloc)


def _normalize_url(value: str) -> str | None:
    candidate = (value or "").strip()
    if not _looks_like_url(candidate):
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    return candidate


def _is_feishu_open_api_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc == "open.feishu.cn" and parsed.path.startswith("/open-apis/")


def _invalid_argument_error(tool_name: str, message: str, *, provider: str, hint: str) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class="bad_arguments",
        message=message,
        provider=provider,
        retryable=False,
        actionable_hint=hint,
    )


def _http_error(tool_name: str, *, provider: str, status_code: int, detail: str, hint: str | None = None) -> str:
    error_class, retryable = classify_http_status(status_code)
    return render_tool_error(
        tool_name=tool_name,
        error_class=error_class,
        message=f"{tool_name} failed with HTTP {status_code}: {detail[:200]}",
        provider=provider,
        http_status=status_code,
        retryable=retryable,
        actionable_hint=hint,
    )


def _extract_text_with_trafilatura(markup: str, *, url: str | None = None) -> str:
    try:
        import trafilatura
    except Exception as e:
        logger.debug("Trafilatura HTML extraction unavailable: %s", e)
        return ""
    try:
        extracted = trafilatura.extract(
            markup,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
    except Exception as e:
        logger.debug("Trafilatura HTML extraction failed: %s", e)
        return ""
    return (extracted or "").strip()


def _extract_text_from_html(markup: str, *, url: str | None = None) -> str:
    trafilatura_text = _extract_text_with_trafilatura(markup, url=url)
    if trafilatura_text:
        return trafilatura_text
    parser = _HTMLTextExtractor()
    parser.feed(markup)
    return parser.get_text()


def _looks_like_incomplete_rendered_page(markup: str, extracted_text: str) -> bool:
    if len((extracted_text or "").strip()) >= 120:
        return False
    lowered = (markup or "").lower()
    js_shell_markers = (
        'id="root"',
        "id='root'",
        'id="app"',
        "id='app'",
        "__next",
        "data-reactroot",
        "window.__",
        "<script",
    )
    return any(marker in lowered for marker in js_shell_markers)


def _provider_result_failed(result: str) -> bool:
    normalized = (result or "").strip()
    return normalized.startswith("❌") or "<tool_error>" in normalized


def _provider_failure_message(result: str, engine: str) -> str:
    normalized = (result or "").strip()
    if not normalized:
        return f"web_search provider '{engine}' returned no usable content"
    first_line = normalized.splitlines()[0].strip()
    return first_line.removeprefix("❌").strip() or f"web_search provider '{engine}' failed"


def _anysearch_api_keys(config: dict) -> list[str]:
    keys = _split_config_list(config.get("anysearch_api_keys"))
    if keys:
        return keys
    return _split_config_list(get_settings().ANYSEARCH_API_KEYS)


def _anysearch_content_types(config: dict) -> list[str]:
    configured = _split_config_list(config.get("anysearch_content_types"))
    if configured:
        return configured
    settings_value = _split_config_list(get_settings().ANYSEARCH_DEFAULT_CONTENT_TYPES)
    return settings_value or ["web"]


def _anysearch_zone(config: dict) -> str:
    zone = str(config.get("anysearch_zone") or get_settings().ANYSEARCH_DEFAULT_ZONE or "intl").strip().lower()
    return zone if zone in _ANYSEARCH_ZONES else "intl"


async def _next_anysearch_key_start_index(keys: list[str], scope: str) -> int:
    if not keys:
        return 0
    try:
        from app.core.events import get_redis

        redis = await get_redis()
        value = await redis.incr(f"web_search:anysearch:key_index:{scope}")
        return (int(value) - 1) % len(keys)
    except Exception as e:
        logger.debug("AnySearch Redis key rotation unavailable: %s", e)
        return next(_ANYSEARCH_LOCAL_COUNTER) % len(keys)


def _ordered_anysearch_keys(keys: list[str], start_index: int) -> list[str]:
    if not keys:
        return []
    start = start_index % len(keys)
    return keys[start:] + keys[:start]


def _anysearch_timeout(config: dict) -> int:
    return max(3, min(_safe_int(config.get("anysearch_timeout_seconds"), get_settings().ANYSEARCH_TIMEOUT_SECONDS), 30))


def _anysearch_mcp_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    result = data.get("result")
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"].strip())
        if parts:
            return "\n\n".join(part for part in parts if part).strip()
    if isinstance(result.get("text"), str):
        return result["text"].strip()
    if isinstance(result.get("structuredContent"), dict):
        return json.dumps(result["structuredContent"], ensure_ascii=False, indent=2)
    return ""


def _anysearch_json_rpc_error_message(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or error.get("code") or "AnySearch MCP JSON-RPC error").strip()
    return message or "AnySearch MCP JSON-RPC error"


def _anysearch_should_retry_json_rpc_error(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in ("rate", "quota", "limit", "timeout", "temporar", "unavailable"))


def _with_anysearch_max_results(arguments: dict, value: object) -> dict:
    max_results = max(1, min(_safe_int(value, 10), 10))
    result = dict(arguments)
    result["max_results"] = max_results
    return result


async def _call_anysearch_mcp_tool(public_tool_name: str, mcp_tool_name: str, arguments: dict) -> str:
    config = await _get_tool_config("web_search")
    keys = _anysearch_api_keys(config)
    auth_mode = _anysearch_auth_mode(config)
    if auth_mode == "api_key" and not keys:
        return render_tool_error(
            tool_name=public_tool_name,
            error_class="provider_not_configured",
            message=f"{public_tool_name} requires configured AnySearch API keys.",
            provider="anysearch_mcp",
            retryable=False,
            actionable_hint=(
                "Set anysearch_auth_mode=auto/keyless for anonymous AnySearch MCP access, "
                "or configure AnySearch API keys on web_search."
            ),
        )

    key_scope = str(config.get("anysearch_key_scope") or "global").strip() or "global"
    start_index = await _next_anysearch_key_start_index(keys, key_scope)
    ordered_keys: list[str | None] = _ordered_anysearch_keys(keys, start_index) or [None]
    timeout = _anysearch_timeout(config)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": mcp_tool_name, "arguments": arguments},
    }
    retryable_statuses = {401, 402, 403, 408, 429, 500, 502, 503, 504}
    last_status: int | None = None
    last_error = "AnySearch MCP call failed: no request attempted"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for api_key in ordered_keys:
            headers = {"User-Agent": "Hive WebSearch/1.0"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            resp = await client.post(_ANYSEARCH_MCP_URL, json=payload, headers=headers, timeout=timeout)
            last_status = resp.status_code
            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code != 200:
                last_error = f"AnySearch MCP {mcp_tool_name} failed with HTTP {resp.status_code}: {resp.text[:200]}"
                if api_key and resp.status_code in retryable_statuses:
                    continue
                return _http_error(
                    public_tool_name,
                    provider="anysearch_mcp",
                    status_code=resp.status_code,
                    detail=resp.text,
                    hint="Retry later, add another AnySearch API key, or use basic web_search/SearXNG fallback.",
                )

            rpc_error = _anysearch_json_rpc_error_message(data)
            if rpc_error:
                last_error = f"AnySearch MCP {mcp_tool_name} failed: {rpc_error[:200]}"
                if api_key and _anysearch_should_retry_json_rpc_error(rpc_error):
                    continue
                return render_tool_error(
                    tool_name=public_tool_name,
                    error_class="provider_error",
                    message=last_error,
                    provider="anysearch_mcp",
                    retryable=_anysearch_should_retry_json_rpc_error(rpc_error),
                    actionable_hint="Check the selected domain, sub_domain, and required sub_domain_params.",
                )

            text = _anysearch_mcp_text(data)
            if text:
                return text
            last_error = f"AnySearch MCP {mcp_tool_name} returned no readable text content"

    error_class = classify_http_status(last_status)[0] if last_status else "provider_error"
    retryable = classify_http_status(last_status)[1] if last_status else True
    return render_tool_error(
        tool_name=public_tool_name,
        error_class=error_class,
        message=last_error,
        provider="anysearch_mcp",
        http_status=last_status,
        retryable=retryable,
        actionable_hint="Retry later, add another AnySearch API key, or use basic web_search/SearXNG fallback.",
    )


async def _anysearch_get_sub_domains(arguments: dict) -> str:
    domain = _optional_string(arguments.get("domain"))
    domains = _split_config_list(arguments.get("domains"))
    if not domain and not domains:
        return _invalid_argument_error(
            "anysearch_get_sub_domains",
            "anysearch_get_sub_domains requires domain or domains.",
            provider="anysearch_mcp",
            hint="Pass a single domain such as 'finance', or up to five domains such as ['finance', 'academic'].",
        )
    payload: dict[str, object] = {}
    if domains:
        payload["domains"] = ",".join(domains[:5])
    elif domain:
        payload["domain"] = domain
    return await _call_anysearch_mcp_tool("anysearch_get_sub_domains", "get_sub_domains", payload)


async def _anysearch_search(arguments: dict) -> str:
    query = _optional_string(arguments.get("query"))
    if not query:
        return _invalid_argument_error(
            "anysearch_search",
            "anysearch_search requires a non-empty query.",
            provider="anysearch_mcp",
            hint="Pass concise search keywords. For vertical search, call anysearch_get_sub_domains first.",
        )
    payload: dict[str, object] = {"query": query}
    for key in ("domain", "sub_domain"):
        value = _optional_string(arguments.get(key))
        if value:
            payload[key] = value
    sub_domain_params = arguments.get("sub_domain_params")
    if isinstance(sub_domain_params, dict) and sub_domain_params:
        payload["sub_domain_params"] = sub_domain_params
    elif isinstance(sub_domain_params, str) and sub_domain_params.strip():
        payload["sub_domain_params"] = sub_domain_params.strip()
    if "max_results" in arguments:
        payload = _with_anysearch_max_results(payload, arguments.get("max_results"))
    return await _call_anysearch_mcp_tool("anysearch_search", "search", payload)


async def _anysearch_batch_search(arguments: dict) -> str:
    queries = arguments.get("queries")
    if not isinstance(queries, list) or not (2 <= len(queries) <= 5):
        return _invalid_argument_error(
            "anysearch_batch_search",
            "anysearch_batch_search requires 2-5 query objects.",
            provider="anysearch_mcp",
            hint="Pass queries=[{'query': '...'}, {'query': '...', 'domain': 'finance', 'sub_domain': '...'}].",
        )
    max_results = arguments.get("max_results")
    payload_queries = []
    for item in queries:
        if not isinstance(item, dict) or not _optional_string(item.get("query")):
            return _invalid_argument_error(
                "anysearch_batch_search",
                "Each AnySearch batch item requires a non-empty query.",
                provider="anysearch_mcp",
                hint="Use query objects with at least {'query': '...'}; add domain/sub_domain/sub_domain_params when needed.",
            )
        payload_item = dict(item)
        if max_results is not None and "max_results" not in payload_item:
            payload_item = _with_anysearch_max_results(payload_item, max_results)
        payload_queries.append(payload_item)
    return await _call_anysearch_mcp_tool("anysearch_batch_search", "batch_search", {"queries": payload_queries})


async def _anysearch_extract(arguments: dict) -> str:
    url = _optional_string(arguments.get("url"))
    if not url:
        return _invalid_argument_error(
            "anysearch_extract",
            "anysearch_extract requires a URL.",
            provider="anysearch_mcp",
            hint="Pass the URL returned by search when you need AnySearch's full-page Markdown extraction.",
        )
    if not _looks_like_url(url):
        url = f"https://{url}"
    return await _call_anysearch_mcp_tool("anysearch_extract", "extract", {"url": url})


async def _get_tool_config(tool_name: str) -> dict:
    """Resolve tool config with tenant isolation via ContextVar.

    Reads the current tenant_id from the execution context (set by
    ToolExecutionRegistry.try_execute) and merges:
    Tool.config (platform default) → TenantToolConfig.config (tenant override).
    """
    try:
        from app.core.execution_context import get_tool_tenant_id
        from app.services.tool_config_service import resolve_tool_config

        tenant_id = get_tool_tenant_id()
        return await resolve_tool_config(tool_name, tenant_id)
    except Exception as e:
        logger.debug("Suppressed: %s", e)

    try:
        from app.models.tool import Tool

        # RLS 阶段1 / Finding #1: this is the GLOBAL (admin-set) tool-config
        # fallback. Pin `tenant_id IS NULL` so a same-named tenant-owned tool can
        # never leak its config (api_key/private_key) here. Under enforced RLS a
        # bare session already fails closed to NULL-tenant rows — the explicit
        # predicate makes the global-config intent unambiguous either way.
        async with async_session() as db:
            result = await db.execute(select(Tool).where(Tool.name == tool_name, Tool.tenant_id.is_(None)))
            tool = result.scalar_one_or_none()
            return getattr(tool, "config", None) or {}
    except Exception as e:
        logger.debug("Suppressed legacy tool config lookup failure: %s", e)
        return {}


async def _web_search(arguments: dict) -> str:
    query = arguments.get("query", "")
    if not query:
        return _invalid_argument_error(
            "web_search",
            "web_search requires a non-empty query.",
            provider="web_search",
            hint="Pass concise search keywords. If you already have a URL, use web_fetch instead.",
        )
    if _looks_like_url(query):
        return _invalid_argument_error(
            "web_search",
            "web_search expects search keywords, not a URL.",
            provider="web_search",
            hint="Use web_fetch when you already have a specific URL.",
        )

    config = await _get_tool_config("web_search")

    configured_engine = str(config.get("search_engine") or "auto").strip().lower()
    if configured_engine == "duckduckgo":
        configured_engine = "duckduckgo_legacy"
    if configured_engine == "anysearch":
        # AnySearch is an L2 add-on surface exposed through anysearch_* tools.
        # CORE web_search keeps a basic provider boundary even if legacy tenant
        # config still contains search_engine=anysearch.
        configured_engine = "auto"
    engine = configured_engine if configured_engine in {"auto", "searxng", "duckduckgo_legacy"} else "auto"
    max_results = min(_safe_int(arguments.get("max_results", config.get("max_results", 5)), 5), 10)
    language = config.get("language", "en")
    searxng_url = (config.get("searxng_url") or await _get_searxng_url()).strip().rstrip("/")

    if engine == "auto":
        if searxng_url:
            engine = "searxng"
        else:
            return render_tool_error(
                tool_name="web_search",
                error_class="provider_unavailable",
                message="web_search has no configured CORE provider: set SEARXNG_URL.",
                provider="web_search",
                retryable=False,
                actionable_hint="Configure SEARXNG_URL for CORE web_search, or enable/discover AnySearch through the L2 web_pack tools.",
            )
    if engine == "searxng" and not searxng_url:
        return render_tool_error(
            tool_name="web_search",
            error_class="provider_unavailable",
            message="SearXNG is selected but SEARXNG_URL is not configured.",
            provider="searxng",
            retryable=False,
            actionable_hint="Configure SEARXNG_URL, or use tool_search to discover L2 advanced search tools such as AnySearch/Exa/Tavily.",
        )

    try:
        if engine == "searxng":
            result = await _search_searxng(query, searxng_url, max_results, language)
        elif engine == "duckduckgo_legacy":
            result = await _search_duckduckgo(query, max_results)
        else:
            return render_tool_error(
                tool_name="web_search",
                error_class="provider_unavailable",
                message=f"Unsupported web_search provider '{engine}'.",
                provider="web_search",
                retryable=False,
                actionable_hint="Use auto, searxng, or duckduckgo_legacy for CORE web_search; use tool_search for L2 advanced search tools.",
            )
        if _provider_result_failed(result):
            return render_tool_fallback(
                tool_name="web_search",
                error_class="provider_error",
                message=_provider_failure_message(result, engine),
                provider=engine,
                retryable=True,
                actionable_hint="The configured provider returned an unusable response and no fallback provider was available.",
                fallback_tool="web_search:none",
                fallback_result="❌ No fallback search provider was available.",
            )
        return result
    except Exception as e:
        if engine == "duckduckgo_legacy":
            return render_tool_error(
                tool_name="web_search",
                error_class="provider_error",
                message=f"web_search failed: {str(e)[:200]}",
                provider=engine,
                retryable=True,
                actionable_hint="Retry with a more specific query or switch to another search provider.",
            )
        return render_tool_error(
            tool_name="web_search",
            error_class="provider_error",
            message=f"web_search provider '{engine}' failed: {str(e)[:200]}",
            provider=engine,
            retryable=True,
            actionable_hint="Retry later or switch to a different search provider.",
        )


async def _get_searxng_url() -> str:
    return (get_settings().SEARXNG_URL or "").strip().rstrip("/")


async def _search_searxng(query: str, searxng_url: str, max_results: int, language: str) -> str:
    base_url = searxng_url.rstrip("/")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            f"{base_url}/search",
            params={
                "q": query,
                "format": "json",
                "language": language,
                "categories": "general",
            },
            headers={"User-Agent": "Hive WebSearch/1.0"},
            timeout=12,
        )

    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code != 200:
        return f"❌ SearXNG search failed: HTTP {resp.status_code}: {resp.text[:200]}"
    if not isinstance(data, dict):
        return f"❌ SearXNG search failed: unexpected response {str(data)[:200]}"

    results = []
    for item in data.get("results", [])[:max_results]:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        url = item.get("url") or ""
        snippet = item.get("content") or item.get("snippet") or ""
        if not (title or url or snippet):
            continue
        results.append(f"**{title}**\n{url}\n{snippet[:300]}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 SearXNG results for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_anysearch(query: str, config: dict, max_results: int, language: str) -> str:
    keys = _anysearch_api_keys(config)
    key_scope = str(config.get("anysearch_key_scope") or "global").strip() or "global"
    start_index = await _next_anysearch_key_start_index(keys, key_scope)
    ordered_keys: list[str | None] = _ordered_anysearch_keys(keys, start_index) or [None]
    zone = _anysearch_zone(config)
    content_types = _anysearch_content_types(config)
    timeout = max(
        3, min(_safe_int(config.get("anysearch_timeout_seconds"), get_settings().ANYSEARCH_TIMEOUT_SECONDS), 30)
    )

    payload: dict[str, object] = {
        "query": query,
        "max_results": max(1, min(max_results, 100)),
        "zone": zone,
        "language": language,
    }
    if content_types:
        payload["content_types"] = content_types
    domain = _optional_string(config.get("anysearch_domain"))
    if domain:
        payload["domain"] = domain
    tag = _optional_string(config.get("anysearch_tag"))
    if tag:
        payload["tag"] = tag
    params = config.get("anysearch_params")
    if isinstance(params, dict) and params:
        payload["params"] = params

    last_error = "AnySearch search failed: no request attempted"
    retryable_statuses = {401, 402, 403, 429, 500, 502, 503, 504}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for api_key in ordered_keys:
            headers = {"User-Agent": "Hive WebSearch/1.0"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            resp = await client.post(
                _ANYSEARCH_API_URL,
                json=payload,
                headers=headers,
                timeout=timeout,
            )

            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code != 200:
                last_error = f"AnySearch search failed: HTTP {resp.status_code}: {resp.text[:200]}"
                if api_key and resp.status_code in retryable_statuses:
                    continue
                return f"❌ {last_error}"

            result_data = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
            if not isinstance(result_data, dict):
                return f"❌ AnySearch search failed: unexpected response {str(data)[:200]}"
            raw_results = result_data.get("results")
            if not isinstance(raw_results, list):
                return f"❌ AnySearch search failed: unexpected response {str(data)[:200]}"

            results = []
            for item in raw_results[:max_results]:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or ""
                url = item.get("url") or ""
                snippet = item.get("snippet") or item.get("content") or ""
                if not (title or url or snippet):
                    continue
                results.append(f"**{title}**\n{url}\n{str(snippet)[:300]}")

            if not results:
                return f'🔍 No results found for "{query}"'
            return f'🔍 AnySearch results for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)

    return f"❌ {last_error}"


async def _search_duckduckgo(query: str, max_results: int) -> str:
    import re

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=10,
        )

    if resp.status_code >= 300:
        return f"❌ DuckDuckGo search failed: HTTP {resp.status_code}: {resp.text[:200]}"

    results = []
    blocks = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        resp.text,
        re.DOTALL,
    )
    for url, title, snippet in blocks[:max_results]:
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet)).strip()
        if "uddg=" in url:
            from urllib.parse import parse_qs, unquote, urlparse

            parsed = parse_qs(urlparse(url).query)
            url = unquote(parsed.get("uddg", [url])[0])
        results.append(f"**{title}**\n{url}\n{snippet}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 DuckDuckGo results for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _get_exa_api_key() -> str:
    return get_settings().EXA_API_KEY


async def _get_tavily_api_key() -> str:
    try:
        from app.models.system_settings import SystemSetting

        async with async_session() as db:
            result = await db.execute(select(SystemSetting).where(SystemSetting.key == "tavily_api_key"))
            setting = result.scalar_one_or_none()
            if setting and setting.value.get("api_key"):
                return setting.value["api_key"]
    except Exception as e:
        logger.debug("Suppressed: %s", e)
    return get_settings().TAVILY_API_KEY


async def _exa_search(arguments: dict) -> str:
    query = arguments.get("query", "")
    if not query:
        return _invalid_argument_error(
            "exa_search",
            "exa_search requires a non-empty query.",
            provider="exa",
            hint="Pass concise semantic search keywords. If you only need basic lookup, use web_search.",
        )
    if _looks_like_url(query):
        return _invalid_argument_error(
            "exa_search",
            "exa_search expects search keywords, not a URL.",
            provider="exa",
            hint="Use web_fetch when you already have a specific URL.",
        )

    config = await _get_tool_config("exa_search")
    api_key = config.get("api_key") or config.get("exa_api_key") or await _get_exa_api_key()
    auth_mode = _optional_enum(config.get("auth_mode"), _WEB_PROVIDER_AUTH_MODES | {"mcp_keyless"}, default="auto")
    if auth_mode == "api_key" and not api_key:
        return render_tool_error(
            tool_name="exa_search",
            error_class="provider_not_configured",
            message="Exa API key is not configured.",
            provider="exa",
            retryable=False,
            actionable_hint="Set auth_mode=auto/keyless for no-key Exa MCP search, or configure an Exa API key.",
        )
    max_results = min(_safe_int(arguments.get("max_results", config.get("max_results", 5)), 5), 20)
    search_type = _optional_enum(arguments.get("search_type"), _EXA_SEARCH_TYPES, default="auto")
    category = _optional_enum(arguments.get("category"), _EXA_CATEGORIES)
    include_domains = _string_list(arguments.get("include_domains"))
    exclude_domains = _string_list(arguments.get("exclude_domains"))
    start_published_date = _optional_string(arguments.get("start_published_date"))
    end_published_date = _optional_string(arguments.get("end_published_date"))
    if not api_key or auth_mode == "keyless" or auth_mode == "mcp_keyless":
        return await _search_exa_mcp(
            query,
            max_results,
            search_type=search_type,
            category=category,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            start_published_date=start_published_date,
            end_published_date=end_published_date,
            api_key=None,
            mcp_url=_optional_string(config.get("mcp_url")) or _EXA_MCP_URL,
        )
    return await _search_exa(
        query,
        api_key,
        max_results,
        search_type=search_type,
        category=category,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        start_published_date=start_published_date,
        end_published_date=end_published_date,
    )


async def _tavily_search(arguments: dict) -> str:
    query = arguments.get("query", "")
    if not query:
        return _invalid_argument_error(
            "tavily_search",
            "tavily_search requires a non-empty query.",
            provider="tavily",
            hint="Pass concise search keywords. If you only need basic lookup, use web_search.",
        )
    if _looks_like_url(query):
        return _invalid_argument_error(
            "tavily_search",
            "tavily_search expects search keywords, not a URL.",
            provider="tavily",
            hint="Use web_fetch when you already have a specific URL.",
        )

    config = await _get_tool_config("tavily_search")
    api_key = config.get("api_key") or config.get("tavily_api_key") or await _get_tavily_api_key()
    auth_mode = _optional_enum(config.get("auth_mode"), _WEB_PROVIDER_AUTH_MODES, default="auto")
    if auth_mode == "api_key" and not api_key:
        return render_tool_error(
            tool_name="tavily_search",
            error_class="provider_not_configured",
            message="Tavily API key is not configured.",
            provider="tavily",
            retryable=False,
            actionable_hint="Set auth_mode=auto/keyless for no-key Tavily search, or configure a Tavily API key.",
        )
    max_results = min(_safe_int(arguments.get("max_results", config.get("max_results", 5)), 5), 20)
    search_depth = _optional_enum(arguments.get("search_depth"), _TAVILY_SEARCH_DEPTHS, default="basic")
    topic = _optional_enum(arguments.get("topic"), _TAVILY_TOPICS, default="general")
    time_range = _optional_enum(arguments.get("time_range"), _TAVILY_TIME_RANGES)
    start_date = _optional_string(arguments.get("start_date"))
    end_date = _optional_string(arguments.get("end_date"))
    include_answer = arguments.get("include_answer")
    include_raw_content = arguments.get("include_raw_content")
    include_domains = _string_list(arguments.get("include_domains"))
    exclude_domains = _string_list(arguments.get("exclude_domains"))
    return await _search_tavily(
        query,
        api_key,
        max_results,
        search_depth=search_depth,
        topic=topic,
        time_range=time_range,
        start_date=start_date,
        end_date=end_date,
        include_answer=include_answer,
        include_raw_content=include_raw_content,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        keyless=not api_key or auth_mode == "keyless",
    )


async def _advanced_web_search(arguments: dict) -> str:
    query = _optional_string(arguments.get("query"))
    if not query:
        return _invalid_argument_error(
            "advanced_web_search",
            "advanced_web_search requires a non-empty query.",
            provider="advanced_web_search",
            hint="Pass concise search keywords. If you already have a URL, use advanced_web_fetch.",
        )
    if _looks_like_url(query):
        return _invalid_argument_error(
            "advanced_web_search",
            "advanced_web_search expects search keywords, not a URL.",
            provider="advanced_web_search",
            hint="Use advanced_web_fetch when you already have a specific URL.",
        )

    intent = _optional_enum(arguments.get("intent"), _ADVANCED_WEB_SEARCH_INTENTS, default="auto") or "auto"
    provider = _optional_enum(arguments.get("provider"), _ADVANCED_WEB_SEARCH_PROVIDERS, default="auto") or "auto"
    max_results = max(1, min(_safe_int(arguments.get("max_results"), 5), 20))
    include_content = _optional_bool(arguments.get("include_content"), False)

    if provider == "anysearch" or intent == "vertical" or arguments.get("domain") or arguments.get("sub_domain"):
        payload: dict[str, object] = {"query": query, "max_results": min(max_results, 10)}
        for key in ("domain", "sub_domain"):
            value = _optional_string(arguments.get(key))
            if value:
                payload[key] = value
        sub_domain_params = arguments.get("sub_domain_params")
        if isinstance(sub_domain_params, dict) and sub_domain_params:
            payload["sub_domain_params"] = sub_domain_params
        return await _anysearch_search(payload)

    if provider == "firecrawl" or intent == "content" or include_content:
        return await _firecrawl_search(
            {
                "query": query,
                "max_results": max_results,
                "include_content": include_content or intent == "content",
            }
        )

    if provider == "tavily" or intent in {"current", "news", "finance"}:
        payload = {"query": query, "max_results": max_results}
        if intent in {"news", "current"}:
            payload["topic"] = "news"
        elif intent == "finance":
            payload["topic"] = "finance"
        return await _tavily_search(payload)

    payload = {"query": query, "max_results": max_results}
    if provider == "exa" or intent in {"semantic", "company", "research_paper", "auto"}:
        if intent == "company":
            payload["category"] = "company"
        elif intent == "research_paper":
            payload["category"] = "research paper"
        return await _exa_search(payload)

    return await _exa_search(payload)


async def _advanced_web_fetch(arguments: dict) -> str:
    url = _optional_string(arguments.get("url"))
    if not url:
        return _invalid_argument_error(
            "advanced_web_fetch",
            "advanced_web_fetch requires a URL.",
            provider="advanced_web_fetch",
            hint="Pass a fully-qualified URL or a domain-like URL such as example.com/path.",
        )
    normalized_url = _normalize_url(url)
    if not normalized_url:
        return _invalid_argument_error(
            "advanced_web_fetch",
            f"advanced_web_fetch received an invalid URL: {url}",
            provider="advanced_web_fetch",
            hint="Use a valid URL. If you only have keywords, use advanced_web_search first.",
        )

    provider = _optional_enum(arguments.get("provider"), _ADVANCED_WEB_FETCH_PROVIDERS, default="auto") or "auto"
    max_chars = min(_safe_int(arguments.get("max_chars"), 12000), 30000)
    prefer_rendered = _optional_bool(arguments.get("prefer_rendered"), False)
    skip_core = _optional_bool(arguments.get("skip_core"), False)

    async def _call_provider(name: str) -> str:
        if name == "web_fetch":
            return await _web_fetch(
                {"url": normalized_url, "max_chars": min(max_chars, 20000), _SKIP_CRAWLER_FALLBACK: True}
            )
        if name == "firecrawl":
            return await _firecrawl_fetch(
                {"url": normalized_url, "max_chars": max_chars, _SKIP_WEB_FETCH_FALLBACK: True}
            )
        if name == "tavily":
            return await _tavily_extract({"url": normalized_url, "max_chars": max_chars})
        if name == "exa":
            return await _exa_fetch({"url": normalized_url, "max_chars": max_chars})
        if name == "anysearch":
            return await _anysearch_extract({"url": normalized_url})
        if name == "xcrawl":
            return await _xcrawl_scrape(
                {"url": normalized_url, "max_chars": max_chars, _SKIP_FIRECRAWL_FALLBACK: True}
            )
        return render_tool_error(
            tool_name="advanced_web_fetch",
            error_class="bad_arguments",
            message=f"Unsupported advanced_web_fetch provider '{name}'.",
            provider="advanced_web_fetch",
            retryable=False,
            actionable_hint="Use auto, web_fetch, firecrawl, tavily, exa, anysearch, or xcrawl.",
        )

    if provider != "auto":
        if provider == "xcrawl" and not await _get_xcrawl_api_key():
            return render_tool_error(
                tool_name="advanced_web_fetch",
                error_class="provider_not_configured",
                message="XCrawl API key is not configured.",
                provider="xcrawl",
                retryable=False,
                actionable_hint="Configure XCrawl before selecting provider=xcrawl, or use the no-key default fetch route.",
            )
        return await _call_provider(provider)

    provider_order = ["firecrawl"] if prefer_rendered else []
    if not skip_core and "web_fetch" not in provider_order:
        provider_order.append("web_fetch")
    for candidate in ("firecrawl", "tavily", "exa", "anysearch"):
        if candidate not in provider_order:
            provider_order.append(candidate)
    if await _get_xcrawl_api_key():
        provider_order.append("xcrawl")

    failures: list[str] = []
    for candidate in provider_order:
        result = await _call_provider(candidate)
        if not _provider_result_failed(result):
            return result
        failures.append(f"{candidate}: {result.splitlines()[0][:160] if result else 'empty result'}")

    return render_tool_error(
        tool_name="advanced_web_fetch",
        error_class="provider_error",
        message=f"advanced_web_fetch could not read {normalized_url} with providers: {'; '.join(failures)[:500]}",
        provider="advanced_web_fetch",
        retryable=True,
        actionable_hint=(
            "Try provider=firecrawl for rendered pages, provider=tavily/exa/anysearch for alternate extractors, "
            "or configure XCrawl for hard JS/proxy/device cases."
        ),
    )


async def _try_crawler_fetch_fallback(normalized_url: str, max_chars: int) -> tuple[str, str] | None:
    firecrawl_result = await _firecrawl_fetch(
        {
            "url": normalized_url,
            "max_chars": max_chars,
            _SKIP_WEB_FETCH_FALLBACK: True,
        }
    )
    if not _provider_result_failed(firecrawl_result):
        return ("firecrawl_fetch", firecrawl_result)

    if await _get_xcrawl_api_key():
        xcrawl_result = await _xcrawl_scrape(
            {
                "url": normalized_url,
                "max_chars": max_chars,
                _SKIP_FIRECRAWL_FALLBACK: True,
            }
        )
        if not _provider_result_failed(xcrawl_result):
            return ("xcrawl_scrape", xcrawl_result)

    return None


async def _web_fetch_failure_result(
    arguments: dict,
    *,
    normalized_url: str,
    max_chars: int,
    error_class: str,
    message: str,
    http_status: int | None = None,
    retryable: bool = False,
    actionable_hint: str,
) -> str:
    if not arguments.get(_SKIP_CRAWLER_FALLBACK):
        fallback = await _try_crawler_fetch_fallback(normalized_url, max_chars)
        if fallback:
            fallback_tool, fallback_result = fallback
            return render_tool_fallback(
                tool_name="web_fetch",
                error_class=error_class,
                message=message,
                provider="web_fetch",
                http_status=http_status,
                retryable=retryable,
                actionable_hint=(
                    "web_fetch could not read this page directly, so it escalated to a configured "
                    "crawler-backed reader."
                ),
                fallback_tool=fallback_tool,
                fallback_result=fallback_result,
            )

    return render_tool_error(
        tool_name="web_fetch",
        error_class=error_class,
        message=message,
        provider="web_fetch",
        http_status=http_status,
        retryable=retryable,
        actionable_hint=actionable_hint,
    )


def _extract_pdf_text(data: bytes) -> str:
    """Extract real text from PDF bytes (RC2).

    Returns '' when the payload is not a parseable PDF so the caller can surface a clean
    error instead of returning httpx-decoded mojibake (the production incident: a fetched
    PDF arrived as a `%PDF-1.4` / `/FlateDecode` byte stream and poisoned the evidence ledger).
    """
    if not data:
        return ""
    try:
        from app.services.text_extractor import extract_text

        return (extract_text(data, "fetched.pdf") or "").strip()
    except Exception as e:
        logger.debug("PDF text extraction failed: %s", e)
        return ""


def _filename_for_fetched_content(normalized_url: str, content_type: str) -> str:
    parsed = urlparse(normalized_url)
    name = Path(parsed.path).name or "index"
    if not Path(name).suffix:
        if "pdf" in content_type:
            name = f"{name}.pdf"
        elif "html" in content_type:
            name = f"{name}.html"
        elif "json" in content_type:
            name = f"{name}.json"
        elif "xml" in content_type:
            name = f"{name}.xml"
        else:
            name = f"{name}.txt"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def _convert_fetched_content(
    *,
    data: bytes,
    filename: str,
    normalized_url: str,
    content_type: str,
) -> DocumentConversionResult:
    return DocumentConversionService().convert_bytes(
        data=data,
        filename=filename,
        workspace_root=_WEB_FETCH_CONVERSION_ROOT,
        source_uri=normalized_url,
        source_mime_type=content_type or None,
        mode="auto",
        force_refresh=True,
    )


def _render_web_fetch_conversion(normalized_url: str, result: DocumentConversionResult, max_chars: int) -> str:
    return f"📄 **Fetched content from: {normalized_url}**\n\n{render_conversion_preview(result, max_chars=max_chars)}"


async def _web_fetch(arguments: dict) -> str:
    url = arguments.get("url", "").strip()
    if not url:
        return _invalid_argument_error(
            "web_fetch",
            "web_fetch requires a URL.",
            provider="web_fetch",
            hint="Pass a fully-qualified URL or a domain-like URL such as example.com/path.",
        )

    normalized_url = _normalize_url(url)
    if not normalized_url:
        return _invalid_argument_error(
            "web_fetch",
            f"web_fetch received an invalid URL: {url}",
            provider="web_fetch",
            hint="Use a valid URL. If you only have keywords, use web_search first.",
        )

    if _is_feishu_open_api_url(normalized_url):
        return render_tool_error(
            tool_name="web_fetch",
            error_class="wrong_tool",
            message="web_fetch cannot call Feishu OpenAPI endpoints because it does not attach managed channel auth.",
            provider="web_fetch",
            retryable=False,
            actionable_hint=(
                "Use the Feishu tools instead: feishu_calendar_list/create/update/delete for calendar, "
                "feishu_doc_read for docs, feishu_sheet_read for sheets, or load the Feishu Integration skill."
            ),
        )

    max_chars = min(_safe_int(arguments.get("max_chars", 8000), 8000), 20000)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(normalized_url, headers={"User-Agent": "Hive WebFetch/1.0"})

        if resp.status_code >= 300:
            error_class, retryable = classify_http_status(resp.status_code)
            return await _web_fetch_failure_result(
                arguments,
                normalized_url=normalized_url,
                max_chars=max_chars,
                error_class=error_class,
                message=f"web_fetch failed with HTTP {resp.status_code}: {resp.text[:200]}",
                http_status=resp.status_code,
                retryable=retryable,
                actionable_hint="Retry with another URL or fall back to search if the page is blocked.",
            )

        content_type = (resp.headers.get("content-type", "") or "").lower()
        raw_bytes = resp.content or b""
        if "application/pdf" in content_type or raw_bytes[:5].startswith(b"%PDF"):
            try:
                converted = _convert_fetched_content(
                    data=raw_bytes,
                    filename=_filename_for_fetched_content(normalized_url, "application/pdf"),
                    normalized_url=normalized_url,
                    content_type=content_type or "application/pdf",
                )
            except Exception as e:
                logger.debug("PDF conversion failed for %s: %s", normalized_url, e)
                return await _web_fetch_failure_result(
                    arguments,
                    normalized_url=normalized_url,
                    max_chars=max_chars,
                    error_class="unreadable_pdf",
                    message=f"web_fetch could not extract text from the PDF at {normalized_url}",
                    retryable=True,
                    actionable_hint="The PDF may be scanned, corrupt, or image-only; try a crawler-backed reader or another source.",
                )
            text = converted.markdown.strip()
            if not text:
                return await _web_fetch_failure_result(
                    arguments,
                    normalized_url=normalized_url,
                    max_chars=max_chars,
                    error_class="unreadable_pdf",
                    message=f"web_fetch could not extract text from the PDF at {normalized_url}",
                    retryable=True,
                    actionable_hint="The PDF may be scanned or image-only; try a crawler-backed reader or another source.",
                )
            if len(text) > max_chars:
                converted = DocumentConversionResult(
                    markdown=text[:max_chars] + f"\n\n[... truncated at {max_chars} chars]",
                    plain_text=converted.plain_text,
                    source_path=converted.source_path,
                    source_uri=converted.source_uri,
                    source_sha256=converted.source_sha256,
                    source_mime_type=converted.source_mime_type,
                    engine=converted.engine,
                    used_ocr=converted.used_ocr,
                    used_vision=converted.used_vision,
                    page_count=converted.page_count,
                    artifact_markdown_path=converted.artifact_markdown_path,
                    artifact_metadata_path=converted.artifact_metadata_path,
                    warnings=converted.warnings,
                )
            return _render_web_fetch_conversion(normalized_url, converted, max_chars)
        else:
            text = resp.text.strip()
            if (
                "html" in content_type
                or text.lstrip().startswith("<!doctype html")
                or text.lstrip().startswith("<html")
            ):
                markup = text
                converted = _convert_fetched_content(
                    data=raw_bytes or markup.encode("utf-8"),
                    filename=_filename_for_fetched_content(normalized_url, content_type or "text/html"),
                    normalized_url=normalized_url,
                    content_type=content_type or "text/html",
                )
                text = converted.markdown.strip()
                if _looks_like_incomplete_rendered_page(markup, text):
                    return await _web_fetch_failure_result(
                        arguments,
                        normalized_url=normalized_url,
                        max_chars=max_chars,
                        error_class="incomplete_content",
                        message=f"web_fetch returned incomplete rendered content for {normalized_url}",
                        retryable=True,
                        actionable_hint="Try a crawler-backed reader for JS-rendered pages.",
                    )
                if not text:
                    return await _web_fetch_failure_result(
                        arguments,
                        normalized_url=normalized_url,
                        max_chars=max_chars,
                        error_class="empty_content",
                        message=f"web_fetch returned empty content for {normalized_url}",
                        http_status=None,
                        retryable=False,
                        actionable_hint="Try another URL or use search to find a cleaner source page.",
                    )
                return _render_web_fetch_conversion(normalized_url, converted, max_chars)
        if not text:
            return await _web_fetch_failure_result(
                arguments,
                normalized_url=normalized_url,
                max_chars=max_chars,
                error_class="empty_content",
                message=f"web_fetch returned empty content for {normalized_url}",
                http_status=None,
                retryable=False,
                actionable_hint="Try another URL or use search to find a cleaner source page.",
            )
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... truncated at {max_chars} chars]"
        return f"📄 **Fetched content from: {normalized_url}**\n\n{text}"
    except Exception as e:
        return await _web_fetch_failure_result(
            arguments,
            normalized_url=normalized_url,
            max_chars=max_chars,
            error_class="provider_error",
            message=f"web_fetch failed: {str(e)[:300]}",
            http_status=None,
            retryable=True,
            actionable_hint="Retry with another URL or use search to discover an alternate page.",
        )


async def _get_firecrawl_api_key() -> str:
    config = await _get_tool_config("firecrawl_fetch")
    return config.get("api_key") or get_settings().FIRECRAWL_API_KEY


async def _get_xcrawl_api_key() -> str:
    config = await _get_tool_config("xcrawl_scrape")
    return config.get("api_key") or get_settings().XCRAWL_API_KEY


async def _firecrawl_search(arguments: dict) -> str:
    query = _optional_string(arguments.get("query"))
    if not query:
        return _invalid_argument_error(
            "firecrawl_search",
            "firecrawl_search requires a non-empty query.",
            provider="firecrawl",
            hint="Pass concise search keywords. If you already have a URL, use firecrawl_fetch or advanced_web_fetch.",
        )
    if _looks_like_url(query):
        return _invalid_argument_error(
            "firecrawl_search",
            "firecrawl_search expects search keywords, not a URL.",
            provider="firecrawl",
            hint="Use firecrawl_fetch or advanced_web_fetch when you already have a specific URL.",
        )

    config = await _get_tool_config("firecrawl_search")
    api_key = config.get("api_key") or config.get("firecrawl_api_key") or await _get_firecrawl_api_key()
    auth_mode = _provider_auth_mode(config)
    if auth_mode == "api_key" and not api_key:
        return render_tool_error(
            tool_name="firecrawl_search",
            error_class="provider_not_configured",
            message="Firecrawl API key is not configured.",
            provider="firecrawl",
            retryable=False,
            actionable_hint="Set auth_mode=auto/keyless for no-key Firecrawl search, or configure a Firecrawl API key.",
        )

    max_results = max(1, min(_safe_int(arguments.get("max_results", config.get("max_results", 5)), 5), 20))
    include_content = _optional_bool(arguments.get("include_content"), False)
    request_payload: dict[str, object] = {"query": query, "limit": max_results}
    if include_content:
        request_payload["scrapeOptions"] = {"formats": ["markdown"], "onlyMainContent": True}
    sources = _string_list(arguments.get("sources"))
    if sources:
        request_payload["sources"] = sources

    try:
        headers = {"Content-Type": "application/json"}
        if api_key and auth_mode != "keyless":
            headers["Authorization"] = f"Bearer {api_key}"
        base_url = (_optional_string(config.get("base_url")) or _FIRECRAWL_API_BASE_URL).rstrip("/")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.post(f"{base_url}/v2/search", json=request_payload, headers=headers)

        data = (
            resp.json()
            if "json" in (resp.headers.get("content-type", "") or "").lower() or resp.text.strip().startswith("{")
            else {}
        )
        if resp.status_code != 200:
            return _http_error(
                "firecrawl_search",
                provider="firecrawl",
                status_code=resp.status_code,
                detail=str(data) or resp.text,
                hint="Retry later, narrow the query, or use another advanced search provider.",
            )

        payload = data.get("data", data) if isinstance(data, dict) else data
        raw_results = payload if isinstance(payload, list) else payload.get("results", []) if isinstance(payload, dict) else []
        results = []
        for item in raw_results[:max_results]:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("name") or ""
            url = item.get("url") or item.get("link") or ""
            text = (
                item.get("markdown")
                or item.get("content")
                or item.get("description")
                or item.get("snippet")
                or item.get("summary")
                or ""
            )
            if not (title or url or text):
                continue
            results.append(f"**{title}**\n{url}\n{str(text)[:500]}")
        if not results:
            return f'🔍 No Firecrawl search results found for "{query}"'
        return f'🔍 Firecrawl search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)
    except Exception as e:
        return render_tool_error(
            tool_name="firecrawl_search",
            error_class="provider_error",
            message=f"firecrawl_search failed: {str(e)[:300]}",
            provider="firecrawl",
            retryable=True,
            actionable_hint="Retry later or use another advanced search provider.",
        )


async def _tavily_extract(arguments: dict) -> str:
    url = _optional_string(arguments.get("url"))
    if not url:
        return _invalid_argument_error(
            "tavily_extract",
            "tavily_extract requires a URL.",
            provider="tavily",
            hint="Pass the URL returned by search when you need Tavily Extract content.",
        )
    normalized_url = _normalize_url(url)
    if not normalized_url:
        return _invalid_argument_error(
            "tavily_extract",
            f"tavily_extract received an invalid URL: {url}",
            provider="tavily",
            hint="Use a valid URL. If you only have keywords, use advanced_web_search first.",
        )

    config = await _get_tool_config("tavily_extract")
    api_key = config.get("api_key") or config.get("tavily_api_key") or await _get_tavily_api_key()
    auth_mode = _provider_auth_mode(config)
    if auth_mode == "api_key" and not api_key:
        return render_tool_error(
            tool_name="tavily_extract",
            error_class="provider_not_configured",
            message="Tavily API key is not configured.",
            provider="tavily",
            retryable=False,
            actionable_hint="Set auth_mode=auto/keyless for no-key Tavily Extract, or configure a Tavily API key.",
        )

    max_chars = min(_safe_int(arguments.get("max_chars"), 12000), 30000)
    extract_depth = _optional_enum(arguments.get("extract_depth"), _TAVILY_EXTRACT_DEPTHS, default="basic") or "basic"
    output_format = _optional_enum(arguments.get("format"), {"markdown", "text"}, default="markdown") or "markdown"
    payload: dict[str, object] = {
        "urls": [normalized_url],
        "extract_depth": extract_depth,
        "format": output_format,
    }
    include_images = arguments.get("include_images")
    if include_images is not None:
        payload["include_images"] = _optional_bool(include_images, False)

    try:
        headers = {"Content-Type": "application/json"}
        if api_key and auth_mode != "keyless":
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["X-Tavily-Access-Mode"] = "keyless"
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.post("https://api.tavily.com/extract", json=payload, headers=headers)
        data = resp.json()
        if resp.status_code != 200:
            detail = data.get("error") if isinstance(data, dict) else None
            return _http_error(
                "tavily_extract",
                provider="tavily",
                status_code=resp.status_code,
                detail=str(detail or data),
                hint="Retry later or use another advanced_web_fetch provider.",
            )
        results = data.get("results", []) if isinstance(data, dict) else []
        first = next((item for item in results if isinstance(item, dict)), {})
        text = (
            first.get("raw_content")
            or first.get("content")
            or first.get("markdown")
            or first.get("text")
            or ""
        )
        text = _truncate_result_text(text, max_chars)
        if not text:
            return render_tool_error(
                tool_name="tavily_extract",
                error_class="empty_content",
                message=f"tavily_extract returned empty content for {normalized_url}",
                provider="tavily",
                retryable=False,
                actionable_hint="Try web_fetch, Firecrawl, Exa Fetch, or AnySearch Extract.",
            )
        return f"📄 **Tavily extracted content from: {normalized_url}**\n\n{text}"
    except Exception as e:
        return render_tool_error(
            tool_name="tavily_extract",
            error_class="provider_error",
            message=f"tavily_extract failed: {str(e)[:300]}",
            provider="tavily",
            retryable=True,
            actionable_hint="Retry later or use another advanced_web_fetch provider.",
        )


async def _exa_fetch(arguments: dict) -> str:
    url = _optional_string(arguments.get("url"))
    if not url:
        return _invalid_argument_error(
            "exa_fetch",
            "exa_fetch requires a URL.",
            provider="exa",
            hint="Pass a URL returned by search when you need Exa Fetch content.",
        )
    normalized_url = _normalize_url(url)
    if not normalized_url:
        return _invalid_argument_error(
            "exa_fetch",
            f"exa_fetch received an invalid URL: {url}",
            provider="exa",
            hint="Use a valid URL. If you only have keywords, use advanced_web_search first.",
        )

    config = await _get_tool_config("exa_fetch")
    api_key = config.get("api_key") or config.get("exa_api_key") or await _get_exa_api_key()
    auth_mode = _provider_auth_mode(config)
    if auth_mode == "api_key" and not api_key:
        return render_tool_error(
            tool_name="exa_fetch",
            error_class="provider_not_configured",
            message="Exa API key is not configured.",
            provider="exa",
            retryable=False,
            actionable_hint="Set auth_mode=auto/keyless for no-key Exa MCP Fetch, or configure an Exa API key.",
        )

    from app.services.mcp_client import MCPClient

    mcp_url = _optional_string(config.get("mcp_url")) or _EXA_MCP_URL
    mcp_api_key = None if auth_mode == "keyless" or not api_key else str(api_key)
    try:
        client = MCPClient(mcp_url, api_key=mcp_api_key)
        result = await client.call_tool("web_fetch_exa", {"url": normalized_url})
        if _provider_result_failed(result):
            return result
        max_chars = min(_safe_int(arguments.get("max_chars"), 12000), 30000)
        return f"📄 **Exa MCP fetched content from: {normalized_url}**\n\n{_truncate_result_text(result, max_chars)}"
    except Exception as e:
        return render_tool_error(
            tool_name="exa_fetch",
            error_class="provider_error",
            message=f"exa_fetch failed: {str(e)[:300]}",
            provider="exa",
            retryable=True,
            actionable_hint="Retry later or use another advanced_web_fetch provider.",
        )


async def _firecrawl_fetch(arguments: dict) -> str:
    url = arguments.get("url", "").strip()
    if not url:
        return _invalid_argument_error(
            "firecrawl_fetch",
            "firecrawl_fetch requires a URL.",
            provider="firecrawl",
            hint="Pass a fully-qualified URL or a domain-like URL such as example.com/path.",
        )

    normalized_url = _normalize_url(url)
    if not normalized_url:
        return _invalid_argument_error(
            "firecrawl_fetch",
            f"firecrawl_fetch received an invalid URL: {url}",
            provider="firecrawl",
            hint="Use a valid URL. If you only have keywords, use web_search first.",
        )

    config = await _get_tool_config("firecrawl_fetch")
    api_key = config.get("api_key") or config.get("firecrawl_api_key") or await _get_firecrawl_api_key()
    auth_mode = _optional_enum(config.get("auth_mode"), _WEB_PROVIDER_AUTH_MODES, default="auto")
    if auth_mode == "api_key" and not api_key:
        return render_tool_error(
            tool_name="firecrawl_fetch",
            error_class="provider_not_configured",
            message="Firecrawl API key is not configured.",
            provider="firecrawl",
            retryable=False,
            actionable_hint="Set auth_mode=auto/keyless for no-key Firecrawl scrape, or configure a Firecrawl API key.",
        )

    max_chars = min(_safe_int(arguments.get("max_chars", 12000), 12000), 30000)
    request_payload: dict[str, object] = {
        "url": normalized_url,
        "formats": _enum_list(arguments.get("formats"), _FIRECRAWL_FORMATS, ["markdown"]),
        "onlyMainContent": _optional_bool(arguments.get("only_main_content"), True),
    }
    if "only_clean_content" in arguments:
        request_payload["onlyCleanContent"] = _optional_bool(arguments.get("only_clean_content"), False)
    if arguments.get("wait_for_ms") is not None:
        request_payload["waitFor"] = _safe_int(arguments.get("wait_for_ms"), 0)
    include_tags = _string_list(arguments.get("include_tags"))
    exclude_tags = _string_list(arguments.get("exclude_tags"))
    if include_tags:
        request_payload["includeTags"] = include_tags
    if exclude_tags:
        request_payload["excludeTags"] = exclude_tags

    try:
        headers = {"Content-Type": "application/json"}
        if api_key and auth_mode != "keyless":
            headers["Authorization"] = f"Bearer {api_key}"
        base_url = (_optional_string(config.get("base_url")) or _FIRECRAWL_API_BASE_URL).rstrip("/")
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            resp = await client.post(
                f"{base_url}/v2/scrape",
                json=request_payload,
                headers=headers,
            )

        data = (
            resp.json()
            if "json" in (resp.headers.get("content-type", "") or "").lower() or resp.text.strip().startswith("{")
            else {}
        )
        if resp.status_code != 200:
            if arguments.get(_SKIP_WEB_FETCH_FALLBACK):
                return _http_error(
                    "firecrawl_fetch",
                    provider="firecrawl",
                    status_code=resp.status_code,
                    detail=str(data) or resp.text,
                    hint="Retry later or use another crawler provider.",
                )
            fallback_result = await _web_fetch(
                {"url": normalized_url, "max_chars": max_chars, _SKIP_CRAWLER_FALLBACK: True}
            )
            return render_tool_fallback(
                tool_name="firecrawl_fetch",
                error_class=classify_http_status(resp.status_code)[0],
                message=f"firecrawl_fetch failed with HTTP {resp.status_code}: {str(data)[:200] or resp.text[:200]}",
                provider="firecrawl",
                http_status=resp.status_code,
                retryable=classify_http_status(resp.status_code)[1],
                actionable_hint="Firecrawl is unavailable or misconfigured, so the tool fell back to web_fetch.",
                fallback_tool="web_fetch",
                fallback_result=fallback_result,
            )

        payload = data.get("data", data)
        text = (
            payload.get("markdown")
            or payload.get("summary")
            or payload.get("content")
            or payload.get("text")
            or payload.get("html")
            or payload.get("rawHtml")
            or ""
        ).strip()
        if not text:
            if arguments.get(_SKIP_WEB_FETCH_FALLBACK):
                return render_tool_error(
                    tool_name="firecrawl_fetch",
                    error_class="empty_content",
                    message=f"firecrawl_fetch returned empty content for {normalized_url}",
                    provider="firecrawl",
                    retryable=False,
                    actionable_hint="Retry later or use another crawler provider.",
                )
            fallback_result = await _web_fetch(
                {"url": normalized_url, "max_chars": max_chars, _SKIP_CRAWLER_FALLBACK: True}
            )
            return render_tool_fallback(
                tool_name="firecrawl_fetch",
                error_class="empty_content",
                message=f"firecrawl_fetch returned empty content for {normalized_url}",
                provider="firecrawl",
                retryable=False,
                actionable_hint="Firecrawl returned no usable content, so the tool fell back to web_fetch.",
                fallback_tool="web_fetch",
                fallback_result=fallback_result,
            )
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... truncated at {max_chars} chars]"
        return f"📄 **Firecrawl content from: {normalized_url}**\n\n{text}"
    except Exception as e:
        if arguments.get(_SKIP_WEB_FETCH_FALLBACK):
            return render_tool_error(
                tool_name="firecrawl_fetch",
                error_class="provider_error",
                message=f"firecrawl_fetch failed: {str(e)[:300]}",
                provider="firecrawl",
                retryable=True,
                actionable_hint="Retry later or use another crawler provider.",
            )
        fallback_result = await _web_fetch(
            {"url": normalized_url, "max_chars": max_chars, _SKIP_CRAWLER_FALLBACK: True}
        )
        return render_tool_fallback(
            tool_name="firecrawl_fetch",
            error_class="provider_error",
            message=f"firecrawl_fetch failed: {str(e)[:300]}",
            provider="firecrawl",
            retryable=True,
            actionable_hint="Firecrawl failed unexpectedly, so the tool fell back to web_fetch.",
            fallback_tool="web_fetch",
            fallback_result=fallback_result,
        )


async def _xcrawl_scrape(arguments: dict) -> str:
    url = arguments.get("url", "").strip()
    if not url:
        return _invalid_argument_error(
            "xcrawl_scrape",
            "xcrawl_scrape requires a URL.",
            provider="xcrawl",
            hint="Pass a fully-qualified URL or a domain-like URL such as example.com/path.",
        )

    normalized_url = _normalize_url(url)
    if not normalized_url:
        return _invalid_argument_error(
            "xcrawl_scrape",
            f"xcrawl_scrape received an invalid URL: {url}",
            provider="xcrawl",
            hint="Use a valid URL. If you only have keywords, use web_search first.",
        )

    api_key = await _get_xcrawl_api_key()
    if not api_key:
        return render_tool_error(
            tool_name="xcrawl_scrape",
            error_class="provider_not_configured",
            message="XCrawl API key is not configured.",
            provider="xcrawl",
            retryable=False,
            actionable_hint="Configure XCrawl before using this tool, or fall back to firecrawl_fetch/web_fetch.",
        )

    max_chars = min(_safe_int(arguments.get("max_chars", 12000), 12000), 30000)
    request_options: dict[str, object] = {
        "only_main_content": _optional_bool(arguments.get("only_main_content"), True),
        "block_ads": _optional_bool(arguments.get("block_ads"), True),
    }
    device = _optional_enum(arguments.get("device"), _XCRAWL_DEVICES)
    locale = _optional_string(arguments.get("locale"))
    if device:
        request_options["device"] = device
    if locale:
        request_options["locale"] = locale

    js_render_options: dict[str, object] = {
        "enabled": _optional_bool(arguments.get("js_render"), True),
    }
    wait_until = _optional_enum(arguments.get("wait_until"), _XCRAWL_WAIT_UNTIL)
    if wait_until:
        js_render_options["wait_until"] = wait_until

    request_payload: dict[str, object] = {
        "url": normalized_url,
        "mode": "sync",
        "request": request_options,
        "js_render": js_render_options,
        "output": {
            "formats": _enum_list(
                arguments.get("output_formats", arguments.get("formats")), _XCRAWL_OUTPUT_FORMATS, ["markdown"]
            )
        },
    }
    proxy_location = _optional_string(arguments.get("proxy_location"))
    if proxy_location:
        request_payload["proxy"] = {"location": proxy_location.upper()}

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.post(
                "https://run.xcrawl.com/v1/scrape",
                json=request_payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

        data = (
            resp.json()
            if "json" in (resp.headers.get("content-type", "") or "").lower() or resp.text.strip().startswith("{")
            else {}
        )
        if resp.status_code != 200:
            if arguments.get(_SKIP_FIRECRAWL_FALLBACK):
                return _http_error(
                    "xcrawl_scrape",
                    provider="xcrawl",
                    status_code=resp.status_code,
                    detail=str(data) or resp.text,
                    hint="Retry later or use another source URL.",
                )
            fallback_result = await _firecrawl_fetch(
                {"url": normalized_url, "max_chars": max_chars, _SKIP_WEB_FETCH_FALLBACK: True}
            )
            return render_tool_fallback(
                tool_name="xcrawl_scrape",
                error_class=classify_http_status(resp.status_code)[0],
                message=f"xcrawl_scrape failed with HTTP {resp.status_code}: {str(data)[:200] or resp.text[:200]}",
                provider="xcrawl",
                http_status=resp.status_code,
                retryable=classify_http_status(resp.status_code)[1],
                actionable_hint="XCrawl is unavailable or misconfigured, so the tool fell back to firecrawl_fetch.",
                fallback_tool="firecrawl_fetch",
                fallback_result=fallback_result,
            )

        payload = data.get("data", data)
        text = (
            payload.get("markdown")
            or payload.get("summary")
            or payload.get("content")
            or payload.get("text")
            or payload.get("html")
            or payload.get("raw_html")
            or ""
        ).strip()
        if not text:
            if arguments.get(_SKIP_FIRECRAWL_FALLBACK):
                return render_tool_error(
                    tool_name="xcrawl_scrape",
                    error_class="empty_content",
                    message=f"xcrawl_scrape returned empty content for {normalized_url}",
                    provider="xcrawl",
                    retryable=False,
                    actionable_hint="Retry later or use another source URL.",
                )
            fallback_result = await _firecrawl_fetch(
                {"url": normalized_url, "max_chars": max_chars, _SKIP_WEB_FETCH_FALLBACK: True}
            )
            return render_tool_fallback(
                tool_name="xcrawl_scrape",
                error_class="empty_content",
                message=f"xcrawl_scrape returned empty content for {normalized_url}",
                provider="xcrawl",
                retryable=False,
                actionable_hint="XCrawl returned no usable content, so the tool fell back to firecrawl_fetch.",
                fallback_tool="firecrawl_fetch",
                fallback_result=fallback_result,
            )
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... truncated at {max_chars} chars]"
        return f"📄 **XCrawl content from: {normalized_url}**\n\n{text}"
    except Exception as e:
        if arguments.get(_SKIP_FIRECRAWL_FALLBACK):
            return render_tool_error(
                tool_name="xcrawl_scrape",
                error_class="provider_error",
                message=f"xcrawl_scrape failed: {str(e)[:300]}",
                provider="xcrawl",
                retryable=True,
                actionable_hint="Retry later or use another source URL.",
            )
        fallback_result = await _firecrawl_fetch(
            {"url": normalized_url, "max_chars": max_chars, _SKIP_WEB_FETCH_FALLBACK: True}
        )
        return render_tool_fallback(
            tool_name="xcrawl_scrape",
            error_class="provider_error",
            message=f"xcrawl_scrape failed: {str(e)[:300]}",
            provider="xcrawl",
            retryable=True,
            actionable_hint="XCrawl failed unexpectedly, so the tool fell back to firecrawl_fetch.",
            fallback_tool="firecrawl_fetch",
            fallback_result=fallback_result,
        )


async def _search_exa(
    query: str,
    api_key: str,
    max_results: int,
    *,
    search_type: str | None = "auto",
    category: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str | None = None,
    end_published_date: str | None = None,
) -> str:
    payload: dict[str, object] = {
        "query": query,
        "numResults": max_results,
        "type": search_type or "auto",
        "contents": {
            "text": {"maxCharacters": 800},
        },
    }
    if category:
        payload["category"] = category
    if include_domains:
        payload["includeDomains"] = include_domains
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains
    if start_published_date:
        payload["startPublishedDate"] = start_published_date
    if end_published_date:
        payload["endPublishedDate"] = end_published_date

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.exa.ai/search",
            json=payload,
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
        )
        data = resp.json()

    if resp.status_code != 200:
        detail = data.get("error") if isinstance(data, dict) else None
        return f"❌ Exa search failed: HTTP {resp.status_code}: {str(detail or data)[:200]}"
    if "results" not in data:
        return f"❌ Exa search failed: {data.get('error', str(data)[:200])}"

    results = []
    for item in data["results"][:max_results]:
        summary = item.get("text") or item.get("summary") or ""
        results.append(f"**{item.get('title', '')}**\n{item.get('url', '')}\n{summary[:200]}")
    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Exa search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_exa_mcp(
    query: str,
    max_results: int,
    *,
    search_type: str | None = "auto",
    category: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str | None = None,
    end_published_date: str | None = None,
    api_key: str | None = None,
    mcp_url: str = _EXA_MCP_URL,
) -> str:
    from app.services.mcp_client import MCPClient

    advanced = bool(
        (search_type and search_type != "auto")
        or category
        or include_domains
        or exclude_domains
        or start_published_date
        or end_published_date
    )
    tool_name = "web_search_advanced_exa" if advanced else "web_search_exa"
    tool_arguments: dict[str, object] = {
        "query": query,
        "numResults": max_results,
    }
    if advanced:
        tool_arguments["type"] = search_type or "auto"
        if category:
            tool_arguments["category"] = category
        if include_domains:
            tool_arguments["includeDomains"] = include_domains
        if exclude_domains:
            tool_arguments["excludeDomains"] = exclude_domains
        if start_published_date:
            tool_arguments["startPublishedDate"] = start_published_date
        if end_published_date:
            tool_arguments["endPublishedDate"] = end_published_date

    client = MCPClient(mcp_url, api_key=api_key)
    result = await client.call_tool(tool_name, tool_arguments)
    if _provider_result_failed(result):
        return result
    return f'🔍 Exa MCP search for "{query}" ({max_results} requested):\n\n{result}'


async def _search_tavily(
    query: str,
    api_key: str | None,
    max_results: int,
    *,
    search_depth: str | None = "basic",
    topic: str | None = "general",
    time_range: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_answer: object = None,
    include_raw_content: object = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    keyless: bool = False,
) -> str:
    import httpx

    payload: dict[str, object] = {
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth or "basic",
        "topic": topic or "general",
    }
    if time_range:
        payload["time_range"] = time_range
    if start_date:
        payload["start_date"] = start_date
    if end_date:
        payload["end_date"] = end_date
    if include_answer is not None:
        payload["include_answer"] = include_answer
    if include_raw_content is not None:
        payload["include_raw_content"] = include_raw_content
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    headers = {"Content-Type": "application/json"}
    if api_key and not keyless:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json=payload,
            headers=headers,
            timeout=15,
        )
        data = resp.json()

    if resp.status_code != 200:
        detail = data.get("error") if isinstance(data, dict) else None
        return f"❌ Tavily search failed: HTTP {resp.status_code}: {str(detail or data)[:200]}"
    if "results" not in data:
        return f"❌ Tavily search failed: {data.get('error', str(data)[:200])}"

    results = []
    answer = str(data.get("answer") or "").strip()
    if answer:
        results.append(f"**Tavily answer:**\n{answer[:1000]}")
    results.extend(
        f"**{r.get('title', '')}**\n{r.get('url', '')}\n{r.get('content', '')[:400]}"
        for r in data["results"][:max_results]
    )
    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Tavily search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_google(query: str, api_key: str, max_results: int, language: str) -> str:
    import httpx

    parts = api_key.split(":", 1)
    if len(parts) != 2:
        return "❌ Google search requires API key in format 'API_KEY:SEARCH_ENGINE_ID'"

    gapi_key, cx = parts
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": gapi_key, "cx": cx, "q": query, "num": max_results, "lr": f"lang_{language[:2]}"},
            timeout=10,
        )
        data = resp.json()

    if resp.status_code != 200:
        error = data.get("error") if isinstance(data, dict) else None
        detail = error.get("message") if isinstance(error, dict) else error
        return f"❌ Google search failed: HTTP {resp.status_code}: {str(detail or data)[:200]}"
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        detail = error.get("message") if isinstance(error, dict) else error
        return f"❌ Google search failed: {str(detail)[:200]}"
    results = [
        f"**{item.get('title', '')}**\n{item.get('link', '')}\n{item.get('snippet', '')}"
        for item in data.get("items", [])[:max_results]
    ]
    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Google search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_bing(query: str, api_key: str, max_results: int, language: str) -> str:
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params={"q": query, "count": max_results, "mkt": language},
            headers={"Ocp-Apim-Subscription-Key": api_key},
            timeout=10,
        )
        data = resp.json()

    if resp.status_code != 200:
        error = data.get("error") if isinstance(data, dict) else None
        detail = error.get("message") if isinstance(error, dict) else error
        return f"❌ Bing search failed: HTTP {resp.status_code}: {str(detail or data)[:200]}"
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        detail = error.get("message") if isinstance(error, dict) else error
        return f"❌ Bing search failed: {str(detail)[:200]}"
    results = [
        f"**{item.get('name', '')}**\n{item.get('url', '')}\n{item.get('snippet', '')}"
        for item in data.get("webPages", {}).get("value", [])[:max_results]
    ]
    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Bing search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _execute_mcp_tool(tool_name: str, arguments: dict, agent_id: "uuid.UUID | None" = None) -> str:
    try:
        from app.models.agent import Agent
        from app.models.tool import AgentTool, Tool
        from app.services.mcp_authz import (
            MCPAuthzError,
            assert_mcp_cloud_transport_allowed,
            assert_no_mcp_token_passthrough,
        )
        from app.services.mcp_client import MCPClient
        from app.services.mcp_naming import build_mcp_tool_name, is_mcp_tool_name
        from app.services.mcp_server_service import resolve_agent_mcp_tool_mode, resolve_mcp_oauth_bearer

        # RLS 阶段1: reads `agents`/`tools` (policy-bearing) then filters
        # global-vs-own-tenant candidates in Python. Scope to the agent's tenant
        # (audited single-row bypass to resolve it); under RLS the scoped read
        # still sees NULL-tenant globals + this tenant's rows — exactly the
        # candidate set the Python filter below expects.
        tid = await resolve_tenant_for_agent(agent_id)
        oauth_bearer: str | None = None
        async with tenant_scoped_session(tid) as db:
            result = await db.execute(select(Tool).where(Tool.name == tool_name, Tool.type == "mcp"))
            candidates = _result_scalars_or_one(result)
            if not candidates and is_mcp_tool_name(tool_name):
                # Canonical-name alias (Step 6): a mcp__server__tool call resolves
                # against a row whose stored name may still be legacy (pre-backfill)
                # by recomputing the canonical name from (mcp_server_name,
                # mcp_tool_name). Makes the canonical name a durable identity, so
                # canonical generation can deploy before the rename backfill runs.
                all_mcp = (await db.execute(select(Tool).where(Tool.type == "mcp"))).scalars().all()
                candidates = [
                    t for t in all_mcp if build_mcp_tool_name(t.mcp_server_name, t.mcp_tool_name) == tool_name
                ]
            agent = None
            if agent_id and any(getattr(t, "tenant_id", None) is not None for t in candidates):
                agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
                agent = agent_result.scalar_one_or_none()
            visible = [t for t in candidates if _tool_visible_to_agent_tenant(t, agent)]
            if agent is not None:
                agent_tenant_id = getattr(agent, "tenant_id", None)
                tool = next(
                    (
                        t
                        for t in visible
                        if getattr(t, "tenant_id", None) is not None
                        and agent_tenant_id is not None
                        and str(getattr(t, "tenant_id", None)) == str(agent_tenant_id)
                    ),
                    None,
                )
                if tool is None:
                    tool = next((t for t in visible if getattr(t, "tenant_id", None) is None), None)
            else:
                tool = next((t for t in visible if getattr(t, "tenant_id", None) is None), None)
            agent_config = {}
            if tool and agent_id:
                at_r = await db.execute(
                    select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id)
                )
                at = at_r.scalar_one_or_none()
                if at is not None and hasattr(at, "enabled") and not bool(at.enabled):
                    return f"❌ MCP tool {tool_name} denied by this agent's tool assignment"
                if at is None and not bool(getattr(tool, "is_default", False)):
                    return f"Unknown tool: {tool_name}"
                agent_config = (getattr(at, "config", None) or {}) if at else {}
                mode = await resolve_agent_mcp_tool_mode(db, agent_id, tool)
                if mode == "deny":
                    return f"❌ MCP tool {tool_name} denied by this agent's MCP server policy"
            # OAuth bearer (Step 7): server-side encrypted token, resolved + refreshed
            # here so the agent never sees it. Fail-closed when expired/unrefreshable.
            _server_url = getattr(tool, "mcp_server_url", None) if tool else None
            if _server_url:
                oauth_bearer, oauth_error = await resolve_mcp_oauth_bearer(db, tid, _server_url)
                if oauth_error:
                    return render_tool_error(
                        tool_name=tool_name,
                        error_class="auth_required",
                        message=oauth_error,
                        provider="mcp",
                        retryable=False,
                        actionable_hint="Re-authorize this MCP server via the OAuth flow (admin MCP controls).",
                    )

        if not tool:
            return f"Unknown tool: {tool_name}"
        if not bool(getattr(tool, "enabled", True)):
            return f"❌ MCP tool {tool_name} is disabled"
        if not tool.mcp_server_url:
            return f"❌ MCP tool {tool_name} has no server URL configured"

        merged_config = {**(getattr(tool, "config", None) or {}), **agent_config}
        try:
            assert_no_mcp_token_passthrough(merged_config)
        except MCPAuthzError as exc:
            return render_tool_error(
                tool_name=tool_name,
                error_class="authz_policy_violation",
                message=str(exc),
                provider="mcp",
                retryable=False,
                actionable_hint=(
                    "Do not pass user/OAuth tokens through MCP tool config. "
                    "Use a server-scoped credential or tenant-managed connector authorization."
                ),
            )
        try:
            assert_mcp_cloud_transport_allowed(
                server_url=tool.mcp_server_url,
                transport=merged_config.get("transport") if isinstance(merged_config, dict) else None,
            )
        except MCPAuthzError as exc:
            return render_tool_error(
                tool_name=tool_name,
                error_class="authz_policy_violation",
                message=str(exc),
                provider="mcp",
                retryable=False,
                actionable_hint=(
                    "Use HTTP/SSE MCP in cloud core. Route stdio, WebSocket, SDK, or local IPC MCP servers "
                    "through the Local Bridge / coding plugin."
                ),
            )
        # Server-resolved OAuth bearer overrides any config key (it is NOT agent
        # passthrough — assert_no_mcp_token_passthrough already validated the
        # agent-supplied config above; this is the tenant's stored token).
        if oauth_bearer:
            merged_config["api_key"] = oauth_bearer
        mcp_url = tool.mcp_server_url
        mcp_name = tool.mcp_tool_name or tool_name

        if ".run.tools" in mcp_url and merged_config:
            return await _execute_via_smithery_connect(mcp_url, mcp_name, arguments, merged_config, agent_id=agent_id)

        direct_api_key = merged_config.get("api_key")
        client = MCPClient(mcp_url, api_key=direct_api_key)
        return await client.call_tool(mcp_name, arguments)
    except Exception as e:
        return f"❌ MCP tool execution error: {str(e)[:200]}"


async def _execute_via_smithery_connect(
    mcp_url: str,
    tool_name: str,
    arguments: dict,
    config: dict,
    agent_id=None,
) -> str:
    import httpx
    import json as json_mod

    from app.services.resource_discovery import _get_smithery_api_key

    api_key = await _get_smithery_api_key(agent_id)
    if not api_key:
        return (
            "❌ Smithery API key not configured.\n\n"
            "请提供你的 Smithery API Key，你可以通过以下步骤获取：\n"
            "1. 注册/登录 https://smithery.ai\n"
            "2. 前往 https://smithery.ai/account/api-keys 创建 API Key\n"
            "3. 将 Key 提供给我，我会帮你配置"
        )

    namespace = config.pop("smithery_namespace", None)
    connection_id = config.pop("smithery_connection_id", None)

    if not namespace or not connection_id:
        try:
            from app.models.tool import Tool

            # RLS 阶段1 / Finding #1: GLOBAL Smithery connect config — pin
            # `tenant_id IS NULL` so a same-named tenant tool can't leak its
            # namespace/connection. Bare session fails closed to NULL-tenant
            # rows under RLS, matching this explicit global-config predicate.
            async with async_session() as db:
                r = await db.execute(select(Tool).where(Tool.name == "discover_resources", Tool.tenant_id.is_(None)))
                disc_tool = r.scalar_one_or_none()
                if disc_tool and disc_tool.config:
                    namespace = namespace or disc_tool.config.get("smithery_namespace")
                    connection_id = connection_id or disc_tool.config.get("smithery_connection_id")
        except Exception as e:
            logger.debug("Suppressed: %s", e)

    if not namespace or not connection_id:
        return (
            "❌ Smithery Connect namespace/connection not configured. "
            "Please set smithery_namespace and smithery_connection_id in the tool configuration."
        )

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            tool_resp = await client.post(
                f"https://api.smithery.ai/connect/{namespace}/{connection_id}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
                headers=headers,
            )

            if tool_resp.status_code in (401, 403, 404):
                recovery_result = await _smithery_auto_recover(api_key, mcp_url, namespace, connection_id, agent_id)
                if recovery_result:
                    return recovery_result

            raw = tool_resp.text
            data = None
            for line in raw.split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    try:
                        data = json_mod.loads(line[6:])
                        break
                    except json_mod.JSONDecodeError:
                        logger.debug("[web_mcp] Failed to parse SSE data line as JSON")

            if data is None:
                try:
                    data = json_mod.loads(raw)
                except json_mod.JSONDecodeError:
                    return render_tool_error(
                        tool_name=tool_name,
                        error_class="provider_bad_response",
                        message=f"Smithery returned an unexpected response for {tool_name}: {raw[:300]}",
                        provider="smithery",
                        retryable=False,
                        actionable_hint="Retry later or re-authorize the MCP server connection.",
                    )

            if "error" in data:
                err = data["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                auth_keywords = ["auth", "unauthorized", "forbidden", "expired", "not found", "connection"]
                if any(kw in msg.lower() for kw in auth_keywords):
                    recovery_result = await _smithery_auto_recover(api_key, mcp_url, namespace, connection_id, agent_id)
                    if recovery_result:
                        return recovery_result
                return render_tool_error(
                    tool_name=tool_name,
                    error_class="provider_error",
                    message=f"MCP tool error: {msg[:300]}",
                    provider="smithery",
                    retryable=False,
                    actionable_hint="Retry after checking MCP authorization and server health.",
                )

            result = data.get("result", {})
            if isinstance(result, str):
                return result

            content_blocks = result.get("content", []) if isinstance(result, dict) else []
            texts = []
            for block in content_blocks:
                if isinstance(block, str):
                    texts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("type") == "image":
                        texts.append(f"[Image: {block.get('mimeType', 'image')}]")
                    else:
                        texts.append(str(block))
                else:
                    texts.append(str(block))
            return "\n".join(texts) if texts else str(result)
    except Exception as e:
        return render_tool_error(
            tool_name=tool_name,
            error_class="provider_error",
            message=f"Smithery Connect failed for {tool_name}: {str(e)[:200]}",
            provider="smithery",
            retryable=True,
            actionable_hint="Retry later or re-authorize the Smithery/MCP connection.",
        )


async def _smithery_auto_recover(
    api_key: str, mcp_url: str, namespace: str, connection_id: str, agent_id=None
) -> str | None:
    try:
        from app.models.tool import AgentTool, Tool
        from app.services.resource_discovery import _ensure_smithery_connection

        display_name = connection_id.replace("-", " ").title() if connection_id else "MCP Server"
        conn_result = await _ensure_smithery_connection(api_key, mcp_url, display_name)
        if "error" in conn_result:
            return (
                f"❌ MCP tool connection expired and auto-recovery failed: {conn_result['error']}\n\n"
                '💡 Please re-authorize by telling me: `import_mcp_server(server_id="...", reauthorize=true)`'
            )

        new_config = {
            "smithery_namespace": conn_result["namespace"],
            "smithery_connection_id": conn_result["connection_id"],
        }
        if agent_id:
            try:
                # RLS 阶段1: reads the policy-bearing `tools` rows by URL and
                # rewrites this agent's AgentTool config — scope to the agent's
                # tenant (audited single-row bypass to resolve it).
                tid = await resolve_tenant_for_agent(agent_id)
                async with tenant_scoped_session(tid) as db:
                    r = await db.execute(select(Tool).where(Tool.mcp_server_url == mcp_url, Tool.type == "mcp"))
                    for tool in r.scalars().all():
                        at_r = await db.execute(
                            select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id)
                        )
                        at = at_r.scalar_one_or_none()
                        if at:
                            at.config = {**(at.config or {}), **new_config}
                    await db.commit()
            except Exception as e:
                logger.debug("Suppressed: %s", e)

        if conn_result.get("auth_url"):
            return (
                "🔐 MCP tool connection expired. Re-authorization needed.\n\n"
                "Please visit the following URL to re-authorize:\n"
                f"{conn_result['auth_url']}\n\n"
                "After completing authorization, the tools will work again automatically."
            )
        return None
    except Exception as e:
        return f"❌ Auto-recovery failed: {str(e)[:200]}"


async def _discover_resources(arguments: dict) -> str:
    query = arguments.get("query", "")
    if not query:
        return "❌ Please provide a search query describing the capability you need."
    max_results = min(_safe_int(arguments.get("max_results", 5), 5), 10)

    from app.services.resource_discovery import search_smithery

    return await search_smithery(query, max_results)


async def _import_mcp_server(agent_id: uuid.UUID, arguments: dict) -> str:
    config = arguments.get("config") or {}
    reauthorize = arguments.get("reauthorize", False)
    mcp_url = config.pop("mcp_url", None) if isinstance(config, dict) else None

    if mcp_url:
        server_name = arguments.get("server_id") or config.pop("server_name", None)
        return await import_mcp_for_agent_and_register(
            agent_id,
            mcp_url=mcp_url,
            server_name=server_name,
            config=config,
            reauthorize=reauthorize,
        )

    server_id = arguments.get("server_id", "")
    if not server_id:
        return "❌ Please provide a server_id (e.g. 'github'). Use discover_resources first to find available servers."

    return await import_mcp_for_agent_and_register(
        agent_id,
        server_id=server_id,
        config=config or None,
        reauthorize=reauthorize,
    )


async def import_mcp_for_agent_and_register(
    agent_id: uuid.UUID,
    *,
    server_id: str | None = None,
    mcp_url: str | None = None,
    server_name: str | None = None,
    config: dict | None = None,
    reauthorize: bool = False,
) -> str:
    from app.services.mcp_server_service import import_mcp_for_agent_and_register as _service_import

    return await _service_import(
        agent_id,
        server_id=server_id,
        mcp_url=mcp_url,
        server_name=server_name,
        config=config,
        reauthorize=reauthorize,
    )
