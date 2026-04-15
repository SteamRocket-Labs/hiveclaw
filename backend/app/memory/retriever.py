"""Four-layer memory retrieval pipeline.

Retrieves memory items from working, episodic, semantic, and external layers,
returning a unified list of MemoryItem objects for the assembler.
"""

from __future__ import annotations

import json
import logging
import re as _re
import uuid
from pathlib import Path

from app.memory.md_store import extract_entry_lines
from app.memory.types import MemoryItem, MemoryKind
from app.runtime.context_budget import ContextBudget

# Rerank: only trigger LLM side-query when semantic candidates exceed this count.
_RERANK_THRESHOLD = 5
_RERANK_MAX_SELECT = 5

logger = logging.getLogger(__name__)

_CJK_RE = _re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF]")
_PUNCTUATION_CHARS = frozenset("，。！？；：""''（）【】、…—《》·,.!?;:\"'()[]{}/ \t\n\r")


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


def _score_relevance(content: str, query: str) -> float:
    """Score content relevance using dual-path: word overlap (English) + char overlap (CJK)."""
    q_lower = query.lower()
    c_lower = content.lower()

    # Path 1: English word overlap
    query_words = set(q_lower.split())
    content_words = set(c_lower.split())
    word_overlap = len(query_words & content_words) / max(len(query_words), 1)

    # Path 2: CJK character overlap (only if query or content has CJK)
    char_overlap = 0.0
    if _has_cjk(query) or _has_cjk(content):
        query_chars = _chars_set(query)
        content_chars = _chars_set(content)
        if query_chars:
            char_overlap = len(query_chars & content_chars) / len(query_chars)

    return max(word_overlap, char_overlap)


