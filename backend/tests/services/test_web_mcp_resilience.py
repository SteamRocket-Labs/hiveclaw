from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest


def _extract_tool_error_payload(result: str) -> dict:
    marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(marker) + len(marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        json_data: dict | None = None,
        headers: dict | None = None,
        content: bytes = b"",
    ):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data or {}
        self.headers = headers or {}
        self.content = content

    def json(self) -> dict:
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, *args, **kwargs):
        return self._response

    async def post(self, *args, **kwargs):
        return self._response


class _SequencedAsyncClient:
    def __init__(self, responses: list[_FakeResponse], calls: list[tuple[str, str]]):
        self._responses = responses
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def _next_response(self) -> _FakeResponse:
        if not self._responses:
            raise AssertionError("No fake response left for httpx call")
        return self._responses.pop(0)

    async def get(self, url: str, *args, **kwargs):
        self._calls.append(("get", url))
        return self._next_response()

    async def post(self, url: str, *args, **kwargs):
        self._calls.append(("post", url))
        return self._next_response()


class _CapturingAsyncClient:
    def __init__(self, response: _FakeResponse, requests: list[dict]):
        self._response = response
        self._requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, *args, **kwargs):
        self._requests.append({"method": "GET", "url": url, **kwargs})
        return self._response

    async def post(self, url: str, *args, **kwargs):
        self._requests.append({"method": "POST", "url": url, **kwargs})
        return self._response


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        return _ScalarResult(self._value)


@pytest.mark.asyncio
async def test_firecrawl_fetch_falls_back_to_web_fetch_on_billing_error(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_firecrawl_api_key() -> str:
        return "fc-key"

    monkeypatch.setattr(web_mcp, "_get_firecrawl_api_key", fake_get_firecrawl_api_key)

    async def fake_web_fetch(arguments: dict) -> str:
        assert arguments["url"] == "https://example.com/article"
        return "fallback fetched results"

    monkeypatch.setattr(web_mcp, "_web_fetch", fake_web_fetch)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            _FakeResponse(status_code=402, text="Payment Required", headers={"content-type": "application/json"}),
        ),
    )

    result = await web_mcp._firecrawl_fetch({"url": "https://example.com/article"})

    assert "fallback fetched results" in result
    payload = _extract_tool_error_payload(result)
    assert payload["error_class"] == "quota_or_billing"
    assert payload["http_status"] == 402
    assert payload["provider"] == "firecrawl"


@pytest.mark.asyncio
async def test_xcrawl_scrape_rejects_non_url_input():
    from app.services.agent_tool_domains import web_mcp

    result = await web_mcp._xcrawl_scrape({"url": "not a valid url"})

    payload = _extract_tool_error_payload(result)
    assert payload["error_class"] == "bad_arguments"
    assert payload["provider"] == "xcrawl"


@pytest.mark.asyncio
async def test_web_fetch_extracts_html_content(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    html = "<html><head><title>Demo</title></head><body><main><h1>Hello</h1><p>World</p></main></body></html>"
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            _FakeResponse(status_code=200, text=html, headers={"content-type": "text/html"}),
        ),
    )

    result = await web_mcp._web_fetch({"url": "https://example.com", "max_chars": 1000})

    assert "Hello" in result
    assert "World" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_web_fetch_uses_document_conversion_for_html_markdown_artifact(monkeypatch, tmp_path):
    from app.services.agent_tool_domains import web_mcp

    conversions: list[dict[str, object]] = []

    def fake_extract(filecontent: str, **kwargs):
        raise AssertionError("web_fetch HTML success path must use DocumentConversionService, not trafilatura directly")

    class _FakeDocumentConversionService:
        def convert_bytes(self, **kwargs):
            conversions.append(kwargs)
            return SimpleNamespace(
                markdown="# Quarterly update\n\nRevenue grew 42 percent.",
                artifact_markdown_path=".hive/document_conversions/html/content.md",
                artifact_metadata_path=".hive/document_conversions/html/metadata.json",
                engine="local_markitdown",
                warnings=(),
            )

    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace(extract=fake_extract))
    monkeypatch.setattr(web_mcp, "DocumentConversionService", _FakeDocumentConversionService, raising=False)
    monkeypatch.setattr(web_mcp, "_WEB_FETCH_CONVERSION_ROOT", tmp_path)
    html = """
    <html>
      <body>
        <nav>cookie banner and navigation noise</nav>
        <main><article><h1>Quarterly update</h1><p>Revenue grew 42 percent.</p></article></main>
      </body>
    </html>
    """
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            _FakeResponse(status_code=200, text=html, headers={"content-type": "text/html"}),
        ),
    )

    result = await web_mcp._web_fetch({"url": "https://example.com/article", "max_chars": 1000})

    assert "Converted with local_markitdown." in result
    assert "Full Markdown: .hive/document_conversions/html/content.md" in result
    assert "# Quarterly update" in result
    assert conversions
    assert conversions[0]["data"] == html.strip().encode()
    assert conversions[0]["source_uri"] == "https://example.com/article"
    assert conversions[0]["source_mime_type"] == "text/html"
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_web_fetch_escalates_to_firecrawl_on_http_error(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_firecrawl_api_key() -> str:
        return "fc-key"

    async def fake_get_xcrawl_api_key() -> str:
        return ""

    calls: list[tuple[str, str]] = []
    responses = [
        _FakeResponse(status_code=403, text="Forbidden", headers={"content-type": "text/plain"}),
        _FakeResponse(
            status_code=200,
            text='{"success": true, "data": {"markdown": "# Rendered\\n\\nBody"}}',
            json_data={"success": True, "data": {"markdown": "# Rendered\n\nBody"}},
            headers={"content-type": "application/json"},
        ),
    ]

    monkeypatch.setattr(web_mcp, "_get_firecrawl_api_key", fake_get_firecrawl_api_key)
    monkeypatch.setattr(web_mcp, "_get_xcrawl_api_key", fake_get_xcrawl_api_key)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: _SequencedAsyncClient(responses, calls))

    result = await web_mcp._web_fetch({"url": "https://example.com/app", "max_chars": 1000})

    assert "Fallback tool used: `firecrawl_fetch`" in result
    assert "Firecrawl content from: https://example.com/app" in result
    assert "Rendered" in result
    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "web_fetch"
    assert payload["fallback_tool"] == "firecrawl_fetch"
    assert payload["http_status"] == 403
    assert calls == [
        ("get", "https://example.com/app"),
        ("post", "https://api.firecrawl.dev/v2/scrape"),
    ]


