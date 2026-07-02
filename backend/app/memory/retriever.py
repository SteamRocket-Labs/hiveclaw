"""Four-layer memory retrieval pipeline.

Retrieves memory items from working, episodic, semantic, and external layers,
returning a unified list of MemoryItem objects for the assembler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re as _re
import uuid
from pathlib import Path
from typing import Any

from app.memory.activation import ActivationContext, ActivationScorer, memory_lifecycle_suppression_reason
from app.memory.explicit_overlay import search_explicit_overlay_entries
from app.memory.types import MemoryItem, MemoryKind
from app.runtime.context_budget import ContextBudget

logger = logging.getLogger(__name__)

_CJK_RE = _re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF]")
_PUNCTUATION_CHARS = frozenset("，。！？；：''（）【】、…—《》·,.!?;:\"'()[]{}/ \t\n\r")


def _has_cjk(text: str) -> bool:
    """Detect if text contains CJK characters."""
    return bool(_CJK_RE.search(text))


def _chars_set(text: str) -> set[str]:
    """Extract meaningful characters for CJK overlap scoring, filtering punctuation/whitespace."""
    return {c for c in text.lower() if c not in _PUNCTUATION_CHARS and not c.isspace()}


def _content_similar(a: str, b: str, threshold: float = 0.7) -> bool:
    """Check if two text blocks are similar using word overlap (English) or char overlap (CJK)."""
    a_lower = a.lower()
    b_lower = b.lower()

    # Path 1: word overlap (English)
    words_a = set(a_lower.split())
    words_b = set(b_lower.split())
    word_sim = 0.0
    if words_a and words_b:
        word_sim = len(words_a & words_b) / min(len(words_a), len(words_b))

    # Path 2: character overlap (CJK)
    char_sim = 0.0
    if _has_cjk(a) or _has_cjk(b):
        chars_a = _chars_set(a)
        chars_b = _chars_set(b)
        if chars_a and chars_b:
            char_sim = len(chars_a & chars_b) / min(len(chars_a), len(chars_b))

    return max(word_sim, char_sim) > threshold


class MemoryRetriever:
    """Four-layer memory retrieval pipeline.

    Each layer maps to a MemoryKind and retrieves items independently.
    The retriever works without a database connection (for testability);
    DB-dependent layers degrade gracefully with logging.
    """

    def __init__(self, *, data_root: Path) -> None:
        self.data_root = Path(data_root)

    async def retrieve(
        self,
        agent_id: uuid.UUID,
        query: str,
        session_id: str | None,
        tenant_id: str | None,
        *,
        limit: int = 50,
        rerank_model_config: dict | None = None,
        retrieval_profile: ContextBudget | None = None,
        activation_context: ActivationContext | None = None,
    ) -> list[MemoryItem]:
        """Retrieve memory items: explicit overlay, knowledge plane, episodic, hooks.

        Reads never run an LLM (spec §4.2). ``rerank_model_config`` is accepted
        for caller compatibility and intentionally ignored.
        """
        items: list[MemoryItem] = []
        items.extend(self._retrieve_explicit_overlay(agent_id, query=query) or [])
        episodic_limit = retrieval_profile.episodic_limit if retrieval_profile else 3
        external_limit = retrieval_profile.external_limit if retrieval_profile else 5
        semantic_limit = retrieval_profile.semantic_limit if retrieval_profile else 5
        del limit, rerank_model_config  # reads never run an LLM (spec §4.2); PPR order is final
        # Knowledge plane (spec §4.2): always-on top-k retrieval over the
        # knowledge/milestones link network built at write time.
        items.extend(self._retrieve_knowledge_pages(agent_id, query=query, limit=semantic_limit) or [])

        items.extend(await self._retrieve_episodic(agent_id, session_id, previous_limit=episodic_limit) or [])
        items.extend(
            await self._retrieve_semantic_backend(
                agent_id,
                query,
                tenant_id,
                limit=semantic_limit,
            )
            or []
        )
        items.extend(
            await self._retrieve_external(
                agent_id,
                query,
                tenant_id,
                limit=external_limit,
                activation_context=activation_context,
            )
            or []
        )

        if activation_context:
            return self._apply_activation(items, activation_context, agent_id=agent_id)
        return items

    def _retrieve_knowledge_pages(self, agent_id: uuid.UUID, *, query: str = "", limit: int = 5) -> list[MemoryItem]:
        """Knowledge plane retrieval (spec §4.2): PPR top-k over knowledge/milestones.

        Zero LLM by contract — the link network was authored at write time, so
        graph order is final; these items are exempt from the LLM rerank pool.
        """
        if not query:
            return []
        try:
            from app.memory.relation_graph import KNOWLEDGE_PAGE_DIRS
            from app.memory.wiki_retrieval import DEFAULT_WIKI_METHOD, search_wiki_pages

            hits = search_wiki_pages(
                self.data_root,
                agent_id,
                query,
                method=DEFAULT_WIKI_METHOD,
                limit=limit,
                page_dirs=KNOWLEDGE_PAGE_DIRS,
            )
        except Exception as exc:
            logger.warning("[Retriever] knowledge-plane retrieval failed: %s", exc)
            return []

        items: list[MemoryItem] = []
        for index, hit in enumerate(hits):
            page_id = str(hit.get("page_id") or "")
            title = str(hit.get("title") or page_id.rsplit("/", 1)[-1].replace("-", " ").title())
            page_kind = str(hit.get("kind") or "knowledge")
            source_ref = str(hit.get("source_ref") or f"memory/{page_id}.md")
            preview = str(hit.get("preview") or "").strip()
            raw_score = float(hit.get("score") or 0.0)
            score = round(max(0.5, min(0.9, 0.5 + raw_score - (index * 0.02))), 4)
            items.append(
                MemoryItem(
                    kind=MemoryKind.SEMANTIC,
                    content=f"[{page_kind}:{title}] {preview}".strip(),
                    score=score,
                    source=source_ref,
                    metadata={
                        "page_id": page_id,
                        "title": title,
                        "page_kind": page_kind,
                        "source_ref": source_ref,
                        "source_type": "knowledge_ppr",
                        "method": str(hit.get("method") or "ppr"),
                        "sensitivity": "PL1_public",
                    },
                )
            )
        return items

    def _apply_activation(
        self,
        items: list[MemoryItem],
        context: ActivationContext,
        *,
        agent_id: uuid.UUID,
    ) -> list[MemoryItem]:
        from app.memory.access_log import bump_access

        scorer = ActivationScorer()
        activated: list[MemoryItem] = []
        for item in items:
            decision = scorer.score(item, context)
            if decision.suppressed:
                continue
            entry_id = item.metadata.get("entry_id")
            if entry_id and item.source:
                try:
                    bump_access(
                        self.data_root,
                        agent_id,
                        file_relpath=item.source,
                        entry_id=str(entry_id),
                    )
                except (OSError, ValueError) as exc:
                    logger.debug("[retriever] access bump failed for %s: %s", entry_id, exc)
            metadata = {
                **item.metadata,
                "activation_score": decision.score,
                "activation_reasons": decision.reasons,
            }
            activated.append(
                MemoryItem(
                    kind=item.kind,
                    content=item.content,
                    score=decision.score,
                    source=item.source,
                    metadata=metadata,
                )
            )
        return sorted(activated, key=lambda item: item.score, reverse=True)

    def _retrieve_explicit_overlay(self, agent_id: uuid.UUID, *, query: str = "") -> list[MemoryItem]:
        items: list[MemoryItem] = []
        for fact in search_explicit_overlay_entries(self.data_root, agent_id, query, limit=8):
            metadata = {
                **(fact.get("metadata") or {}),
                "entry_id": fact.get("id", ""),
                "category": fact.get("category", "general"),
                "target_hint": fact.get("target_hint", "unknown"),
                "source_type": "explicit_overlay",
                "sensitivity": fact.get("sensitivity", "PL1_public"),
                "timestamp": fact.get("timestamp", ""),
            }
            items.append(
                MemoryItem(
                    kind=MemoryKind.SEMANTIC,
                    content=f"[explicit_overlay][{metadata['category']}] {fact.get('content', '')}",
                    score=0.98,
                    source=str(fact.get("source", "")),
                    metadata=metadata,
                )
            )
        return items

    # -- Episodic layer: session summaries from DB --

    async def _retrieve_episodic(
        self,
        agent_id: uuid.UUID,
        session_id: str | None,
        *,
        previous_limit: int = 3,
    ) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        try:
            from app.database import tenant_scoped_session
            from app.models.chat_session import ChatSession
            from app.services.tenant_resolver import resolve_tenant_for_agent
            from sqlalchemy import select

            session_uuid = _parse_session_uuid(session_id)

            # Retriever may run inside a daemon/background path with no request
            # GUC. Resolve the owning tenant so these chat_sessions summary reads
            # survive the stage-3 non-owner role flip (a bare session fail-closes
            # → empty episodic memory).
            tid = await resolve_tenant_for_agent(agent_id)
            async with tenant_scoped_session(tid) as db:
                # Current session summary
                if session_uuid:
                    result = await db.execute(
                        select(ChatSession.summary, ChatSession.id).where(
                            ChatSession.id == session_uuid,
                            ChatSession.summary.isnot(None),
                            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
                        )
                    )
                    row = result.first()
                    if row and row[0]:
                        items.append(
                            MemoryItem(
                                kind=MemoryKind.EPISODIC,
                                content=row[0],
                                score=1.0,
                                source="current_session",
                                metadata={"session_id": str(row[1]), "is_current_session": True},
                            )
                        )

                # Previous session summaries — load a bounded continuity window
                prev_query = (
                    select(ChatSession.summary, ChatSession.id, ChatSession.last_message_at)
                    .where(
                        (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
                        ChatSession.summary.isnot(None),
                    )
                    .order_by(ChatSession.last_message_at.desc(), ChatSession.created_at.desc())
                    .limit(previous_limit)
                )
                if session_uuid:
                    prev_query = prev_query.where(ChatSession.id != session_uuid)
                result = await db.execute(prev_query)
                rows = result.all()
                for i, row in enumerate(rows):
                    if row[0]:
                        # Score decays: 0.8 → 0.6 → 0.4 for older sessions
                        score = max(0.8 - i * 0.2, 0.3)
                        _last_msg_at = row[2]
                        items.append(
                            MemoryItem(
                                kind=MemoryKind.EPISODIC,
                                content=row[0],
                                score=score,
                                source=f"previous_session_{i + 1}",
                                metadata={
                                    "session_id": str(row[1]),
                                    "timestamp": _last_msg_at.isoformat() if _last_msg_at else None,
                                },
                            )
                        )

        except Exception as exc:
            logger.warning("Episodic retrieval failed: %s", exc)

        # Deduplicate episodic items with similar content
        if len(items) > 1:
            unique: list[MemoryItem] = [items[0]]
            for item in items[1:]:
                if not any(_content_similar(item.content, u.content) for u in unique):
                    unique.append(item)
            items = unique

        return items

    # -- Optional enhancement layer: currently no external memory program --

    async def _retrieve_semantic_backend(
        self,
        agent_id: uuid.UUID,
        query: str,
        tenant_id: str | None,
        *,
        limit: int = 5,
    ) -> list[MemoryItem]:
        """Compatibility hook for future optional memory enhancement adapters.

        Native T3 Markdown already covers durable semantic memory. With no
        enhancement program configured, this hook must remain empty.
        """
        del agent_id, query, tenant_id, limit
        return []

    # -- External knowledge layer: kept outside native memory --

    async def _retrieve_external(
        self,
        agent_id: uuid.UUID,
        query: str,
        tenant_id: str | None,
        *,
        limit: int = 5,
        activation_context: ActivationContext | None = None,
    ) -> list[MemoryItem]:
        del agent_id, query, tenant_id, limit, activation_context
        return []


def _parse_session_uuid(session_id: str | None) -> uuid.UUID | None:
    if not session_id:
        return None
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, TypeError) as exc:
        logger.debug("Invalid session UUID %s: %s", session_id, exc)
        return None


def _activation_current_user_id(activation_context: ActivationContext | None) -> str | None:
    if activation_context is None:
        return None
    principal = getattr(activation_context.principal_stack, "current_user", None)
    principal_id = getattr(principal, "id", None)
    if principal_id is None:
        return None
    value = str(principal_id).strip()
    return value or None
