from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.services.test_web_mcp_resilience import _FakeResponse


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, *args, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_web_fetch_hands_fetched_html_bytes_to_document_conversion(monkeypatch, tmp_path):
    from app.services.agent_tool_domains import web_mcp

    conversions: list[dict] = []

    class _FakeDocumentConversionService:
        def convert_bytes(self, **kwargs):
            conversions.append(kwargs)
            return SimpleNamespace(
                markdown="# Web page\n\nConverted by service",
                artifact_markdown_path=".hive/document_conversions/html/content.md",
                artifact_metadata_path=".hive/document_conversions/html/metadata.json",
                engine="local_markitdown",
                warnings=(),
            )

    monkeypatch.setattr(web_mcp, "DocumentConversionService", _FakeDocumentConversionService, raising=False)
    monkeypatch.setattr(web_mcp, "_WEB_FETCH_CONVERSION_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            _FakeResponse(
                status_code=200,
                text="<html><body><h1>Raw</h1></body></html>",
                content=b"<html><body><h1>Raw</h1></body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            ),
        ),
    )

    result = await web_mcp._web_fetch({"url": "https://example.com/page", "max_chars": 1000})

    assert "# Web page" in result
    assert "Converted with local_markitdown." in result
    assert conversions
    assert conversions[0]["data"] == b"<html><body><h1>Raw</h1></body></html>"
    assert conversions[0]["filename"] == "page.html"
    assert conversions[0]["workspace_root"] == Path(tmp_path)
    assert conversions[0]["source_uri"] == "https://example.com/page"
    assert conversions[0]["source_mime_type"] == "text/html; charset=utf-8"