@pytest.mark.asyncio
async def test_web_fetch_escalates_to_xcrawl_when_firecrawl_fails(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_firecrawl_api_key() -> str:
        return "fc-key"

    async def fake_get_xcrawl_api_key() -> str:
        return "xcr-key"

    calls: list[tuple[str, str]] = []
    responses = [
        _FakeResponse(status_code=403, text="Forbidden", headers={"content-type": "text/plain"}),
        _FakeResponse(status_code=503, text="upstream down", headers={"content-type": "application/json"}),
        _FakeResponse(
            status_code=200,
            text='{"data": {"markdown": "# Xcrawl Rendered\\n\\nBody"}}',
            json_data={"data": {"markdown": "# Xcrawl Rendered\n\nBody"}},
            headers={"content-type": "application/json"},
        ),
    ]

    monkeypatch.setattr(web_mcp, "_get_firecrawl_api_key", fake_get_firecrawl_api_key)
    monkeypatch.setattr(web_mcp, "_get_xcrawl_api_key", fake_get_xcrawl_api_key)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: _SequencedAsyncClient(responses, calls))

    result = await web_mcp._web_fetch({"url": "https://example.com/app", "max_chars": 1000})

    assert "Fallback tool used: `xcrawl_scrape`" in result
    assert "XCrawl content from: https://example.com/app" in result
    assert "Xcrawl Rendered" in result
    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "web_fetch"
    assert payload["fallback_tool"] == "xcrawl_scrape"
    assert payload["http_status"] == 403
    assert calls == [
        ("get", "https://example.com/app"),
        ("post", "https://api.firecrawl.dev/v2/scrape"),
        ("post", "https://run.xcrawl.com/v1/scrape"),
    ]


@pytest.mark.asyncio
async def test_web_fetch_rejects_feishu_open_api_urls_before_http(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    class FailingClient:
        async def __aenter__(self):
            raise AssertionError("web_fetch should not call Feishu OpenAPI directly")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: FailingClient())

    result = await web_mcp._web_fetch(
        {
            "url": "https://open.feishu.cn/open-apis/calendar/v4/calendars?page_size=100",
        }
    )

    payload = _extract_tool_error_payload(result)
    assert payload["error_class"] == "wrong_tool"
    assert "feishu_calendar_list" in payload["actionable_hint"]


