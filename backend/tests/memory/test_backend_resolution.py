"""Tests for native T3 Markdown backend resolution."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from app.memory.backend import (
    MDBackend,
    _backend_cache,
    aclose_all_backends,
    get_memory_backend,
    reset_memory_backend,
)


@pytest.fixture(autouse=True)
def _clean_backend_cache() -> Iterator[None]:
    from app.config import get_settings

    reset_memory_backend()
    get_settings.cache_clear()
    yield
    reset_memory_backend()
    get_settings.cache_clear()


def test_md_backend_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    backend = get_memory_backend(tenant_id=uuid.uuid4())
    assert isinstance(backend, MDBackend)


def test_md_backend_shared_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "external")
    b1 = get_memory_backend(tenant_id=uuid.uuid4())
    b2 = get_memory_backend(tenant_id=uuid.uuid4())
    assert b1 is b2
    assert isinstance(b1, MDBackend)


def test_legacy_tenant_backend_preference_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "external")
    backend = get_memory_backend(
        tenant_id=uuid.uuid4(),
        tenant_backend_pref="external",
    )
    assert isinstance(backend, MDBackend)


@pytest.mark.asyncio
async def test_md_backend_store_writes_explicit_overlay_not_accepted_t3(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory.write_gate import MemoryWriteDecision

    async def accept_reviewed_memory(content: str, *, category: str, **_kwargs) -> MemoryWriteDecision:
        return MemoryWriteDecision(
            original_content=content,
            content=content,
            category=category,
            sensitivity="PL1_public",
            metadata={"threat_gate_method": "test_model_reviewer"},
        )

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(
        "app.memory.explicit_overlay.prepare_memory_write_with_llm",
        accept_reviewed_memory,
    )
    backend = MDBackend(data_root=tmp_path)
    agent_id = uuid.uuid4()

    await backend.store(agent_id, "User prefers concise answers", "feedback")

    memory_dir = tmp_path / str(agent_id) / "memory"
    assert (memory_dir / "explicit" / "MEMORY.md").exists()
    assert not (memory_dir / "feedback.md").exists()
    assert not (memory_dir / "t3" / "user.md").exists()


@pytest.mark.asyncio
async def test_md_backend_search_has_no_hidden_default_candidate_limit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MDBackend(data_root=tmp_path)
    agent_id = uuid.uuid4()
    captured: dict[str, object] = {}

    def fake_search_plane_facts(*_args, **kwargs):
        captured.update(kwargs)
        return [{"content": f"candidate {index}", "category": "general"} for index in range(25)]

    monkeypatch.setattr("app.memory.plane_read.search_plane_facts", fake_search_plane_facts)

    results = await backend.search(agent_id, "candidate")

    assert captured["limit"] is None
    assert len(results) == 25


@pytest.mark.asyncio
async def test_aclose_all_backends_closes_cached_objects() -> None:
    closed: list[str] = []

    class _Closable:
        async def close(self) -> None:
            closed.append("closed")

    _backend_cache["test"] = _Closable()  # type: ignore[assignment]

    await aclose_all_backends()

    assert closed == ["closed"]
    assert _backend_cache == {}


@pytest.mark.asyncio
async def test_aclose_all_backends_survives_close_error() -> None:
    class _BrokenClosable:
        async def close(self) -> None:
            raise RuntimeError("close boom")

    _backend_cache["test"] = _BrokenClosable()  # type: ignore[assignment]

    await aclose_all_backends()

    assert _backend_cache == {}
