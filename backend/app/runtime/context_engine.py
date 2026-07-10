"""Context harness primitives.

ContextEngine does not own memory or objectives. It wraps context blocks with
explicit source/fence metadata and records reference artifacts on SessionContext.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html import escape
from typing import Any, Protocol

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


def _metadata_artifacts(session_context: SessionContext, *, artifact_limit: int) -> list[dict[str, Any]]:
    metadata = session_context.metadata if isinstance(session_context.metadata, dict) else {}
    session_context.metadata = metadata
    artifacts = metadata.setdefault("context_artifacts", [])
    if not isinstance(artifacts, list):
        artifacts = []
        metadata["context_artifacts"] = artifacts
    if len(artifacts) > artifact_limit:
        del artifacts[:-artifact_limit]
    return artifacts


def record_prompt_manifest_context_artifacts(
    session_context: SessionContext | None,
    prompt_manifest: dict[str, Any] | None,
    *,
    artifact_limit: int = 50,
) -> None:
    """Record prompt-manifest selected contexts on the normal context artifact rail."""
    if session_context is None or not isinstance(prompt_manifest, dict):
        return
    selected_contexts = prompt_manifest.get("selected_contexts")
    if not isinstance(selected_contexts, list) or not selected_contexts:
        return

    artifacts = _metadata_artifacts(session_context, artifact_limit=artifact_limit)
    seen = {
        (
            str(item.get("candidate_id") or ""),
            str(item.get("source") or ""),
            str(item.get("content_hash") or ""),
        )
        for item in artifacts
        if isinstance(item, dict)
    }
    for candidate in selected_contexts:
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("id") or "").strip()
        source = str(candidate.get("source_ref") or "").strip()
        content_hash = str(candidate.get("source_hash") or "").strip()
        if not candidate_id or not source or not content_hash:
            continue
        key = (candidate_id, source, content_hash)
        if key in seen:
            continue
        artifacts.append(
            {
                "kind": str(candidate.get("name") or candidate.get("kind") or "prompt_context"),
                "source": source,
                "char_count": int(candidate.get("chars") or 0),
                "token_count": int(candidate.get("tokens") or 0),
                "content_hash": content_hash,
                "candidate_id": candidate_id,
                "selection_reason": str(candidate.get("why_selected") or ""),
                "cacheability": str(candidate.get("cacheability") or "dynamic"),
            }
        )
        seen.add(key)
    if len(artifacts) > artifact_limit:
        del artifacts[:-artifact_limit]


@dataclass(slots=True)
class DefaultContextEngine:
    artifact_limit: int = 50

    def inject(self, session_context: SessionContext | None, *, kind: str, source: str, content: str) -> str:
        stripped = (content or "").strip()
        if not stripped:
            return ""

        if session_context is not None:
            artifacts = _metadata_artifacts(session_context, artifact_limit=self.artifact_limit)
            artifacts.append(
                {
                    "kind": kind,
                    "source": source,
                    "char_count": len(stripped),
                    "content_hash": hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
                }
            )
            if len(artifacts) > self.artifact_limit:
                del artifacts[: -self.artifact_limit]

        safe_kind = escape(kind, quote=True)
        safe_source = escape(source, quote=True)
        return f'<context_block kind="{safe_kind}" source="{safe_source}">\n{stripped}\n</context_block>'
