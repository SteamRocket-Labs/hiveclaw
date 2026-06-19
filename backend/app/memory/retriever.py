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
from app.memory.md_store import build_t3_entry_manifest
from app.memory.t2_store import HIGH_PRIORITY_THRESHOLD, load_t2_entries
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
    agent_id: uuid.UUID | None = None,
    tenant_id: str | uuid.UUID | None = None,
    max_select: int = _RERANK_MAX_SELECT,
    timeout_seconds: float = 3.0,
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
        from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context
    except ImportError:
        return items[:max_select]

    # A3: 400-char previews — the reranker judges meaning, not headlines.
    manifest_lines = [str(i) + ": " + item.content[:400] for i, item in enumerate(items)]
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
    client = None
    try:
        client = create_llm_client_from_config(
            with_llm_usage_context(
                model_config,
                source="memory_rerank",
                agent_id=agent_id,
                tenant_id=tenant_id,
            )
        )
        response = await asyncio.wait_for(
            client.stream(
                messages=[
                    LLMMessage(role="system", content=system_prompt),
                    LLMMessage(role="user", content=user_prompt),
                ],
                max_tokens=100,
                temperature=0.0,
            ),
            timeout=timeout_seconds,
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
    except Exception as exc:
        # A3: degradation to mechanical order must be observable, not silent.
        logger.warning(
            "[Retriever] Rerank failed, using mechanical order: %s",
            exc,
            extra={"metric": "memory_rerank_fallback", "candidates": len(items)},
        )
        return items[:max_select]
    finally:
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception:
                logger.debug("[Retriever] Rerank client close failed", exc_info=True)


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
        items.extend(self._retrieve_explicit_overlay(agent_id, query=query) or [])
        if self.use_t3_index_first:
            items.extend(self._retrieve_t3_index_first(agent_id, query=query) or [])
        else:
            items.extend(self._retrieve_t3_direct(agent_id, query=query) or [])
        items.extend(self._retrieve_understandings(agent_id, query=query) or [])
        items.extend(self._retrieve_high_priority_t2(agent_id, query=query) or [])
        episodic_limit = retrieval_profile.episodic_limit if retrieval_profile else 3
        external_limit = retrieval_profile.external_limit if retrieval_profile else 5
        semantic_limit = retrieval_profile.semantic_limit if retrieval_profile else 5
        del limit  # prompt memory is sourced entirely from T3 markdown files.
        items.extend(self._retrieve_wiki_pages(agent_id, query=query, limit=semantic_limit) or [])

        items.extend(await self._retrieve_episodic(agent_id, session_id, previous_limit=episodic_limit) or [])

        # T3 markdown is the only long-term memory source injected into the prompt.
        # Other retrieval paths run independently and must not merge into prompt
        # assembly, avoiding dual-source drift.

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

        # A3 (docs/agent-lifecycle-cc-alignment.md 主题 A): semantic activation
        # gets an LLM pass. Keyword + fixed-weight scoring never reads content
        # meaning — when the semantic pool is contested (> threshold) and a
        # rerank model is available, the LLM picks; mechanical order remains
        # the observable fallback inside _rerank_semantic_items.
        if rerank_model_config and query:
            semantic_pool = [item for item in items if item.kind == MemoryKind.SEMANTIC]
            if len(semantic_pool) > _RERANK_THRESHOLD:
                reranked = await _rerank_semantic_items(
                    semantic_pool,
                    query,
                    rerank_model_config,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                )
                items = [item for item in items if item.kind != MemoryKind.SEMANTIC] + list(reranked)

        if activation_context:
            return self._apply_activation(items, activation_context, agent_id=agent_id)
        return items

    def _retrieve_high_priority_t2(self, agent_id: uuid.UUID, *, query: str = "") -> list[MemoryItem]:
        entries, _mtimes = load_t2_entries(self.data_root, agent_id)
        items: list[MemoryItem] = []
        for entry in entries:
            category = str(entry.get("category") or "").lower()
            if category not in {"feedback", "constraint"}:
                continue
            if str(entry.get("status") or "active").lower() not in {"", "active"}:
                continue
            weight = float(entry.get("weight") or 0.0)
            if weight < HIGH_PRIORITY_THRESHOLD:
                continue
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            relevance = _score_relevance(content, query) if query else 1.0
            if query and relevance <= 0:
                continue
            source_file = str(entry.get("file") or "insights.md")
            metadata = {
                "entry_id": str(entry.get("entry_id") or ""),
                "category": category,
                "source_type": "t2_high_priority",
                "lane": "t2_high_priority",
                "weight": str(weight),
                "sensitivity": str(entry.get("sensitivity") or "PL1_public"),
            }
            for key in ("confidence", "conf", "retention_score", "open_loop", "reaction", "polarity", "decision_ref"):
                value = entry.get(key)
                if value is not None and str(value).strip():
                    metadata[key] = str(value)
            items.append(
                MemoryItem(
                    kind=MemoryKind.SEMANTIC,
                    content=f"[t2:{category}] {content}",
                    score=round(min(1.0, max(weight, 0.75 + (0.2 * relevance))), 4),
                    source=f"memory/learnings/{source_file}",
                    metadata=metadata,
                )
            )
        return sorted(items, key=lambda item: item.score, reverse=True)[:5]

    def _retrieve_wiki_pages(self, agent_id: uuid.UUID, *, query: str = "", limit: int = 5) -> list[MemoryItem]:
        """Retrieve wiki/scene pages through the PPR-backed Markdown graph.

        Wiki and scene pages are T3/T9 Markdown truth sources, but they live
        under memory/wiki and memory/scenes rather than the legacy flat T3
        files. They must therefore join the main prompt-memory retrieval path
        explicitly; otherwise the PPR graph remains an offline experiment.
        """
        if not query:
            return []
        try:
            from app.memory.wiki_retrieval import DEFAULT_WIKI_METHOD, search_wiki_pages

            hits = search_wiki_pages(
                self.data_root,
                agent_id,
                query,
                method=DEFAULT_WIKI_METHOD,
                limit=limit,
            )
        except Exception as exc:
            logger.debug("[Retriever] wiki PPR retrieval failed: %s", exc)
            return []

        items: list[MemoryItem] = []
        for index, hit in enumerate(hits):
            page_id = str(hit.get("page_id") or "")
            title = str(hit.get("title") or page_id.rsplit("/", 1)[-1].replace("-", " ").title())
            page_kind = str(hit.get("kind") or "wiki")
            method = str(hit.get("method") or "ppr")
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
                        "source_type": f"wiki_{method}",
                        "method": method,
                        "sensitivity": "PL1_public",
                    },
                )
            )
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

    # -- T3 Direct layer: memory/*.md files (MD = Source of Truth) --

    # P0 files are always loaded at full score (user corrections and failure
    # patterns must never be dropped by query-aware filtering).
    # P1/P2 files are scored per-entry by relevance to the current query.
    _T3_FILES: list[tuple[str, str, float, bool]] = [
        #  (path, category, base_score, is_p0)
        ("memory/t3/user.md", "user", 0.95, True),  # P0 if relevant
        ("memory/t3/worker.md", "worker", 0.95, True),  # P0 if relevant
        ("memory/t3/episodes.md", "episode", 0.85, False),  # P1
        ("memory/t3/capabilities.md", "capability", 0.80, False),  # P1
    ]
    _T3_SCORE_BY_SOURCE = {
        rel_path: (category, base_score, is_p0) for rel_path, category, base_score, is_p0 in _T3_FILES
    }
    _P0_FULL_RECENT_LIMIT = 8
    _P1_P2_FULL_QUERY_LIMIT = 5

    @staticmethod
    def _index_entry_content(entry_id: str, category: str, timestamp: str, preview: str) -> str:
        date = timestamp[:10] if timestamp else "undated"
        return (
            f"Memory index entry id={entry_id} [{category}] ({date}) {preview} "
            f'- call load_memory(ids=["{entry_id}"]) before relying on the full fact.'
        )

    def _memory_item_from_entry(
        self,
        entry,
        *,
        score: float,
        source_type: str,
        indexed_only: bool,
        category: str | None = None,
    ) -> MemoryItem:
        display_category = category or entry.category
        metadata: dict[str, Any] = {
            **entry.metadata,
            "entry_id": entry.entry_id,
            "category": display_category,
            "source_type": source_type,
        }
        if indexed_only:
            metadata["indexed_only"] = "true"
        if entry.timestamp:
            metadata["timestamp"] = entry.timestamp
        content = (
            self._index_entry_content(entry.entry_id, display_category, entry.timestamp, entry.preview)
            if indexed_only
            else f"[{display_category}] {entry.content}"
        )
        return MemoryItem(
            kind=MemoryKind.SEMANTIC,
            content=content,
            score=round(score, 4),
            source=entry.source,
            metadata=metadata,
        )

    def _retrieve_explicit_overlay(self, agent_id: uuid.UUID, *, query: str = "") -> list[MemoryItem]:
        items: list[MemoryItem] = []
        for fact in search_explicit_overlay_entries(self.data_root, agent_id, query, limit=8):
            metadata = {
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
        items: list[MemoryItem] = []

        for entry in build_t3_entry_manifest(self.data_root, agent_id):
            if memory_lifecycle_suppression_reason(entry.metadata):
                continue
            score_spec = self._T3_SCORE_BY_SOURCE.get(entry.source)
            if not score_spec:
                continue
            category, base_score, is_p0 = score_spec
            if is_p0 or not query:
                score = base_score
            else:
                relevance = _score_relevance(entry.content, query)
                score = base_score * max(relevance, 0.15)
            items.append(
                self._memory_item_from_entry(
                    entry,
                    score=score,
                    source_type="t3_direct",
                    indexed_only=False,
                    category=category,
                )
            )

        return items

    def _retrieve_t3_index_first(self, agent_id: uuid.UUID, *, query: str = "") -> list[MemoryItem]:
        """P0 direct + P1/P2 index-guided retrieval.

        This is intentionally a separate path so callers can run shadow
        comparisons before switching production retrieval.
        """
        items: list[MemoryItem] = []
        entries = build_t3_entry_manifest(self.data_root, agent_id)
        if not entries:
            return []

        p0_entries = [entry for entry in entries if entry.is_p0]
        p0_full_ids = {entry.entry_id for entry in p0_entries[-self._P0_FULL_RECENT_LIMIT :]}

        p1_p2_ranked: list[tuple[float, str]] = []
        if query:
            for entry in entries:
                if entry.is_p0:
                    continue
                relevance = _score_relevance(
                    f"{entry.filename} {entry.category} {entry.preview} {entry.content}", query
                )
                if relevance > 0:
                    p1_p2_ranked.append((relevance, entry.entry_id))
            p1_p2_ranked.sort(reverse=True)
        p1_p2_full_ids = {entry_id for _relevance, entry_id in p1_p2_ranked[: self._P1_P2_FULL_QUERY_LIMIT]}

        for entry in entries:
            if memory_lifecycle_suppression_reason(entry.metadata):
                continue
            score_spec = self._T3_SCORE_BY_SOURCE.get(entry.source)
            if not score_spec:
                continue
            category, base_score, is_p0 = score_spec
            relevance = _score_relevance(f"{entry.filename} {entry.category} {entry.preview} {entry.content}", query)
            should_expand = (is_p0 and entry.entry_id in p0_full_ids) or (
                not is_p0 and entry.entry_id in p1_p2_full_ids
            )
            score = base_score if is_p0 else base_score * max(relevance, 0.15)
            if not should_expand:
                score *= 0.55
            items.append(
                self._memory_item_from_entry(
                    entry,
                    score=score,
                    source_type="t3_full_entry" if should_expand else "t3_index_entry",
                    indexed_only=not should_expand,
                    category=category,
                )
            )
        return items

    def retrieve_t3_index_shadow(self, agent_id: uuid.UUID, *, query: str = "") -> dict[str, Any]:
        direct = self._retrieve_t3_direct(agent_id, query=query)
        index_first = self._retrieve_t3_index_first(agent_id, query=query)
        direct_p0 = {
            item.metadata.get("entry_id") or item.content
            for item in direct
            if item.source in {"memory/t3/user.md", "memory/t3/worker.md"}
        }
        index_p0 = {
            item.metadata.get("entry_id") or item.content
            for item in index_first
            if item.source in {"memory/t3/user.md", "memory/t3/worker.md"}
        }
        direct_p1p2 = {
            item.metadata.get("entry_id") or item.content
            for item in direct
            if item.source not in {"memory/t3/user.md", "memory/t3/worker.md"}
        }
        index_p1p2 = {
            item.metadata.get("entry_id") or item.content
            for item in index_first
            if item.source not in {"memory/t3/user.md", "memory/t3/worker.md"}
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