@pytest.mark.asyncio
async def test_web_search_anysearch_key_present_prefers_anysearch_over_searxng(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(
        config={
            "search_engine": "auto",
            "anysearch_api_keys": ["any-key"],
            "searxng_url": "https://search.example.com",
            "max_results": 5,
            "language": "en",
        }
    )
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_anysearch(query: str, config: dict, max_results: int, language: str) -> str:
        assert query == "openai sdk"
        assert config["anysearch_api_keys"] == ["any-key"]
        assert max_results == 5
        assert language == "en"
        return "anysearch primary results"

    async def fake_searxng(query: str, searxng_url: str, max_results: int, language: str) -> str:
        raise AssertionError("SearXNG must not run before configured AnySearch keys")

    monkeypatch.setattr(web_mcp, "_search_anysearch", fake_anysearch)
    monkeypatch.setattr(web_mcp, "_search_searxng", fake_searxng)

    result = await web_mcp._web_search({"query": "openai sdk"})

    assert result == "anysearch primary results"
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_web_search_no_anysearch_key_uses_searxng_fallback(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(
        config={
            "search_engine": "auto",
            "anysearch_api_keys": [],
            "searxng_url": "https://search.example.com",
            "max_results": 5,
            "language": "en",
        }
    )
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_anysearch(query: str, config: dict, max_results: int, language: str) -> str:
        raise AssertionError("AnySearch must not run without configured API keys")

    async def fake_searxng(query: str, searxng_url: str, max_results: int, language: str) -> str:
        assert query == "openai sdk"
        assert searxng_url == "https://search.example.com"
        assert max_results == 5
        assert language == "en"
        return "searxng fallback results"

    monkeypatch.setattr(web_mcp, "_search_anysearch", fake_anysearch)
    monkeypatch.setattr(web_mcp, "_search_searxng", fake_searxng)

    result = await web_mcp._web_search({"query": "openai sdk"})

    assert result == "searxng fallback results"
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_web_search_anysearch_failure_falls_back_to_searxng_not_duckduckgo(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(
        config={
            "search_engine": "auto",
            "anysearch_api_keys": ["any-key"],
            "searxng_url": "https://search.example.com",
            "max_results": 5,
            "language": "en",
        }
    )
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_anysearch(query: str, config: dict, max_results: int, language: str) -> str:
        assert query == "openai sdk"
        return "❌ AnySearch search failed: HTTP 429: quota exhausted"

    async def fake_searxng(query: str, searxng_url: str, max_results: int, language: str) -> str:
        assert query == "openai sdk"
        assert searxng_url == "https://search.example.com"
        return "searxng fallback results"

    async def fake_duckduckgo(query: str, max_results: int) -> str:
        raise AssertionError("Direct DuckDuckGo must not be the auto fallback")

    monkeypatch.setattr(web_mcp, "_search_anysearch", fake_anysearch)
    monkeypatch.setattr(web_mcp, "_search_searxng", fake_searxng)
    monkeypatch.setattr(web_mcp, "_search_duckduckgo", fake_duckduckgo)

    result = await web_mcp._web_search({"query": "openai sdk"})

    assert "searxng fallback results" in result
    payload = _extract_tool_error_payload(result)
    assert payload["provider"] == "anysearch"
    assert payload["fallback_tool"] == "web_search:searxng"


@pytest.mark.asyncio
async def test_web_search_auto_uses_basic_search_even_when_exa_key_is_available(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(
        config={
            "search_engine": "auto",
            "searxng_url": "https://search.example.com",
            "max_results": 5,
            "language": "en",
        }
    )
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_get_exa_api_key() -> str:
        return "exa-key"

    async def fake_exa(query: str, api_key: str, max_results: int) -> str:
        raise AssertionError("web_search must not auto-route to Exa; use exa_search via tool_search instead")

    async def fake_searxng(query: str, searxng_url: str, max_results: int, language: str) -> str:
        assert query == "python asyncio"
        assert searxng_url == "https://search.example.com"
        assert max_results == 5
        return "searxng basic results"

    monkeypatch.setattr(web_mcp, "_get_exa_api_key", fake_get_exa_api_key)
    monkeypatch.setattr(web_mcp, "_search_exa", fake_exa)
    monkeypatch.setattr(web_mcp, "_search_searxng", fake_searxng)

    result = await web_mcp._web_search({"query": "python asyncio", "max_results": 5})

    assert "searxng basic results" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_web_search_defaults_missing_language_to_english(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_tool_config(tool_name: str) -> dict:
        assert tool_name == "web_search"
        return {"search_engine": "searxng", "max_results": 5}

    async def fake_get_searxng_url() -> str:
        return "https://search.example.com"

    async def fake_searxng(query: str, searxng_url: str, max_results: int, language: str) -> str:
        assert query == "agent memory"
        assert searxng_url == "https://search.example.com"
        assert max_results == 5
        assert language == "en"
        return "searxng results"

    monkeypatch.setattr(web_mcp, "_get_tool_config", fake_get_tool_config)
    monkeypatch.setattr(web_mcp, "_get_searxng_url", fake_get_searxng_url)
    monkeypatch.setattr(web_mcp, "_search_searxng", fake_searxng)

    result = await web_mcp._web_search({"query": "agent memory"})

    assert result == "searxng results"


@pytest.mark.asyncio
async def test_searxng_search_uses_official_json_search_endpoint(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    requests: list[dict] = []
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                json_data={
                    "results": [
                        {
                            "title": "Agent memory",
                            "url": "https://example.com/memory",
                            "content": "SearXNG JSON result",
                        }
                    ]
                },
                headers={"content-type": "application/json"},
            ),
            requests,
        ),
    )

    result = await web_mcp._search_searxng("agent memory", "https://searx.example", 3, "en")

    assert "SearXNG results" in result
    assert "Agent memory" in result
    assert requests[0]["method"] == "GET"
    assert requests[0]["url"] == "https://searx.example/search"
    assert requests[0]["params"] == {
        "q": "agent memory",
        "format": "json",
        "language": "en",
        "categories": "general",
    }
    assert requests[0]["headers"]["User-Agent"] == "Hive WebSearch/1.0"


@pytest.mark.asyncio
async def test_anysearch_search_uses_official_v1_search_endpoint_and_parses_nested_shape(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    requests: list[dict] = []
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                json_data={
                    "code": 0,
                    "message": "success",
                    "data": {
                        "results": [
                            {
                                "title": "Quantum Computing Explained",
                                "url": "https://www.nist.gov/quantum",
                                "snippet": "NIST explains quantum computing.",
                                "content": "Longer content that should not be dumped in full.",
                            }
                        ],
                        "metadata": {"provider": "anysearch"},
                    },
                },
                headers={"content-type": "application/json"},
            ),
            requests,
        ),
    )

    result = await web_mcp._search_anysearch(
        "quantum computing",
        {
            "anysearch_api_keys": ["any-key"],
            "anysearch_zone": "intl",
            "anysearch_content_types": ["web"],
        },
        3,
        "en",
    )

    assert "AnySearch results" in result
    assert "Quantum Computing Explained" in result
    assert "https://www.nist.gov/quantum" in result
    assert "Longer content that should not be dumped in full" not in result
    assert requests[0]["method"] == "POST"
    assert requests[0]["url"] == "https://api.anysearch.com/v1/search"
    assert requests[0]["headers"]["Authorization"] == "Bearer any-key"
    assert requests[0]["headers"]["User-Agent"] == "Hive WebSearch/1.0"
    assert requests[0]["json"] == {
        "query": "quantum computing",
        "max_results": 3,
        "zone": "intl",
        "language": "en",
        "content_types": ["web"],
    }


@pytest.mark.asyncio
async def test_anysearch_search_accepts_top_level_results_shape_and_omits_auth_without_key(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    requests: list[dict] = []
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                json_data={
                    "results": [
                        {
                            "title": "Market data",
                            "url": "https://example.com/markets",
                            "snippet": "Market data summary.",
                        }
                    ],
                    "metadata": {},
                },
                headers={"content-type": "application/json"},
            ),
            requests,
        ),
    )

    result = await web_mcp._search_anysearch(
        "market data",
        {"anysearch_api_keys": [], "anysearch_zone": "intl", "anysearch_content_types": ["web"]},
        1,
        "en",
    )

    assert "AnySearch results" in result
    assert "Market data" in result
    assert "Authorization" not in requests[0]["headers"]


