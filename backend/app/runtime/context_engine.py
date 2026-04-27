"""Context harness primitives.

ContextEngine does not own memory or objectives. It wraps context blocks with
explicit source/fence metadata and records reference artifacts on SessionContext.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html import escape
from typing import Protocol

from app.runtime.session import SessionContext


class MemoryProvider(Protocol):
    """Provider contract for memory-like context sources."""

    name: str

    async def prefetch(self, *args, **kwargs) -> str:
        """Fetch stable snapshot context before a turn."""

    async def inject(self, *args, **kwargs) -> str:
        """Fetch query-scoped context for a turn."""

    async def sync_turn(self, *args, **kwargs) -> None:
        """Sync turn outcomes back to the provider."""

    async def on_pre_compress(self, *args, **kwargs) -> str:
        """Return provider state that must be preserved before compaction."""

    async def on_session_end(self, *args, **kwargs) -> None:
        """Finalize provider state at session end."""


class ContextEngine(Protocol):
    """Request-scoped context harness contract."""

    def inject(self, session_context: SessionContext | None, *, kind: str, source: str, content: str) -> str:
        """Return fenced content and record a reference artifact."""


@dataclass(slots=True)
class DefaultContextEngine:
    artifact_limit: int = 50

    def inject(self, session_context: SessionContext | None, *, kind: str, source: str, content: str) -> str:
        stripped = (content or "").strip()
        if not stripped:
            return ""

        if session_context is not None:
            metadata = session_context.metadata if isinstance(session_context.metadata, dict) else {}
            session_context.metadata = metadata
            artifacts = metadata.setdefault("context_artifacts", [])
            artifacts.append(
                {
                    "kind": kind,
                    "source": source,
                    "char_count": len(stripped),
                    "content_hash": hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
                }
            )
            if len(artifacts) > self.artifact_limit:
                del artifacts[:-self.artifact_limit]

        safe_kind = escape(kind, quote=True)
        safe_source = escape(source, quote=True)
        return f'<context_block kind="{safe_kind}" source="{safe_source}">\n{stripped}\n</context_block>'
