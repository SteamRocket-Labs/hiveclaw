from __future__ import annotations

import json
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
        ("post", "https://api.firecrawl.dev/v1/scrape"),
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
        ("post", "https://api.firecrawl.dev/v1/scrape"),
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
async def test_web_search_falls_back_to_duckduckgo_when_searxng_returns_error_string(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(config={"search_engine": "auto", "max_results": 5, "language": "en"})
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_get_searxng_url() -> str:
        return "https://search.example.com"

    async def fake_searxng(query: str, searxng_url: str, max_results: int, language: str) -> str:
        assert query == "openai sdk"
        assert searxng_url == "https://search.example.com"
        assert max_results == 5
        assert language == "en"
        return "❌ SearXNG search failed: upstream 400"

    async def fake_duckduckgo(query: str, max_results: int) -> str:
        assert query == "openai sdk"
        assert max_results == 5
        return "duckduckgo fallback results"

    monkeypatch.setattr(web_mcp, "_get_searxng_url", fake_get_searxng_url)
    monkeypatch.setattr(web_mcp, "_search_searxng", fake_searxng)
    monkeypatch.setattr(web_mcp, "_search_duckduckgo", fake_duckduckgo)

    result = await web_mcp._web_search({"query": "openai sdk"})

    assert "duckduckgo fallback results" in result
    payload = _extract_tool_error_payload(result)
    assert payload["provider"] == "searxng"
    assert payload["fallback_tool"] == "web_search:duckduckgo"


@pytest.mark.asyncio
async def test_web_search_auto_uses_basic_search_even_when_exa_key_is_available(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(config={"search_engine": "auto", "max_results": 5, "language": "en"})
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_get_exa_api_key() -> str:
        return "exa-key"

    async def fake_get_searxng_url() -> str:
        return ""

    async def fake_exa(query: str, api_key: str, max_results: int) -> str:
        raise AssertionError("web_search must not auto-route to Exa; use exa_search via tool_search instead")

    async def fake_duckduckgo(query: str, max_results: int) -> str:
        assert query == "python asyncio"
        assert max_results == 5
        return "duckduckgo basic results"

    monkeypatch.setattr(web_mcp, "_get_exa_api_key", fake_get_exa_api_key)
    monkeypatch.setattr(web_mcp, "_get_searxng_url", fake_get_searxng_url)
    monkeypatch.setattr(web_mcp, "_search_exa", fake_exa)
    monkeypatch.setattr(web_mcp, "_search_duckduckgo", fake_duckduckgo)

    result = await web_mcp._web_search({"query": "python asyncio", "max_results": 5})

    assert "duckduckgo basic results" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_exa_search_uses_provider_config(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(config={"api_key": "exa-key", "max_results": 5})
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_exa(query: str, api_key: str, max_results: int) -> str:
        assert query == "semantic competitors"
        assert api_key == "exa-key"
        assert max_results == 5
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
async def test_web_search_returns_provider_error_when_duckduckgo_fails_without_provider_fallback(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(config={"search_engine": "duckduckgo", "max_results": 5, "language": "en"})
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_get_exa_api_key() -> str:
        return ""

    monkeypatch.setattr(web_mcp, "_get_exa_api_key", fake_get_exa_api_key)

    async def fake_duckduckgo(query: str, max_results: int) -> str:
        raise RuntimeError("duckduckgo blocked")

    monkeypatch.setattr(web_mcp, "_search_duckduckgo", fake_duckduckgo)

    result = await web_mcp._web_search({"query": "python asyncio", "max_results": 5})

    payload = _extract_tool_error_payload(result)
    assert payload["provider"] == "duckduckgo"
    assert payload["error_class"] == "provider_error"
    assert "Firecrawl" not in result
    assert "XCrawl" not in result


@pytest.mark.asyncio
async def test_web_search_treats_legacy_google_config_as_basic_search(monkeypatch):
    from app.services.agent_tool_domains import web_mcp

    tool = SimpleNamespace(config={"search_engine": "google", "api_key": "key:cx", "max_results": 5, "language": "en"})
    monkeypatch.setattr(web_mcp, "async_session", lambda: _FakeSession(tool))

    async def fake_get_searxng_url() -> str:
        return ""

    async def fake_duckduckgo(query: str, max_results: int) -> str:
        assert query == "cloud deploy"
        assert max_results == 5
        return "duckduckgo basic results"

    monkeypatch.setattr(web_mcp, "_get_searxng_url", fake_get_searxng_url)
    monkeypatch.setattr(web_mcp, "_search_duckduckgo", fake_duckduckgo)

    result = await web_mcp._web_search({"query": "cloud deploy"})

    assert "duckduckgo basic results" in result
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
