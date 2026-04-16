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

_backend_instance: MemoryBackend | None = None


def get_memory_backend() -> MemoryBackend:
    """Return the configured memory backend (singleton).

    Reads MEMORY_BACKEND env var:
    - "md" (default): MDBackend using T3 markdown files + BM25
    - "hindsight": future Hindsight integration
    - "cognee": future Cognee integration

    Unknown values fall back to MD with a warning.
    """
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    import os
    from app.config import get_settings

    backend_name = os.environ.get("MEMORY_BACKEND", "md").strip().lower()
    settings = get_settings()
    data_root = Path(settings.AGENT_DATA_DIR)

    if backend_name == "md":
        _backend_instance = MDBackend(data_root)
    elif backend_name == "hindsight":
        # Future: from app.memory.backends.hindsight import HindsightBackend
        # _backend_instance = HindsightBackend(url=settings.HINDSIGHT_URL, ...)
        logger.warning("[MemoryBackend] hindsight backend not yet implemented, falling back to MD")
        _backend_instance = MDBackend(data_root)
    elif backend_name == "cognee":
        logger.warning("[MemoryBackend] cognee backend not yet implemented, falling back to MD")
        _backend_instance = MDBackend(data_root)
    else:
        logger.warning("[MemoryBackend] Unknown backend '%s', falling back to MD", backend_name)
        _backend_instance = MDBackend(data_root)

    logger.info("[MemoryBackend] Using %s backend", type(_backend_instance).__name__)
    return _backend_instance


def reset_memory_backend() -> None:
    """Reset the singleton (for testing)."""
    global _backend_instance
    _backend_instance = None
