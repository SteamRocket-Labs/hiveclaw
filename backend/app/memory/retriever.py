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
from typing import Any

from app.memory.activation import ActivationContext, ActivationScorer
from app.memory.md_store import extract_entry_lines
from app.memory.types import MemoryItem, MemoryKind
from app.runtime.context_budget import ContextBudget

# Rerank: only trigger LLM side-query when semantic candidates exceed this count.
_RERANK_THRESHOLD = 5
_RERANK_MAX_SELECT = 5

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

    manifest_lines = [str(i) + ": " + item.content[:150] for i, item in enumerate(items)]
    manifest = "\n".join(manifest_lines)
    system_prompt = (
        "<role>\n"
        "You are a memory reranker. Given a user query and a numbered list of\n"
        "candidate semantic memory items, select the indices that will best\n"
        "help the caller answer the query. You do NOT summarize, explain, or\n"
        "modify the memories — you only pick.\n"
        "</role>\n\n"
        "<selection_criteria>\n"
        "- Relevance to the literal query intent comes first.\n"
        "- Prefer items with concrete artifacts (file paths, decisions, errors,\n"
        "  user preferences) over abstract or generic statements.\n"
        "- Skip items that merely restate a well-known fact already covered by\n"
        "  a higher-ranked item.\n"
        "- When items are near-duplicates, keep ONE (the one with richer\n"
        "  evidence) and drop the rest.\n"
        "</selection_criteria>\n\n"
        "<anti_patterns>\n"
        "- Do NOT invent new memory content. You can only return indices that\n"
        "  appear in the numbered list.\n"
        "- Do NOT return indices outside [0, len(items) - 1].\n"
        "- Do NOT pad the selection to hit the cap — return fewer indices if\n"
        "  fewer are relevant. An empty selection is valid if nothing fits.\n"
        "- Do NOT wrap the JSON in markdown fences or add prose outside it.\n"
        "</anti_patterns>\n\n"
        "<output_contract>\n"
        "Respond with a single raw JSON object, no fences, no prose:\n"
        '  {"selected": [<int>, <int>, ...]}\n'
        "Indices are 0-based. Order by descending relevance (most useful first).\n"
        "</output_contract>"
    )
    user_prompt = (
        "<query>" + query + "</query>\n\n"
        "<candidate_memories>\n"
        "Format: `<index>: <content preview, truncated at 150 chars>`\n\n" + manifest + "\n</candidate_memories>\n\n"
        "<task>\n"
        "Select up to " + str(max_select) + " memory indices most useful for the query above.\n"
        "Return only the JSON object defined in <output_contract>.\n"
        "</task>"
    )
    try:
        client = create_llm_client(**model_config)
        response = await client.stream(
            messages=[
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=user_prompt),
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

    def __init__(self, *, data_root: Path, use_t3_index_first: bool = False) -> None:
        self.data_root = Path(data_root)
        self.use_t3_index_first = use_t3_index_first

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
        """Retrieve memory items from all four layers.

        Args:
            rerank_model_config: When provided and semantic candidates > _RERANK_THRESHOLD,
                use a cheap LLM side-query to re-score semantic items by relevance.
                Dict with keys: provider, api_key, model, base_url (for create_llm_client).
        """
        items: list[MemoryItem] = []
        items.extend(self._retrieve_working(agent_id) or [])
        if self.use_t3_index_first:
            items.extend(self._retrieve_t3_index_first(agent_id, query=query) or [])
        else:
            items.extend(self._retrieve_t3_direct(agent_id, query=query) or [])
        items.extend(self._retrieve_understandings(agent_id, query=query) or [])
        episodic_limit = retrieval_profile.episodic_limit if retrieval_profile else 3
        external_limit = retrieval_profile.external_limit if retrieval_profile else 5
        semantic_limit = retrieval_profile.semantic_limit if retrieval_profile else 5
        del limit  # prompt memory is sourced entirely from T3 markdown files.

        items.extend(await self._retrieve_episodic(agent_id, session_id, previous_limit=episodic_limit) or [])

        # T3 markdown is the only long-term memory source injected into the prompt.
        # Other retrieval paths (search_memory tool, session_recall service) run
        # independently and must not merge into prompt assembly — md-first only,
        # to avoid dual-source drift.

        items.extend(
            await self._retrieve_semantic_backend(
                agent_id,
                query,
                tenant_id,
                limit=semantic_limit,
            )
            or []
        )
        items.extend(await self._retrieve_external(agent_id, query, tenant_id, limit=external_limit) or [])
        if activation_context:
            return self._apply_activation(items, activation_context, agent_id=agent_id)
        return items

    def _retrieve_understandings(self, agent_id: uuid.UUID, *, query: str = "") -> list[MemoryItem]:
        """Read relationship-shaped understandings as semantic candidates.

        `understandings.md` is not a replacement for T3. It is a first-class
        relationship graph projection that can now compete in the same
        activation path as semantic facts.
        """
        try:
            from app.memory.understanding_store import UnderstandingStore
        except ImportError:
            return []

        store = UnderstandingStore(self.data_root / str(agent_id) / "memory")
        items: list[MemoryItem] = []
        for entry in store.query():
            rendered = (
                f"[understanding] {entry.subject} -[{entry.relation_type}]-> {entry.object_}: "
                f"{entry.current_understanding}"
            )
            relevance_text = " ".join(
                [
                    entry.subject,
                    entry.object_,
                    entry.relation_type,
                    entry.current_understanding,
                    " ".join(entry.evidence_refs),
                    " ".join(entry.boundaries),
                    " ".join(entry.open_questions),
                ]
            )
            relevance = _score_relevance(relevance_text, query) if query else 1.0
            if query and relevance <= 0:
                continue
            confidence = max(0.0, min(float(entry.confidence), 1.0))
            score = round(min(1.0, 0.55 + (0.3 * relevance) + (0.15 * confidence)), 4)
            items.append(
                MemoryItem(
                    kind=MemoryKind.SEMANTIC,
                    content=rendered,
                    score=score,
                    source="memory/understandings.md",
                    metadata={
                        "entry_id": entry.entry_id,
                        "category": "understanding",
                        "source_type": "understanding_store",
                        "subject": entry.subject,
                        "object": entry.object_,
                        "relation_type": entry.relation_type,
                        "confidence": str(entry.confidence),
                        "evidence_refs": ",".join(entry.evidence_refs),
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

    # -- Objective projection layer: focus.md compatibility projection --

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

    # P0 files are always loaded at full score (user corrections and failure
    # patterns must never be dropped by query-aware filtering).
    # P1/P2 files are scored per-entry by relevance to the current query.
    _T3_FILES: list[tuple[str, str, float, bool]] = [
        #  (path, category, base_score, is_p0)
        ("memory/feedback.md", "feedback", 0.95, True),  # P0
        ("memory/blocked.md", "blocked_pattern", 0.95, True),  # P0
        ("memory/knowledge.md", "knowledge", 0.80, False),  # P1
        ("memory/strategies.md", "strategy", 0.80, False),  # P1
        ("memory/user.md", "user", 0.70, False),  # P2
    ]

    def _retrieve_t3_direct(self, agent_id: uuid.UUID, *, query: str = "") -> list[MemoryItem]:
        """Read T3 memory/*.md files — per-entry granularity with query-aware scoring.

        P0 entries (feedback + blocked) are always included at full score.
        P1/P2 entries are scored by relevance to the current user query so
        only the most useful knowledge/strategy/user facts occupy prompt space.

        Previously this emitted one MemoryItem per file (all bullets concatenated).
        Now each bullet is a separate MemoryItem, enabling the assembler's
        budget trimming to drop individual low-relevance entries instead of
        losing an entire file.
        """
        from app.memory.md_store import parse_entry_record

        ws = self.data_root / str(agent_id)
        items: list[MemoryItem] = []

        for rel_path, category, base_score, is_p0 in self._T3_FILES:
            fpath = ws / rel_path
            try:
                content = fpath.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError):
                continue

            if not content:
                continue

            lines = extract_entry_lines(content)
            if not lines:
                continue

            for line in lines:
                record = parse_entry_record(line)
                entry_content = record.content
                if not entry_content:
                    continue

                if is_p0 or not query:
                    # P0 entries always at full base_score; no query = load all
                    score = base_score
                else:
                    # P1/P2: score by query relevance × base priority
                    relevance = _score_relevance(entry_content, query)
                    # Minimum floor of 0.15 so even low-relevance entries can
                    # survive if budget allows — prevents total loss of context
                    score = base_score * max(relevance, 0.15)

                metadata: dict[str, Any] = {
                    **record.metadata,
                    "category": category,
                    "source_type": "t3_direct",
                }
                if record.timestamp:
                    metadata["timestamp"] = record.timestamp

                items.append(
                    MemoryItem(
                        kind=MemoryKind.SEMANTIC,
                        content=f"[{category}] {entry_content}",
                        score=round(score, 4),
                        source=rel_path,
                        metadata=metadata,
                    )
                )

        return items

    def _retrieve_t3_index_first(self, agent_id: uuid.UUID, *, query: str = "") -> list[MemoryItem]:
        """P0 direct + P1/P2 index-guided retrieval.

        This is intentionally a separate path so callers can run shadow
        comparisons before switching production retrieval.
        """
        from app.memory.md_store import parse_entry_record

        ws = self.data_root / str(agent_id)
        index_text = ""
        try:
            index_text = (ws / "memory" / "INDEX.md").read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            index_text = ""
        index_lower = index_text.lower()

        items: list[MemoryItem] = []
        for rel_path, category, base_score, is_p0 in self._T3_FILES:
            filename = rel_path.split("/")[-1]
            if not is_p0 and query and filename.lower() not in index_lower:
                continue
            fpath = ws / rel_path
            try:
                content = fpath.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError):
                continue
            if not content:
                continue
            for line in extract_entry_lines(content):
                record = parse_entry_record(line)
                entry_content = record.content
                if not entry_content:
                    continue
                if is_p0:
                    score = base_score
                else:
                    relevance = _score_relevance(f"{filename} {entry_content} {index_text}", query)
                    if query and relevance <= 0:
                        continue
                    score = base_score * max(relevance, 0.15)
                metadata: dict[str, Any] = {
                    **record.metadata,
                    "category": category,
                    "source_type": "t3_index_first",
                }
                if record.timestamp:
                    metadata["timestamp"] = record.timestamp
                items.append(
                    MemoryItem(
                        kind=MemoryKind.SEMANTIC,
                        content=f"[{category}] {entry_content}",
                        score=round(score, 4),
                        source=rel_path,
                        metadata=metadata,
                    )
                )
        return items

    def retrieve_t3_index_shadow(self, agent_id: uuid.UUID, *, query: str = "") -> dict[str, Any]:
        direct = self._retrieve_t3_direct(agent_id, query=query)
        index_first = self._retrieve_t3_index_first(agent_id, query=query)
        direct_p0 = {item.content for item in direct if item.source in {"memory/feedback.md", "memory/blocked.md"}}
        index_p0 = {item.content for item in index_first if item.source in {"memory/feedback.md", "memory/blocked.md"}}
        direct_p1p2 = {
            item.content for item in direct if item.source not in {"memory/feedback.md", "memory/blocked.md"}
        }
        index_p1p2 = {
            item.content for item in index_first if item.source not in {"memory/feedback.md", "memory/blocked.md"}
        }
        overlap = len(direct_p1p2 & index_p1p2)
        miss_count = max(0, len(direct_p1p2 - index_p1p2))
        return {
            "query": query,
            "direct_count": len(direct),
            "index_count": len(index_first),
            "p0_preserved": direct_p0 <= index_p0,
            "p1_p2_overlap": overlap,
            "p1_p2_direct_count": len(direct_p1p2),
            "p1_p2_index_count": len(index_p1p2),
            "p1_p2_miss_count": miss_count,
            "p1_p2_miss_rate": round(miss_count / len(direct_p1p2), 4) if direct_p1p2 else 0.0,
        }

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

    # -- Semantic backend layer: optional Hindsight read-side accelerator --

    async def _retrieve_semantic_backend(
        self,
        agent_id: uuid.UUID,
        query: str,
        tenant_id: str | None,
        *,
        limit: int = 5,
    ) -> list[MemoryItem]:
        """If tenant has a non-MD MemoryBackend configured, augment T3 direct reads
        with ranked results from that backend (e.g. Hindsight's TEMPR fusion).

        The T3 direct layer already covers every bullet; this layer adds ranking
        signal by producing items at higher base scores for semantically relevant
        matches. The assembler dedupes near-identical content so overlap is safe.

        Any failure / MD backend / missing tenant → returns [] silently.
        """
        if not query or not tenant_id:
            return []
        try:
            tenant_uuid = uuid.UUID(tenant_id)
        except (ValueError, TypeError):
            return []

        try:
            from app.memory.backend import MDBackend, get_memory_backend
            from app.memory.hindsight_sync import (
                LOOKUP_FAILED,
                _fetch_tenant_backend_pref,
            )

            pref = await _fetch_tenant_backend_pref(tenant_uuid)
            if pref is LOOKUP_FAILED:
                return []  # fail-closed: unknown tenant state → skip external backend
            backend = get_memory_backend(tenant_id=tenant_uuid, tenant_backend_pref=pref)
            if isinstance(backend, MDBackend):
                return []  # MD path already covered by _retrieve_t3_direct

            scored = await backend.search(agent_id, query, limit=limit)
        except Exception as exc:
            logger.debug("[Retriever] semantic backend failed: %s", exc)
            return []

        items: list[MemoryItem] = []
        for sm in scored:
            content = (sm.content or "").strip()
            if not content:
                continue
            items.append(
                MemoryItem(
                    kind=MemoryKind.SEMANTIC,
                    content=f"[{sm.category}] {content}",
                    score=float(sm.score or 0.5),
                    source="hindsight",
                    metadata={
                        "category": sm.category,
                        "timestamp": sm.timestamp,
                        "source_type": "memory_backend",
                        **(sm.metadata or {}),
                    },
                )
            )
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