@pytest.mark.asyncio
async def test_anysearch_search_tries_next_key_after_quota_error(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    requests: list[dict] = []
    responses = [
        _FakeResponse(status_code=402, text="quota exhausted", json_data={"error": "quota_exhausted"}),
        _FakeResponse(
            status_code=200,
            json_data={
                "code": 0,
                "message": "success",
                "data": {
                    "results": [
                        {
                            "title": "Second key result",
                            "url": "https://example.com/second",
                            "snippet": "Recovered with second key.",
                        }
                    ],
                    "metadata": {},
                },
            },
        ),
    ]

    class SequencedCapturingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, *args, **kwargs):
            requests.append({"method": "POST", "url": url, **kwargs})
            if not responses:
                raise AssertionError("No fake response left for AnySearch call")
            return responses.pop(0)

    async def fake_key_start_index(keys: list[str], scope: str) -> int:
        assert keys == ["key-a", "key-b"]
        assert scope == "global"
        return 0

    monkeypatch.setattr(web_mcp, "_next_anysearch_key_start_index", fake_key_start_index)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: SequencedCapturingClient())

    result = await web_mcp._search_anysearch(
        "market data",
        {"anysearch_api_keys": "key-a\nkey-b", "anysearch_zone": "intl", "anysearch_content_types": ["web"]},
        2,
        "en",
    )

    assert "AnySearch results" in result
    assert "Second key result" in result
    assert [request["headers"]["Authorization"] for request in requests] == ["Bearer key-a", "Bearer key-b"]


