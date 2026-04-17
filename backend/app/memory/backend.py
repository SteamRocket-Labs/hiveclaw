"""Pluggable memory backend interface.

The retriever uses this interface to read/write memory. The default
MDBackend wraps the existing md_store + BM25 search. Alternative
backends (Hindsight, Cognee, pgvector, etc.) implement the same
protocol and are swapped via MEMORY_BACKEND config.

Design principles:
- MD files remain the write-side source of truth
- Enhanced backends are read-side accelerators
- Any backend can be rebuilt from MD at any time
- Unknown / missing backend degrades gracefully to MD
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ScoredMemory:
    """A single memory fact with a relevance score."""

    content: str
    category: str
    score: float
    timestamp: str = ""
    metadata: dict[str, Any] | None = None


@runtime_checkable
class MemoryBackend(Protocol):
    """Interface for pluggable memory backends.

    Every backend MUST implement search(). store() and reflect() are
    optional — backends that only accelerate reads can leave them as
    no-ops (the MD pipeline handles writes).
    """

    async def search(
        self,
        agent_id: uuid.UUID,
        query: str,
        *,
        limit: int = 10,
        date_from: str | None = None,
        date_to: str | None = None,
        category: str | None = None,
    ) -> list[ScoredMemory]:
        """Search memory facts by relevance to query."""
        ...

    async def store(
        self,
        agent_id: uuid.UUID,
        content: str,
        category: str,
        *,
        timestamp: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Store a fact (optional — MD pipeline handles writes by default)."""
        ...

    async def reflect(
        self,
        agent_id: uuid.UUID,
        topic: str,
    ) -> str | None:
        """Generate consolidated insight from stored memories (optional).

        Used by dream cycle to produce higher-order understanding.
        Returns a summary string or None if not supported.
        """
        ...


class MDBackend:
    """Default backend — reads/writes T3 markdown files with BM25 search.

    This wraps the existing md_store functions. No external dependencies.
    """

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root

    async def search(
        self,
        agent_id: uuid.UUID,
        query: str,
        *,
        limit: int = 10,
        date_from: str | None = None,
        date_to: str | None = None,
        category: str | None = None,
    ) -> list[ScoredMemory]:
        from app.memory.md_store import search_t3_facts

        facts = search_t3_facts(
            self._data_root,
            agent_id,
            query,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
        )
        results: list[ScoredMemory] = []
        for f in facts:
            if category and f.get("category") != category:
                continue
            results.append(
                ScoredMemory(
                    content=f.get("content", ""),
                    category=f.get("category", "general"),
                    score=1.0,  # BM25 scores are relative, normalize to 1.0
                    timestamp=f.get("timestamp", ""),
                )
            )
        return results

    async def store(
        self,
        agent_id: uuid.UUID,
        content: str,
        category: str,
        *,
        timestamp: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        from app.memory.md_store import append_t3_entry

        append_t3_entry(
            self._data_root,
            agent_id,
            category=category,
            content=content,
            timestamp=timestamp or None,
        )

    async def reflect(
        self,
        agent_id: uuid.UUID,
        topic: str,
    ) -> str | None:
        # MD backend has no reflect capability — dream handles this via LLM
        return None


# ── Backend resolution ──────────────────────────────────────────

_backend_cache: dict[str, MemoryBackend] = {}


def _md_backend() -> MemoryBackend:
    """MDBackend singleton — tenant-agnostic."""
    if "md" not in _backend_cache:
        from app.config import get_settings
        _backend_cache["md"] = MDBackend(Path(get_settings().AGENT_DATA_DIR))
    return _backend_cache["md"]


def _resolve_backend_name(tenant_backend_pref: str | None) -> str:
    """Resolve backend name. Priority: tenant override > env > 'md'."""
    if tenant_backend_pref and tenant_backend_pref.strip():
        return tenant_backend_pref.strip().lower()
    import os
    return os.environ.get("MEMORY_BACKEND", "md").strip().lower()


def get_memory_backend(
    tenant_id: uuid.UUID | None = None,
    *,
    tenant_backend_pref: str | None = None,
) -> MemoryBackend:
    """Return the configured memory backend.

    MDBackend is tenant-agnostic (singleton). HindsightBackend is tenant-scoped
    (cached per tenant_id to reuse its httpx.AsyncClient).

    Backend resolution (first non-empty wins):
    1. ``tenant_backend_pref`` argument (caller read from Tenant.memory_backend)
    2. ``MEMORY_BACKEND`` environment variable
    3. "md" default

    Supported backend names:
    - "md": MDBackend — T3 markdown files + BM25
    - "hindsight": HindsightBackend (Phase A: global key + bank_id isolation)
    - unknown values or unconfigured Hindsight → fall back to MD with warning

    `tenant_id=None` always returns MDBackend regardless of preference —
    tenant-agnostic callers (admin tools, legacy paths) always use MD.
    """
    from app.config import get_settings

    backend_name = _resolve_backend_name(tenant_backend_pref)

    # MD path — tenant-agnostic, shared singleton
    if backend_name == "md" or tenant_id is None:
        return _md_backend()

    if backend_name == "hindsight":
        settings = get_settings()
        if not (settings.HINDSIGHT_ENABLED and settings.HINDSIGHT_URL):
            logger.warning(
                "[MemoryBackend] hindsight selected but disabled/unconfigured "
                "(ENABLED=%s URL=%r), falling back to MD",
                settings.HINDSIGHT_ENABLED, settings.HINDSIGHT_URL,
            )
            return _md_backend()

        cache_key = f"hindsight:{tenant_id.hex}"
        if cache_key in _backend_cache:
            return _backend_cache[cache_key]

        from app.memory.backends.hindsight import HindsightBackend
        _backend_cache[cache_key] = HindsightBackend(
            tenant_id=tenant_id,
            url=settings.HINDSIGHT_URL,
            api_key=settings.HINDSIGHT_API_KEY,
            timeout=settings.HINDSIGHT_TIMEOUT_SECONDS,
        )
        logger.info("[MemoryBackend] Using HindsightBackend for tenant=%s", tenant_id)
        return _backend_cache[cache_key]

    if backend_name == "cognee":
        logger.warning("[MemoryBackend] cognee backend not yet implemented, falling back to MD")
        return _md_backend()

    logger.warning("[MemoryBackend] Unknown backend '%s', falling back to MD", backend_name)
    return _md_backend()


def reset_memory_backend() -> None:
    """Clear all cached backends (for testing).

    Note: this does NOT close HindsightBackend's httpx.AsyncClient — tests
    using MockTransport are unaffected, but production callers must use
    aclose_all_backends() during app shutdown to avoid leaking sockets.
    """
    _backend_cache.clear()


async def aclose_all_backends() -> None:
    """Close all cached backend HTTP clients. Call at app shutdown."""
    for backend in list(_backend_cache.values()):
        close = getattr(backend, "close", None)
        if close is None:
            continue
        try:
            await close()
        except Exception as exc:
            logger.warning("[MemoryBackend] close failed for %s: %s", type(backend).__name__, exc)
    _backend_cache.clear()
