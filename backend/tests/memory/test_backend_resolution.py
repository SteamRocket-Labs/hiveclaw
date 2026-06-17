"""Tests for native T3 Markdown backend resolution."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

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