@pytest.mark.asyncio
async def test_anysearch_mcp_get_sub_domains_calls_official_json_rpc_endpoint(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_tool_config(tool_name: str) -> dict:
        assert tool_name == "web_search"
        return {"anysearch_api_keys": ["any-key"], "anysearch_timeout_seconds": 9}

    requests: list[dict] = []
    monkeypatch.setattr(web_mcp, "_get_tool_config", fake_get_tool_config)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                json_data={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "## finance Domain Capabilities\n\n### finance.quote\nQuote data.",
                            }
                        ]
                    },
                },
            ),
            requests,
        ),
    )

    result = await web_mcp._anysearch_get_sub_domains({"domain": "finance"})

    assert "finance Domain Capabilities" in result
    assert requests[0]["method"] == "POST"
    assert requests[0]["url"] == "https://api.anysearch.com/mcp"
    assert requests[0]["headers"]["Authorization"] == "Bearer any-key"
    assert requests[0]["headers"]["User-Agent"] == "Hive WebSearch/1.0"
    assert requests[0]["timeout"] == 9
    assert requests[0]["json"] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_sub_domains", "arguments": {"domain": "finance"}},
    }


@pytest.mark.asyncio
async def test_anysearch_mcp_search_forwards_vertical_domain_arguments(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_tool_config(tool_name: str) -> dict:
        assert tool_name == "web_search"
        return {"anysearch_api_keys": "any-key"}

    requests: list[dict] = []
    monkeypatch.setattr(web_mcp, "_get_tool_config", fake_get_tool_config)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                json_data={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "## Search Results\n\nAAPL quote data from AnySearch MCP.",
                            }
                        ]
                    },
                },
            ),
            requests,
        ),
    )

    result = await web_mcp._anysearch_search(
        {
            "query": "AAPL",
            "domain": "finance",
            "sub_domain": "finance.quote",
            "sub_domain_params": {"type": "stock", "symbol": "AAPL", "cn_code": ""},
            "max_results": 5,
        }
    )

    assert "AAPL quote data" in result
    assert requests[0]["json"]["params"] == {
        "name": "search",
        "arguments": {
            "query": "AAPL",
            "domain": "finance",
            "sub_domain": "finance.quote",
            "sub_domain_params": {"type": "stock", "symbol": "AAPL", "cn_code": ""},
            "max_results": 5,
        },
    }


@pytest.mark.asyncio
async def test_anysearch_mcp_batch_search_forwards_queries(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_tool_config(tool_name: str) -> dict:
        assert tool_name == "web_search"
        return {"anysearch_api_keys": ["any-key"]}

    requests: list[dict] = []
    monkeypatch.setattr(web_mcp, "_get_tool_config", fake_get_tool_config)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                json_data={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": "Merged AnySearch batch results"}]},
                },
            ),
            requests,
        ),
    )

    queries = [
        {"query": "AAPL", "domain": "finance", "sub_domain": "finance.quote", "sub_domain_params": "symbol=AAPL"},
        {"query": "OpenAI funding news"},
    ]

    result = await web_mcp._anysearch_batch_search({"queries": queries, "max_results": 3})

    assert "Merged AnySearch batch results" in result
    assert requests[0]["json"]["params"] == {
        "name": "batch_search",
        "arguments": {
            "queries": [
                {
                    "query": "AAPL",
                    "domain": "finance",
                    "sub_domain": "finance.quote",
                    "sub_domain_params": "symbol=AAPL",
                    "max_results": 3,
                },
                {"query": "OpenAI funding news", "max_results": 3},
            ]
        },
    }


@pytest.mark.asyncio
async def test_anysearch_mcp_extract_forwards_url(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_tool_config(tool_name: str) -> dict:
        assert tool_name == "web_search"
        return {"anysearch_api_keys": ["any-key"]}

    requests: list[dict] = []
    monkeypatch.setattr(web_mcp, "_get_tool_config", fake_get_tool_config)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                json_data={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": "# Extracted page\n\nArticle body."}]},
                },
            ),
            requests,
        ),
    )

    result = await web_mcp._anysearch_extract({"url": "https://example.com/article"})

    assert "Extracted page" in result
    assert requests[0]["json"]["params"] == {
        "name": "extract",
        "arguments": {"url": "https://example.com/article"},
    }