async def _rerank_semantic_items(
    items: list[MemoryItem],
    query: str,
    model_config: dict | None = None,
    *,
    max_select: int = _RERANK_MAX_SELECT,
) -> list[MemoryItem]:
    """Use a cheap LLM side-query to select the most relevant semantic memories.

    Returns up to _RERANK_MAX_SELECT items, preserving original MemoryItem objects.
    Falls back to the original list on any error.

    Args:
        model_config: dict with keys provider/api_key/model/base_url for create_llm_client.
            If None, rerank is skipped (graceful degradation).
    """
    if not model_config:
        return items[:max_select]

    try:
        from app.services.llm_client import LLMMessage, create_llm_client
    except ImportError:
        return items[:max_select]

    manifest_lines = [
        str(i) + ": " + item.content[:150] for i, item in enumerate(items)
    ]
    manifest = "\n".join(manifest_lines)
    prompt_parts = [
        "Query: " + query,
        "",
        "Memories (index: content):",
        manifest,
        "",
        "Select up to " + str(max_select) + " memory indices most useful for this query. ",
        'Return JSON: {"selected": [0, 2, 4]}',
    ]
    prompt_text = "\n".join(prompt_parts)
    try:
        client = create_llm_client(**model_config)
        response = await client.stream(
            messages=[
                LLMMessage(role="system", content="Select the most relevant memories. Return only JSON."),
                LLMMessage(role="user", content=prompt_text),
            ],
            max_tokens=100,
            temperature=0.0,
        )
        content = response.content if hasattr(response, "content") else str(response)
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(content[start:end])
            indices = parsed.get("selected", [])
            if isinstance(indices, list) and indices:
                selected = [items[i] for i in indices if isinstance(i, int) and 0 <= i < len(items)]
                if selected:
                    logger.debug("[Retriever] Rerank selected %d/%d items", len(selected), len(items))
                    return selected
        if hasattr(client, "close"):
            await client.close()
    except Exception as exc:
        logger.debug("[Retriever] Rerank failed, using original order: %s", exc)

        return items[:max_select]


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
    ) -> list[MemoryItem]:
        """Retrieve memory items from all four layers.

        Args:
            rerank_model_config: When provided and semantic candidates > _RERANK_THRESHOLD,
                use a cheap LLM side-query to re-score semantic items by relevance.
                Dict with keys: provider, api_key, model, base_url (for create_llm_client).
        """
        items: list[MemoryItem] = []
        items.extend(self._retrieve_working(agent_id) or [])
        items.extend(self._retrieve_t3_direct(agent_id) or [])
        episodic_limit = retrieval_profile.episodic_limit if retrieval_profile else 3
        external_limit = retrieval_profile.external_limit if retrieval_profile else 5
        del limit  # prompt memory is sourced entirely from T3 markdown files.

        items.extend(await self._retrieve_episodic(agent_id, session_id, previous_limit=episodic_limit) or [])

        # T3 markdown is the only long-term memory source injected into the prompt.
        # search_memory/session recall may use separate shadow indexes, but prompt
        # assembly must stay md-first to avoid dual-source drift.

        items.extend(await self._retrieve_external(agent_id, query, tenant_id, limit=external_limit) or [])
        return items

    # -- Working layer: agent's focus.md --

    def _retrieve_working(self, agent_id: uuid.UUID) -> list[MemoryItem]:
        focus_file = self.data_root / str(agent_id) / "focus.md"
        # Atomic read: skip exists() check to avoid TOCTOU race — just try to read
        try:
            content = focus_file.read_text(encoding="utf-8").strip()
            if not content:
                return []
            return [MemoryItem(kind=MemoryKind.WORKING, content=content, score=1.0, source="focus.md")]
        except FileNotFoundError:
            return []
        except OSError:
            logger.debug("Failed to read focus.md for agent %s", agent_id)
            return []

    # -- T3 Direct layer: memory/*.md files (MD = Source of Truth) --

    # P0 files are always loaded; P1/P2 loaded with lower scores.
    _T3_FILES: list[tuple[str, str, float]] = [
        ("memory/feedback.md", "feedback", 0.95),     # P0: user corrections
        ("memory/blocked.md", "blocked_pattern", 0.95),  # P0: failed approaches
        ("memory/knowledge.md", "knowledge", 0.80),   # P1: project knowledge
        ("memory/strategies.md", "strategy", 0.80),   # P1: effective approaches
        ("memory/user.md", "user", 0.70),             # P2: user profile
    ]

    def _retrieve_t3_direct(self, agent_id: uuid.UUID) -> list[MemoryItem]:
        """Read T3 memory/*.md files directly — the MD source of truth.

        These files are written by heartbeat (T2→T3 curation) and refined
        by dream (dedup + soul promotion). Reading them directly ensures
        the agent always sees the latest curated knowledge.
        """
        ws = self.data_root / str(agent_id)
        items: list[MemoryItem] = []

        for rel_path, category, base_score in self._T3_FILES:
            fpath = ws / rel_path
            try:
                content = fpath.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError):
                continue

            if not content:
                continue

            # Only bullet entries count as curated memory. Template prose is ignored.
            lines = extract_entry_lines(content)
            if not lines:
                continue

            entry_text = "\n".join(lines)
            items.append(
                MemoryItem(
                    kind=MemoryKind.SEMANTIC,
                    content=f"[{category}]\n{entry_text}",
                    score=base_score,
                    source=rel_path,
                    metadata={"category": category, "source_type": "t3_direct"},
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
            from app.database import async_session
            from app.models.chat_session import ChatSession
            from sqlalchemy import select

            session_uuid = _parse_session_uuid(session_id)

            async with async_session() as db:
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

    # -- External layer: OpenViking recall --

    async def _retrieve_external(
        self,
        agent_id: uuid.UUID,
        query: str,
        tenant_id: str | None,
        *,
        limit: int = 5,
    ) -> list[MemoryItem]:
        if not query or not tenant_id:
            return []

        try:
            from app.services import viking_client

            if not viking_client.is_configured():
                return []

            results = await viking_client.find(
                query,
                tenant_id=tenant_id,
                agent_id=str(agent_id),
                limit=limit,
            )

            items: list[MemoryItem] = []
            for result in results:
                content = result.get("content", "")
                if not content:
                    continue
                items.append(
                    MemoryItem(
                        kind=MemoryKind.EXTERNAL,
                        content=content,
                        score=result.get("score", 0.5),
                        source="openviking",
                        metadata={"uri": result.get("uri", "")},
                    )
                )
            return items

        except Exception as exc:
            logger.warning("External retrieval failed: %s", exc)
            return []


def _parse_session_uuid(session_id: str | None) -> uuid.UUID | None:
    if not session_id:
        return None
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, TypeError) as exc:
        logger.debug("Invalid session UUID %s: %s", session_id, exc)
        return None
