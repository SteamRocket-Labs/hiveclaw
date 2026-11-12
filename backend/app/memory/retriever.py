"""Authorized memory candidate gathering for the deterministic recall eval.

Live prompt assembly no longer runs a pre-selection model call: the resident
memory and the explicit-memory index enter the prompt directly and the
conversation model loads bodies on demand through ``search_memory`` /
``load_memory``. This module keeps :meth:`MemoryRetriever._gather_authorized_candidates`
— the complete four-layer candidate walk with activation scoring — as the seam
the offline recall eval (``app.evals.memory_recall_eval``) measures.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.memory.activation import ActivationContext, ActivationScorer
from app.memory.explicit_overlay import search_explicit_overlay_entries
from app.memory.t2.read_model import load_t2_package_snapshots, render_t2_package_snapshots
from app.memory.types import MemoryItem, MemoryKind

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """Four-layer memory retrieval pipeline.

    Each layer maps to a MemoryKind and retrieves items independently.
    The retriever works without a database connection (for testability);
    DB-dependent layers degrade gracefully with logging.
    """

    def __init__(self, *, data_root: Path) -> None:
        self.data_root = Path(data_root)

    async def _gather_authorized_candidates(
        self,
        agent_id: uuid.UUID,
        query: str,
        session_id: str | None,
        tenant_id: str | None,
        *,
        activation_context: ActivationContext | None = None,
    ) -> list[MemoryItem]:
        """Return complete authorized evidence with deterministic ranking.

        This seam exists for the deterministic offline recall eval; the live
        prompt path no longer consults it.
        """

        items: list[MemoryItem] = []
        items.extend(self._retrieve_explicit_overlay(agent_id, query=query) or [])
        items.extend(self._retrieve_knowledge_pages(agent_id, query=query) or [])
        items.extend(await self._retrieve_episodic(agent_id, session_id) or [])
        items.extend(
            await self._retrieve_semantic_backend(
                agent_id,
                query,
                tenant_id,
            )
            or []
        )
        items.extend(
            await self._retrieve_external(
                agent_id,
                query,
                tenant_id,
                activation_context=activation_context,
            )
            or []
        )

        if activation_context:
            items = self._apply_activation(items, activation_context, agent_id=agent_id)
        return items

    def _retrieve_knowledge_pages(
        self, agent_id: uuid.UUID, *, query: str = "", limit: int | None = None
    ) -> list[MemoryItem]:
        """Expose complete active knowledge pages with PPR/BM25 as evidence."""
        del limit  # compatibility-only; the eval must see every page
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
                limit=None,
                page_dirs=KNOWLEDGE_PAGE_DIRS,
            )
        except Exception as exc:
            logger.warning("[Retriever] knowledge-plane retrieval failed: %s", exc)
            return []

        items: list[MemoryItem] = []
        for hit in hits:
            page_id = str(hit.get("page_id") or "")
            title = str(hit.get("title") or page_id.rsplit("/", 1)[-1].replace("-", " ").title())
            page_kind = str(hit.get("kind") or "knowledge")
            source_ref = str(hit.get("source_ref") or f"memory/{page_id}.md")
            content = str(hit.get("content") or "").strip()
            raw_score = float(hit.get("score") or 0.0)
            items.append(
                MemoryItem(
                    kind=MemoryKind.SEMANTIC,
                    content=f"[{page_kind}:{title}]\n{content}".strip(),
                    score=max(0.0, min(1.0, raw_score)),
                    source=source_ref,
                    metadata={
                        "page_id": page_id,
                        "entry_id": page_id,
                        "title": title,
                        "description": str(hit.get("preview") or title),
                        "page_kind": page_kind,
                        "source_ref": source_ref,
                        "source_type": "knowledge_ppr",
                        "method": str(hit.get("method") or "ppr"),
                        "retrieval_score": raw_score,
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

        items = self._join_access_telemetry(items, agent_id=agent_id)
        items = self._join_context_boost(items, agent_id=agent_id, working_set=context.working_set)
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
                        create_if_missing=True,
                    )
                except (OSError, ValueError) as exc:
                    logger.debug("[retriever] access bump failed for %s: %s", entry_id, exc)
            metadata = {
                **item.metadata,
                "activation_score": decision.score,
                "activation_raw_score": decision.raw_score,
                "activation_reasons": decision.reasons,
                "activation_score_trace": decision.score_trace,
            }
            activated.append(
                replace(item, score=decision.score, metadata=metadata)
            )
        # Rank on the unclamped score: the display score saturates at 1.0,
        # which would erase ordering between highly-relevant items.
        return sorted(
            activated, key=lambda item: float(item.metadata.get("activation_raw_score") or item.score), reverse=True
        )

    def _join_context_boost(
        self,
        items: list[MemoryItem],
        *,
        agent_id: uuid.UUID,
        working_set: tuple[tuple[str, float], ...],
    ) -> list[MemoryItem]:
        """ContextBoost (design §4.2): diffuse session working-set strength
        over the relation graph and stamp each item's boost factor.

        The working set's strongest members seed Personalized PageRank on the
        knowledge/milestones link network, so pages graph-adjacent to what the
        session already touched warm up even with zero query-term overlap.
        Refs outside the graph (explicit entries, episodic) self-boost only.
        """
        if not items or not working_set:
            return items
        try:
            from app.memory.relation_graph import KNOWLEDGE_PAGE_DIRS, build_relation_graph, personalized_pagerank

            graph = build_relation_graph(self.data_root, agent_id, page_dirs=KNOWLEDGE_PAGE_DIRS)
            adjacency = graph.adjacency()
            seeds = {ref: max(0.0, strength) for ref, strength in working_set if ref in adjacency}
            diffusion = personalized_pagerank(adjacency, seeds) if seeds else {}
            # Normalize against the hottest NON-seed node: seeds dominate raw
            # PPR mass, which would crush every neighbor toward zero. Working
            # set members are directly hot at their own strength anyway.
            spread = {node: score for node, score in diffusion.items() if node not in seeds and score > 0}
            peak = max(spread.values(), default=0.0)
            boost: dict[str, float] = (
                {node: min(1.0, score / peak) for node, score in spread.items()} if peak > 0 else {}
            )
            for ref, strength in working_set:
                if strength > 0:
                    boost[ref] = max(boost.get(ref, 0.0), min(1.0, strength))
        except (OSError, ValueError) as exc:
            logger.warning("[retriever] context boost failed: %s", exc)
            return items
        if not boost:
            return items
        joined: list[MemoryItem] = []
        for item in items:
            ref = str(item.metadata.get("entry_id") or item.metadata.get("page_id") or "")
            factor = boost.get(ref, 0.0)
            if factor <= 0:
                joined.append(item)
                continue
            joined.append(
                replace(item, metadata={**item.metadata, "context_boost": round(factor, 6)})
            )
        return joined

    def _join_access_telemetry(self, items: list[MemoryItem], *, agent_id: uuid.UUID) -> list[MemoryItem]:
        """Join lifecycle-sidecar telemetry onto retrieved items (design §4.3).

        Closes the write-only half-loop: ``bump_access`` has always written the
        sidecar; this read-side join is what lets BaseLevel actually see
        frequency, recency, and feedback credit. Joins strictly before this
        turn's own bumps so one retrieval scores against the pre-turn state.
        """
        if not items:
            return items
        try:
            from app.memory.lifecycle_store import read_access_telemetry

            telemetry = read_access_telemetry(self.data_root, agent_id)
        except (OSError, ValueError) as exc:
            logger.warning("[retriever] telemetry join failed: %s", exc)
            return items
        if not telemetry:
            return items
        joined: list[MemoryItem] = []
        for item in items:
            record = telemetry.get(str(item.metadata.get("entry_id") or ""))
            if not record:
                joined.append(item)
                continue
            joined.append(
                replace(item, metadata={**item.metadata, **record})
            )
        return joined

    def _retrieve_explicit_overlay(self, agent_id: uuid.UUID, *, query: str = "") -> list[MemoryItem]:
        items: list[MemoryItem] = []
        sources = {}

        def observed(entry_id, refs):
            sources[entry_id] = refs

        for fact in search_explicit_overlay_entries(self.data_root, agent_id, query, limit=None,
            source_observer=observed):
            metadata = {
                **(fact.get("metadata") or {}),
                "entry_id": fact.get("id", ""),
                "category": fact.get("category", "general"),
                "target_hint": fact.get("target_hint", "unknown"),
                "description": fact.get("target_hint")
                if str(fact.get("target_hint") or "").strip() not in {"", "unknown"}
                else fact.get("preview", ""),
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
                    source_refs=sources.get(fact.get("id"), ()),
                )
            )
        return items

    # -- Episodic layer: canonical T2 package read model --

    async def _retrieve_episodic(
        self,
        agent_id: uuid.UUID,
        session_id: str | None,
        *,
        previous_limit: int | None = None,
    ) -> list[MemoryItem]:
        del previous_limit  # compatibility-only; the eval ranks every package
        try:
            snapshots, _mtimes = load_t2_package_snapshots(self.data_root, agent_id, limit=None)
        except Exception as exc:
            logger.warning("Episodic retrieval failed: %s", exc)
            return []

        current_session_id = str(session_id or "").strip()
        current = [snapshot for snapshot in snapshots if snapshot.session_id == current_session_id]
        previous = [snapshot for snapshot in snapshots if snapshot.session_id != current_session_id]
        items: list[MemoryItem] = []
        previous_index = 0
        for snapshot in [*current, *previous]:
            is_current = bool(current_session_id) and snapshot.session_id == current_session_id
            if is_current:
                score = 1.0
            else:
                score = max(0.8 - previous_index * 0.2, 0.3)
                previous_index += 1
            items.append(
                MemoryItem(
                    kind=MemoryKind.EPISODIC,
                    content=render_t2_package_snapshots([snapshot]),
                    score=score,
                    source=snapshot.rel_path,
                    metadata={
                        "session_id": snapshot.session_id,
                        "is_current_session": is_current,
                        "source_type": "t2_package",
                        "package_kind": snapshot.package_kind,
                        "package_status": snapshot.package_status,
                        "source_refs": list(snapshot.source_refs),
                        "timestamp": datetime.fromtimestamp(snapshot.updated_mtime, tz=UTC).isoformat(),
                    },
                )
            )

        return items

    # -- Optional enhancement layer: currently no external memory program --

    async def _retrieve_semantic_backend(
        self,
        agent_id: uuid.UUID,
        query: str,
        tenant_id: str | None,
        *,
        limit: int | None = None,
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
        limit: int | None = None,
        activation_context: ActivationContext | None = None,
    ) -> list[MemoryItem]:
        del agent_id, query, tenant_id, limit, activation_context
        return []