@pytest.mark.asyncio
async def test_anysearch_mcp_requires_configured_key_unless_anonymous_is_enabled(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_tool_config(tool_name: str) -> dict:
        assert tool_name == "web_search"
        return {"anysearch_api_keys": "", "anysearch_allow_anonymous": False}

    monkeypatch.setattr(web_mcp, "_get_tool_config", fake_get_tool_config)

    result = await web_mcp._anysearch_get_sub_domains({"domain": "finance"})

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "anysearch_get_sub_domains"
    assert payload["error_class"] == "provider_not_configured"
    assert payload["provider"] == "anysearch_mcp"


@pytest.mark.asyncio
async def test_anysearch_mcp_tries_next_key_after_rate_limit(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_tool_config(tool_name: str) -> dict:
        assert tool_name == "web_search"
        return {"anysearch_api_keys": "key-a,key-b"}

    async def fake_key_start_index(keys: list[str], scope: str) -> int:
        assert keys == ["key-a", "key-b"]
        assert scope == "global"
        return 0

    requests: list[dict] = []
    responses = [
        _FakeResponse(status_code=429, text="rate limited", json_data={"error": "rate_limited"}),
        _FakeResponse(
            status_code=200,
            json_data={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "Recovered with second key"}]},
            },
        ),
    ]

    class SequencedCapturingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, *args, **kwargs):
            requests.append({"method": "POST", "url": url, **kwargs})
            if not responses:
                raise AssertionError("No fake response left for AnySearch MCP call")
            return responses.pop(0)

    monkeypatch.setattr(web_mcp, "_get_tool_config", fake_get_tool_config)
    monkeypatch.setattr(web_mcp, "_next_anysearch_key_start_index", fake_key_start_index)
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: SequencedCapturingClient())

    result = await web_mcp._anysearch_get_sub_domains({"domain": "finance"})

    assert "Recovered with second key" in result
    assert [request["headers"]["Authorization"] for request in requests] == ["Bearer key-a", "Bearer key-b"]


@pytest.mark.asyncio
async def test_duckduckgo_search_reports_http_errors(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(_FakeResponse(status_code=403, text="Forbidden")),
    )

    result = await web_mcp._search_duckduckgo("blocked query", 5)

    assert result.startswith("❌ DuckDuckGo search failed: HTTP 403")
    assert "Forbidden" in result


@pytest.mark.asyncio
async def test_duckduckgo_search_parses_html_fallback_results(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    html_doc = """
    <html>
      <body>
        <div class="result">
          <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fa">A &amp; B</a>
          <a class="result__snippet">C &amp; D <b>snippet</b></a>
        </div>
      </body>
    </html>
    """
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(_FakeResponse(status_code=200, text=html_doc)),
    )

    result = await web_mcp._search_duckduckgo("html fallback", 5)

    assert "DuckDuckGo results" in result
    assert "**A & B**" in result
    assert "https://example.com/a" in result
    assert "C & D snippet" in result


@pytest.mark.asyncio
async def test_exa_search_uses_provider_config(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(config={"api_key": "exa-key", "max_results": 5})
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_exa(
        query: str,
        api_key: str,
        max_results: int,
        *,
        search_type: str | None,
        category: str | None,
        include_domains: list[str],
        exclude_domains: list[str],
        start_published_date: str | None,
        end_published_date: str | None,
    ) -> str:
        assert query == "semantic competitors"
        assert api_key == "exa-key"
        assert max_results == 5
        assert search_type == "auto"
        assert category is None
        assert include_domains == []
        assert exclude_domains == []
        assert start_published_date is None
        assert end_published_date is None
        return "exa advanced results"

    monkeypatch.setattr(web_mcp, "_search_exa", fake_exa)

    result = await web_mcp._exa_search({"query": "semantic competitors"})

    assert "exa advanced results" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_tavily_search_reports_missing_provider_key(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(config={"api_key": "", "max_results": 5})
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_get_tavily_api_key() -> str:
        return ""

    monkeypatch.setattr(web_mcp, "_get_tavily_api_key", fake_get_tavily_api_key)

    result = await web_mcp._tavily_search({"query": "latest ai news"})

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "tavily_search"
    assert payload["error_class"] == "provider_not_configured"


@pytest.mark.asyncio
async def test_exa_search_forwards_search_type_category_and_filters(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_tool_config(tool_name: str) -> dict:
        assert tool_name == "exa_search"
        return {"api_key": "exa-key", "max_results": 5}

    requests: list[dict] = []
    monkeypatch.setattr(web_mcp, "_get_tool_config", fake_get_tool_config)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                json_data={"results": [{"title": "Paper", "url": "https://arxiv.org/abs/1", "text": "Dense text"}]},
            ),
            requests,
        ),
    )

    result = await web_mcp._exa_search(
        {
            "query": "multi-agent memory papers",
            "max_results": 7,
            "search_type": "fast",
            "category": "research paper",
            "include_domains": ["arxiv.org"],
            "start_published_date": "2025-01-01T00:00:00.000Z",
            "end_published_date": "2026-01-01T00:00:00.000Z",
        }
    )

    assert "Exa search" in result
    assert requests[0]["url"] == "https://api.exa.ai/search"
    assert requests[0]["json"] == {
        "query": "multi-agent memory papers",
        "numResults": 7,
        "type": "fast",
        "category": "research paper",
        "includeDomains": ["arxiv.org"],
        "startPublishedDate": "2025-01-01T00:00:00.000Z",
        "endPublishedDate": "2026-01-01T00:00:00.000Z",
        "contents": {"text": {"maxCharacters": 800}},
    }


