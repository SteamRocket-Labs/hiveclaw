"""Tests for retriever._retrieve_semantic_backend — the Hindsight hook."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.memory.backend import ScoredMemory, reset_memory_backend
from app.memory.retriever import MemoryRetriever
from app.memory.types import MemoryKind


TENANT = uuid.UUID("aaaaaaaa-1111-2222-3333-444444444444")
AGENT = uuid.UUID("bbbbbbbb-5555-6666-7777-888888888888")


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    from app.config import get_settings
    reset_memory_backend()
    get_settings.cache_clear()
    yield
    reset_memory_backend()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_returns_empty_without_query(tmp_path: Path) -> None:
    retriever = MemoryRetriever(data_root=tmp_path)
    assert await retriever._retrieve_semantic_backend(AGENT, "", str(TENANT)) == []


@pytest.mark.asyncio
async def test_returns_empty_without_tenant(tmp_path: Path) -> None:
    retriever = MemoryRetriever(data_root=tmp_path)
    assert await retriever._retrieve_semantic_backend(AGENT, "alice", None) == []


@pytest.mark.asyncio
async def test_returns_empty_when_md_backend_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "md")
    retriever = MemoryRetriever(data_root=tmp_path)
    items = await retriever._retrieve_semantic_backend(AGENT, "alice", str(TENANT))
    # MD path is already covered by _retrieve_t3_direct; this layer must not duplicate
    assert items == []


@pytest.mark.asyncio
async def test_returns_empty_on_invalid_tenant_uuid(tmp_path: Path) -> None:
    retriever = MemoryRetriever(data_root=tmp_path)
    items = await retriever._retrieve_semantic_backend(AGENT, "alice", "not-a-uuid")
    assert items == []


async def _stub_tenant_pref_hindsight(tenant_id):
    return "hindsight"


@pytest.mark.asyncio
async def test_calls_hindsight_backend_when_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    calls: list[tuple[uuid.UUID, str]] = []

    async def fake_search(self, agent_id, query, *, limit=10, **kwargs):
        calls.append((agent_id, query))
        return [
            ScoredMemory(
                content="Alice works at Google",
                category="knowledge",
                score=1.0,
                timestamp="2026-04-15T00:00:00Z",
                metadata={"hindsight_id": "m1"},
            )
        ]

    monkeypatch.setattr(
        "app.memory.hindsight_sync._fetch_tenant_backend_pref",
        _stub_tenant_pref_hindsight,
    )
    monkeypatch.setattr(
        "app.memory.backends.hindsight.HindsightBackend.search", fake_search,
    )

    retriever = MemoryRetriever(data_root=tmp_path)
    items = await retriever._retrieve_semantic_backend(AGENT, "what does alice do?", str(TENANT))

    assert len(calls) == 1
    assert calls[0] == (AGENT, "what does alice do?")
    assert len(items) == 1
    assert items[0].kind == MemoryKind.SEMANTIC
    assert items[0].content.startswith("[knowledge] Alice")
    assert items[0].source == "hindsight"
    assert items[0].metadata["source_type"] == "memory_backend"
    assert items[0].metadata["hindsight_id"] == "m1"


@pytest.mark.asyncio
async def test_swallows_backend_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    async def boom(self, agent_id, query, **kwargs):
        raise RuntimeError("network blew up")

    monkeypatch.setattr(
        "app.memory.hindsight_sync._fetch_tenant_backend_pref",
        _stub_tenant_pref_hindsight,
    )
    monkeypatch.setattr(
        "app.memory.backends.hindsight.HindsightBackend.search", boom,
    )

    retriever = MemoryRetriever(data_root=tmp_path)
    # Must not raise; must not break retrieval pipeline
    items = await retriever._retrieve_semantic_backend(AGENT, "q", str(TENANT))
    assert items == []


@pytest.mark.asyncio
async def test_skips_empty_content_from_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    async def fake_search(self, agent_id, query, **kwargs):
        return [
            ScoredMemory(content="", category="x", score=1.0),
            ScoredMemory(content="   ", category="y", score=0.9),
            ScoredMemory(content="real fact", category="z", score=0.8),
        ]

    monkeypatch.setattr(
        "app.memory.hindsight_sync._fetch_tenant_backend_pref",
        _stub_tenant_pref_hindsight,
    )
    monkeypatch.setattr(
        "app.memory.backends.hindsight.HindsightBackend.search", fake_search,
    )

    retriever = MemoryRetriever(data_root=tmp_path)
    items = await retriever._retrieve_semantic_backend(AGENT, "q", str(TENANT))
    assert len(items) == 1
    assert items[0].content == "[z] real fact"
