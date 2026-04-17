"""Tests for get_memory_backend(tenant_id) resolution and caching."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from app.memory.backend import (
    MDBackend,
    aclose_all_backends,
    get_memory_backend,
    reset_memory_backend,
)
from app.memory.backends.hindsight import HindsightBackend


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
    monkeypatch.setenv("MEMORY_BACKEND", "md")
    b1 = get_memory_backend(tenant_id=uuid.uuid4())
    b2 = get_memory_backend(tenant_id=uuid.uuid4())
    assert b1 is b2  # tenant-agnostic singleton


def test_tenant_id_none_always_returns_md(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    # Even when hindsight is enabled, tenant_id=None → MD
    backend = get_memory_backend(tenant_id=None)
    assert isinstance(backend, MDBackend)


def test_hindsight_falls_back_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    backend = get_memory_backend(tenant_id=uuid.uuid4())
    assert isinstance(backend, MDBackend)


def test_hindsight_falls_back_when_url_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    backend = get_memory_backend(tenant_id=uuid.uuid4())
    assert isinstance(backend, MDBackend)


def test_hindsight_activated_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    backend = get_memory_backend(tenant_id=uuid.uuid4())
    assert isinstance(backend, HindsightBackend)


def test_hindsight_per_tenant_instance_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    a1 = get_memory_backend(tenant_id=tenant_a)
    a2 = get_memory_backend(tenant_id=tenant_a)
    b1 = get_memory_backend(tenant_id=tenant_b)

    assert a1 is a2, "same tenant must return the same cached instance"
    assert a1 is not b1, "different tenants must get different instances"


def test_unknown_backend_falls_back_to_md(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "cognee")
    backend = get_memory_backend(tenant_id=uuid.uuid4())
    assert isinstance(backend, MDBackend)

    monkeypatch.setenv("MEMORY_BACKEND", "nonsense-value")
    reset_memory_backend()
    backend = get_memory_backend(tenant_id=uuid.uuid4())
    assert isinstance(backend, MDBackend)


# ── tenant-level opt-in (Phase 3) ─────────────────────────────


def test_tenant_pref_overrides_env_to_hindsight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenant says 'hindsight' even though env says 'md'."""
    monkeypatch.setenv("MEMORY_BACKEND", "md")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    backend = get_memory_backend(
        tenant_id=uuid.uuid4(), tenant_backend_pref="hindsight",
    )
    assert isinstance(backend, HindsightBackend)


def test_tenant_pref_overrides_env_to_md(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenant explicitly opted out even though env says 'hindsight'."""
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    backend = get_memory_backend(
        tenant_id=uuid.uuid4(), tenant_backend_pref="md",
    )
    assert isinstance(backend, MDBackend)


def test_tenant_pref_none_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    backend = get_memory_backend(
        tenant_id=uuid.uuid4(), tenant_backend_pref=None,
    )
    assert isinstance(backend, HindsightBackend)


def test_tenant_pref_empty_string_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace/empty preference is treated as 'unset'."""
    monkeypatch.setenv("MEMORY_BACKEND", "md")
    backend = get_memory_backend(
        tenant_id=uuid.uuid4(), tenant_backend_pref="   ",
    )
    assert isinstance(backend, MDBackend)


@pytest.mark.asyncio
async def test_aclose_all_backends_invokes_close_and_clears_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    a = get_memory_backend(tenant_id=tenant_a, tenant_backend_pref="hindsight")
    b = get_memory_backend(tenant_id=tenant_b, tenant_backend_pref="hindsight")
    assert isinstance(a, HindsightBackend)
    assert isinstance(b, HindsightBackend)

    closed: list[uuid.UUID] = []
    async def tracking_close(self=a):
        closed.append(self._tenant_id)
    a.close = tracking_close  # type: ignore[method-assign]

    async def tracking_close_b(self=b):
        closed.append(self._tenant_id)
    b.close = tracking_close_b  # type: ignore[method-assign]

    await aclose_all_backends()

    assert set(closed) == {tenant_a, tenant_b}
    # Cache is cleared → next call yields a fresh instance
    c = get_memory_backend(tenant_id=tenant_a, tenant_backend_pref="hindsight")
    assert c is not a


@pytest.mark.asyncio
async def test_aclose_all_backends_survives_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    backend = get_memory_backend(tenant_id=uuid.uuid4(), tenant_backend_pref="hindsight")
    assert isinstance(backend, HindsightBackend)

    async def raising_close():
        raise RuntimeError("close boom")
    backend.close = raising_close  # type: ignore[method-assign]

    # Must not raise
    await aclose_all_backends()