@pytest.mark.asyncio
async def test_tavily_search_forwards_topic_depth_time_and_answer_options(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_tool_config(tool_name: str) -> dict:
        assert tool_name == "tavily_search"
        return {"api_key": "tvly-key", "max_results": 5}

    requests: list[dict] = []
    monkeypatch.setattr(web_mcp, "_get_tool_config", fake_get_tool_config)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                json_data={
                    "answer": "Recent answer",
                    "results": [{"title": "News", "url": "https://news.example/a", "content": "Fresh context"}],
                },
            ),
            requests,
        ),
    )

    result = await web_mcp._tavily_search(
        {
            "query": "latest CPI market reaction",
            "max_results": 8,
            "topic": "finance",
            "search_depth": "advanced",
            "time_range": "week",
            "include_answer": "advanced",
            "include_raw_content": "markdown",
            "include_domains": ["reuters.com"],
        }
    )

    assert "Tavily answer" in result
    assert "Recent answer" in result
    assert requests[0]["url"] == "https://api.tavily.com/search"
    assert requests[0]["json"] == {
        "query": "latest CPI market reaction",
        "max_results": 8,
        "search_depth": "advanced",
        "topic": "finance",
        "time_range": "week",
        "include_answer": "advanced",
        "include_raw_content": "markdown",
        "include_domains": ["reuters.com"],
    }


@pytest.mark.asyncio
async def test_web_search_returns_provider_error_when_duckduckgo_fails_without_provider_fallback(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(config={"search_engine": "duckduckgo_legacy", "max_results": 5, "language": "en"})
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_get_exa_api_key() -> str:
        return ""

    monkeypatch.setattr(web_mcp, "_get_exa_api_key", fake_get_exa_api_key)

    async def fake_duckduckgo(query: str, max_results: int) -> str:
        raise RuntimeError("duckduckgo blocked")

    monkeypatch.setattr(web_mcp, "_search_duckduckgo", fake_duckduckgo)

    result = await web_mcp._web_search({"query": "python asyncio", "max_results": 5})

    payload = _extract_tool_error_payload(result)
    assert payload["provider"] == "duckduckgo_legacy"
    assert payload["error_class"] == "provider_error"
    assert "Firecrawl" not in result
    assert "XCrawl" not in result


@pytest.mark.asyncio
async def test_web_search_treats_legacy_google_config_as_basic_search(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(config={"search_engine": "google", "api_key": "key:cx", "max_results": 5, "language": "en"})
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_get_searxng_url() -> str:
        return "https://search.example.com"

    async def fake_searxng(query: str, searxng_url: str, max_results: int, language: str) -> str:
        assert query == "cloud deploy"
        assert searxng_url == "https://search.example.com"
        assert max_results == 5
        return "searxng basic results"

    monkeypatch.setattr(web_mcp, "_get_searxng_url", fake_get_searxng_url)
    monkeypatch.setattr(web_mcp, "_search_searxng", fake_searxng)

    result = await web_mcp._web_search({"query": "cloud deploy"})

    assert "searxng basic results" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_firecrawl_fetch_returns_markdown_content(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_firecrawl_api_key() -> str:
        return "fc-key"

    monkeypatch.setattr(web_mcp, "_get_firecrawl_api_key", fake_get_firecrawl_api_key)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            _FakeResponse(
                status_code=200,
                text='{"success": true, "data": {"markdown": "# Hello\\n\\nWorld"}}',
                json_data={"success": True, "data": {"markdown": "# Hello\n\nWorld"}},
                headers={"content-type": "application/json"},
            ),
        ),
    )

    result = await web_mcp._firecrawl_fetch({"url": "https://example.com/article", "max_chars": 1000})

    assert "Hello" in result
    assert "World" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_firecrawl_fetch_posts_v2_scrape_payload_with_official_options(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_firecrawl_api_key() -> str:
        return "fc-key"

    requests: list[dict] = []
    monkeypatch.setattr(web_mcp, "_get_firecrawl_api_key", fake_get_firecrawl_api_key)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                text='{"success": true, "data": {"markdown": "# Hello\\n\\nWorld"}}',
                json_data={"success": True, "data": {"markdown": "# Hello\n\nWorld"}},
                headers={"content-type": "application/json"},
            ),
            requests,
        ),
    )

    result = await web_mcp._firecrawl_fetch(
        {
            "url": "https://example.com/article",
            "max_chars": 1000,
            "formats": ["markdown", "links"],
            "only_main_content": False,
            "only_clean_content": True,
            "wait_for_ms": 2500,
            "include_tags": ["article"],
            "exclude_tags": ["nav", "footer"],
        }
    )

    assert "Hello" in result
    assert requests[0]["url"] == "https://api.firecrawl.dev/v2/scrape"
    assert requests[0]["json"] == {
        "url": "https://example.com/article",
        "formats": ["markdown", "links"],
        "onlyMainContent": False,
        "onlyCleanContent": True,
        "waitFor": 2500,
        "includeTags": ["article"],
        "excludeTags": ["nav", "footer"],
    }


