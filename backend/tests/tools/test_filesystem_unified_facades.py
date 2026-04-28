"""P1-W3-6 — unified filesystem facades.

Three new tools (fs_read / fs_write / fs_list) dispatch to the existing
nine fine-grained handlers via a `mode` argument. They live next to the
old tools — backwards compat keeps the per-action surface available so
nothing breaks, but new agent prompts can ship with only the facades on
day one.

These tests pin the dispatch contract so future contributors can refactor
the underlying handlers without breaking the facade surface.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from app.tools.handlers.filesystem import fs_list, fs_read, fs_write


# ── fs_read modes ────────────────────────────────────────────


def test_fs_read_text_dispatches_to_read_file(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_read_file(workspace, args, tenant_id=None):
        captured["args"] = args
        captured["workspace"] = workspace
        return "TEXT_RESULT"

    monkeypatch.setattr("app.tools.handlers.filesystem.read_file", fake_read_file)

    out = fs_read(tmp_path, {"path": "soul.md", "mode": "text"})
    assert out == "TEXT_RESULT"
    assert captured["args"] == {"path": "soul.md"}


def test_fs_read_default_mode_is_text(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.tools.handlers.filesystem.read_file",
        lambda *a, **kw: "TEXT_DEFAULT",
    )
    assert fs_read(tmp_path, {"path": "f.md"}) == "TEXT_DEFAULT"


def test_fs_read_document_dispatches_to_read_document(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.tools.handlers.filesystem.read_document",
        lambda *a, **kw: "DOC_RESULT",
    )
    assert fs_read(tmp_path, {"path": "x.pdf", "mode": "document"}) == "DOC_RESULT"


def test_fs_read_glob_passes_pattern_default_star(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_glob(workspace, args, tenant_id=None):
        captured["args"] = args
        return "GLOB"

    monkeypatch.setattr("app.tools.handlers.filesystem.glob_search", fake_glob)

    fs_read(tmp_path, {"path": "src", "mode": "glob"})
    assert captured["args"] == {"path": "src", "pattern": "*"}


def test_fs_read_grep_forwards_pattern(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_grep(workspace, args, tenant_id=None):
        captured["args"] = args
        return "GREP"

    monkeypatch.setattr("app.tools.handlers.filesystem.grep_search", fake_grep)

    fs_read(tmp_path, {"path": ".", "mode": "grep", "pattern": "TODO"})
    assert captured["args"] == {"path": ".", "pattern": "TODO"}


def test_fs_read_unknown_mode_returns_clear_error(tmp_path):
    out = fs_read(tmp_path, {"path": "f", "mode": "exotic"})
    assert "unknown mode" in out
    assert "'exotic'" in out


# ── fs_write modes ───────────────────────────────────────────


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.mark.asyncio
async def test_fs_write_default_mode_is_write(monkeypatch, tmp_path):
    captured: dict = {}

    async def fake_write(workspace, args, tenant_id=None):
        captured["args"] = args
        return "WROTE"

    monkeypatch.setattr("app.tools.handlers.filesystem.write_file", fake_write)

    out = await fs_write(tmp_path, {"path": "n.md", "content": "hi"})
    assert out == "WROTE"
    assert captured["args"] == {"path": "n.md", "content": "hi"}


@pytest.mark.asyncio
async def test_fs_write_edit_threads_old_new_strings(monkeypatch, tmp_path):
    captured: dict = {}

    async def fake_edit(workspace, args, tenant_id=None):
        captured["args"] = args
        return "EDITED"

    monkeypatch.setattr("app.tools.handlers.filesystem.edit_file", fake_edit)

    await fs_write(
        tmp_path,
        {"path": "f.md", "mode": "edit", "old_string": "A", "new_string": "B"},
    )
    assert captured["args"] == {"path": "f.md", "old_string": "A", "new_string": "B"}


@pytest.mark.asyncio
async def test_fs_write_delete_dispatches_to_delete_file(monkeypatch, tmp_path):
    captured: dict = {}

    async def fake_delete(workspace, args, tenant_id=None):
        captured["args"] = args
        return "DELETED"

    monkeypatch.setattr("app.tools.handlers.filesystem.delete_file", fake_delete)

    out = await fs_write(tmp_path, {"path": "tmp", "mode": "delete"})
    assert out == "DELETED"
    assert captured["args"] == {"path": "tmp"}


@pytest.mark.asyncio
async def test_fs_write_unknown_mode_returns_clear_error(tmp_path):
    out = await fs_write(tmp_path, {"path": "f", "mode": "rename"})
    assert "unknown mode" in out
    assert "'rename'" in out


# ── fs_list ──────────────────────────────────────────────────


def test_fs_list_dispatches_to_list_files(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_list(workspace, args, tenant_id=None):
        captured["args"] = args
        return "LISTED"

    monkeypatch.setattr("app.tools.handlers.filesystem.list_files", fake_list)

    out = fs_list(tmp_path, {"path": "skills"})
    assert out == "LISTED"
    assert captured["args"] == {"path": "skills"}


# ── Capability + governance integration ───────────────────────


def test_capability_map_covers_all_three_facades() -> None:
    from app.services.capability_gate import CAPABILITY_MAP

    assert CAPABILITY_MAP["fs_read"] == "workspace.file.read"
    assert CAPABILITY_MAP["fs_write"] == "workspace.file.write"
    assert CAPABILITY_MAP["fs_list"] == "workspace.file.read"


def test_facades_share_governance_with_underlying_tools() -> None:
    """fs_read / fs_list must be SAFE; fs_write must be SENSITIVE — i.e.
    the same governance bucket as the per-action tool they dispatch to."""
    from app.tools.governance import SAFE_TOOLS, SENSITIVE_TOOLS

    assert "fs_read" in SAFE_TOOLS
    assert "fs_list" in SAFE_TOOLS
    assert "fs_write" in SENSITIVE_TOOLS


def test_fs_write_is_async_function() -> None:
    """write/edit/delete are async; the facade must mirror that or the
    runtime will silently miss the awaitable."""
    assert inspect.iscoroutinefunction(fs_write)


def test_fs_read_and_list_are_sync() -> None:
    """The underlying read/list helpers are sync — the facades stay sync
    so callers don't pay an unnecessary task scheduling cost."""
    assert not inspect.iscoroutinefunction(fs_read)
    assert not inspect.iscoroutinefunction(fs_list)


# ── Prompt rubric mentions the facades ───────────────────────


def test_tools_section_prompt_mentions_unified_facades() -> None:
    """The agent rubric must surface the facades so the LLM knows it can
    reach for them."""
    from app.runtime.prompt_sections import build_tools_section

    section = build_tools_section()
    assert "fs_read" in section
    assert "fs_write" in section
    assert "fs_list" in section


# Avoid pytest collection complaint about unused helper.
_ = _run
del _
