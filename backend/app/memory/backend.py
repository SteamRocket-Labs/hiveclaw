"""Native Markdown memory backend.

T3 Markdown remains the only long-term memory store used by Hive's memory
runtime. This module intentionally exposes a small protocol so future optional
enhancement adapters can be built outside the native T3 chain, but backend
resolution always returns the Markdown backend.
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


# -- Backend resolution -------------------------------------------------------

_backend_cache: dict[str, MemoryBackend] = {}


def _md_backend() -> MemoryBackend:
    """MDBackend singleton — tenant-agnostic."""
    if "md" not in _backend_cache:
        from app.config import get_settings
        _backend_cache["md"] = MDBackend(Path(get_settings().AGENT_DATA_DIR))
    return _backend_cache["md"]


def get_memory_backend(
    tenant_id: uuid.UUID | None = None,
    *,
    tenant_backend_pref: str | None = None,
) -> MemoryBackend:
    """Return the native T3 Markdown backend.

    ``tenant_id`` and ``tenant_backend_pref`` are accepted only for compatibility
    with older call sites and persisted tenant rows. They deliberately do not
    select another memory system.
    """
    del tenant_id, tenant_backend_pref
    return _md_backend()


def reset_memory_backend() -> None:
    """Clear cached backend instances for tests."""
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