@pytest.mark.asyncio
async def test_xcrawl_scrape_falls_back_to_firecrawl_on_provider_error(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_xcrawl_api_key() -> str:
        return "xcr-key"

    async def fake_firecrawl_fetch(arguments: dict) -> str:
        assert arguments["url"] == "https://example.com/app"
        return "firecrawl fallback result"

    monkeypatch.setattr(web_mcp, "_get_xcrawl_api_key", fake_get_xcrawl_api_key)
    monkeypatch.setattr(web_mcp, "_firecrawl_fetch", fake_firecrawl_fetch)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            _FakeResponse(status_code=503, text="upstream down", headers={"content-type": "application/json"}),
        ),
    )

    result = await web_mcp._xcrawl_scrape({"url": "https://example.com/app"})

    assert "firecrawl fallback result" in result
    payload = _extract_tool_error_payload(result)
    assert payload["provider"] == "xcrawl"
    assert payload["fallback_tool"] == "firecrawl_fetch"


@pytest.mark.asyncio
async def test_xcrawl_scrape_posts_official_sync_payload(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    async def fake_get_xcrawl_api_key() -> str:
        return "xcr-key"

    requests: list[dict] = []
    monkeypatch.setattr(web_mcp, "_get_xcrawl_api_key", fake_get_xcrawl_api_key)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _CapturingAsyncClient(
            _FakeResponse(
                status_code=200,
                text='{"status":"completed","data":{"markdown":"# App\\n\\nRendered"}}',
                json_data={"status": "completed", "data": {"markdown": "# App\n\nRendered"}},
                headers={"content-type": "application/json"},
            ),
            requests,
        ),
    )

    result = await web_mcp._xcrawl_scrape(
        {
            "url": "https://example.com/app",
            "max_chars": 1000,
            "output_formats": ["markdown", "links"],
            "only_main_content": False,
            "block_ads": False,
            "js_render": False,
            "wait_until": "networkidle",
            "device": "mobile",
            "locale": "zh-CN,zh;q=0.9",
            "proxy_location": "SG",
        }
    )

    assert "Rendered" in result
    assert requests[0]["url"] == "https://run.xcrawl.com/v1/scrape"
    assert requests[0]["json"] == {
        "url": "https://example.com/app",
        "mode": "sync",
        "request": {
            "only_main_content": False,
            "block_ads": False,
            "device": "mobile",
            "locale": "zh-CN,zh;q=0.9",
        },
        "js_render": {
            "enabled": False,
            "wait_until": "networkidle",
        },
        "output": {"formats": ["markdown", "links"]},
        "proxy": {"location": "SG"},
    }


def _make_pdf_bytes(body: str) -> bytes:
    from io import BytesIO

    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, body)
    pdf.save()
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_web_fetch_extracts_text_from_pdf(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    pdf_bytes = _make_pdf_bytes("Quarterly RWA tokenization grew 35 percent in 2026")
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            _FakeResponse(
                status_code=200,
                # httpx would decode the PDF bytes into mojibake; web_fetch must use resp.content instead.
                text="%PDF-1.4 �� binary mojibake",
                content=pdf_bytes,
                headers={"content-type": "application/pdf"},
            ),
        ),
    )

    result = await web_mcp._web_fetch({"url": "https://issuer.example/report.pdf", "max_chars": 5000})

    assert "RWA tokenization grew 35 percent" in result
    assert "%PDF" not in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_web_fetch_rejects_unreadable_pdf(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            _FakeResponse(
                status_code=200,
                text="%PDF garbage",
                content=b"%PDF-1.4\nthis is not a parseable pdf body at all",
                headers={"content-type": "application/pdf"},
            ),
        ),
    )

    result = await web_mcp._web_fetch(
        {"url": "https://issuer.example/broken.pdf", "max_chars": 5000, web_mcp._SKIP_CRAWLER_FALLBACK: True}
    )

    payload = _extract_tool_error_payload(result)
    assert payload["error_class"] == "unreadable_pdf"
