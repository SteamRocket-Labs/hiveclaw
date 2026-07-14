from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


def _install_fake_markitdown(monkeypatch, output: str) -> None:
    class _FakeMarkItDown:
        def convert_local(self, _source: str):
            return SimpleNamespace(text_content=output)

    monkeypatch.setitem(sys.modules, "markitdown", SimpleNamespace(MarkItDown=_FakeMarkItDown))


@pytest.mark.asyncio
async def test_read_document_returns_conversion_preview_and_artifact_paths(monkeypatch, tmp_path):
    from app.services.agent_tool_domains.workspace import _read_document

    _install_fake_markitdown(monkeypatch, "# Report\n\nConverted body")
    ws = tmp_path / "agent"
    source = ws / "workspace" / "uploads" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF fake")

    result = await _read_document(ws, "workspace/uploads/report.pdf", max_chars=80, return_format="preview")

    assert "Converted with local_markitdown." in result
    assert "Full Markdown: workspace/.hive/document_conversions/" in result
    assert "Metadata: workspace/.hive/document_conversions/" in result
    assert "# Report" in result
    assert (ws / "workspace" / ".hive" / "document_conversions").exists()


@pytest.mark.asyncio
async def test_read_document_supports_full_markdown_return_format(monkeypatch, tmp_path):
    from app.services.agent_tool_domains.workspace import _read_document

    _install_fake_markitdown(monkeypatch, "# Full\n\nNo preview wrapper")
    ws = tmp_path / "agent"
    source = ws / "workspace" / "uploads" / "report.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"docx")

    result = await _read_document(
        ws,
        "workspace/uploads/report.docx",
        return_format="markdown",
    )

    assert result == "# Full\n\nNo preview wrapper"


@pytest.mark.asyncio
async def test_read_document_handler_defaults_to_complete_markdown(monkeypatch, tmp_path) -> None:
    from app.tools.handlers import filesystem as filesystem_handler

    captured: dict = {}

    async def fake_read_document(_workspace, _path, **kwargs):
        captured.update(kwargs)
        return "complete document"

    monkeypatch.setattr("app.services.agent_tool_domains.workspace._read_document", fake_read_document)

    result = await filesystem_handler.read_document(
        tmp_path,
        {"path": "workspace/uploads/report.pdf"},
    )

    assert result == "complete document"
    assert captured["return_format"] == "markdown"
    assert captured["max_chars"] is None


@pytest.mark.asyncio
async def test_read_document_full_markdown_never_prunes_semantic_tail(monkeypatch, tmp_path) -> None:
    from app.services.agent_tool_domains.workspace import _read_document

    tail = "DECISIVE_DOCUMENT_TAIL"
    _install_fake_markitdown(monkeypatch, ("document evidence " * 1_000) + tail)
    ws = tmp_path / "agent"
    source = ws / "workspace" / "uploads" / "long.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"docx")

    result = await _read_document(
        ws,
        "workspace/uploads/long.docx",
        max_chars=200,
        return_format="markdown",
    )

    assert tail in result


@pytest.mark.asyncio
async def test_read_document_rejects_escape_before_conversion(tmp_path):
    from app.services.agent_tool_domains.workspace import _read_document

    ws = tmp_path / "agent"
    ws.mkdir()
    sibling = tmp_path / "agent-evil"
    sibling.mkdir()
    (sibling / "secret.pdf").write_bytes(b"%PDF secret")

    result = await _read_document(ws, "../agent-evil/secret.pdf")

    assert "Access denied" in result
    assert "secret" not in result
