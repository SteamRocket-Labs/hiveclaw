"""Four-layer memory retrieval pipeline.

Retrieves memory items from working, episodic, semantic, and external layers,
returning a unified list of MemoryItem objects for the assembler.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path

from app.memory.activation import ActivationContext, ActivationScorer
from app.memory.explicit_overlay import search_explicit_overlay_entries
from app.memory.types import MemoryItem, MemoryKind
from app.runtime.context_budget import ContextBudget

logger = logging.getLogger(__name__)

MAX_AUTOMATIC_MEMORY_ITEMS = 5
MAX_SELECTION_DESCRIPTOR_BYTES = 512


def _bounded_descriptor_text(value: object, *, max_bytes: int = MAX_SELECTION_DESCRIPTOR_BYTES) -> str:
    compact = " ".join(str(value or "").split())
    encoded = compact.encode("utf-8")
    if len(encoded) <= max_bytes:
        return compact
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def _selection_descriptor(item: MemoryItem) -> dict[str, object]:
    """Return a bounded CC-style name/description manifest entry.

    The selector chooses files/pages from this index. It never receives every
    candidate body merely to decide which bodies should enter the main prompt.
    Full authorized bytes remain bound to the stable load ref and are read only
    for the at-most-five selected entries.
    """

    metadata = item.metadata
    candidate_id = str(metadata["selection_candidate_id"])
    entry_id = str(metadata.get("entry_id") or metadata.get("page_id") or "").strip()
    session_id = str(metadata.get("session_id") or "").strip()
    source_type = str(metadata.get("source_type") or item.kind.value)
    name = _bounded_descriptor_text(
        metadata.get("title")
        or metadata.get("target_hint")
        or (f"Session {session_id}" if session_id else "")
        or metadata.get("category")
        or item.source
        or candidate_id,
        max_bytes=256,
    )
    description = _bounded_descriptor_text(
        metadata.get("description") or metadata.get("preview") or metadata.get("summary") or item.content
    )
    return {
        "candidate_id": candidate_id,
        "kind": item.kind.value,
        "name": name,
        "description": description,
        "source_type": source_type,
        "load_ref": {
            "entry_id": entry_id or None,
            "session_id": session_id or None,
            "source": item.source,
        },
        "observations": {
            "score": item.score,
            "activation_reasons": metadata.get("activation_reasons") or [],
            "category": metadata.get("category"),
            "timestamp": metadata.get("timestamp"),
        },
    }


class MemoryRetriever:
    """Four-layer memory retrieval pipeline.

    Each layer maps to a MemoryKind and retrieves items independently.
    The retriever works without a database connection (for testability);
    DB-dependent layers degrade gracefully with logging.
    """

    def __init__(self, *, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.last_selection_status = "not_run"
        self.last_selection_error: str | None = None
        self.last_selection_receipt: str | None = None
        self.last_selection_coverage_path: str | None = None

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
        """Gather complete authorized evidence, then let the model select semantics.

        Mechanical scores, graph propagation, recency, and working-set heat are
        observable ranking evidence only. They never make the semantic
        selection. The model may choose at most five bodies for automatic
        surfacing; every other authorized item remains losslessly reachable via
        search/load refs and the persisted coverage receipt.
        """
        del limit, retrieval_profile  # compatibility-only; never semantic cutoffs
        items = await self._gather_authorized_candidates(
            agent_id,
            query,
            session_id,
            tenant_id,
            activation_context=activation_context,
        )
        return await self._select_candidates(
            items,
            agent_id=agent_id,
            tenant_id=tenant_id,
            query=query,
            model_config=rerank_model_config,
        )

    async def _gather_authorized_candidates(
        self,
        agent_id: uuid.UUID,
        query: str,
        session_id: str | None,
        tenant_id: str | None,
        *,
        activation_context: ActivationContext | None = None,
    ) -> list[MemoryItem]:
        """Return ranking evidence before model-owned automatic selection.

        This internal seam exists for the deterministic offline recall eval.
        Live prompt assembly must call :meth:`retrieve`, which adds the model
        selector and the ref-only failure contract.
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
        del limit  # compatibility-only; the runtime selector must see every page
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

    async def _select_candidates(
        self,
        items: list[MemoryItem],
        *,
        agent_id: uuid.UUID,
        tenant_id: str | None,
        query: str,
        model_config: dict | None,
    ) -> list[MemoryItem]:
        """Apply model-owned semantic selection with a ref-only failure path."""

        self.last_selection_error = None
        self.last_selection_receipt = None
        self.last_selection_coverage_path = None
        if not items:
            self.last_selection_status = "not_needed"
            return []

        prepared = self._attach_selection_candidate_ids(items)
        selection_run_id = uuid.uuid4().hex
        if not model_config:
            self.last_selection_status = "model_unavailable"
            self._record_selection_receipt(
                agent_id=agent_id,
                run_id=selection_run_id,
                candidates=prepared,
                selected=[],
                status="model_unavailable",
            )
            return []

        try:
            selected_ids, reason = await self._select_with_model(
                items=prepared,
                agent_id=agent_id,
                tenant_id=tenant_id,
                query=query,
                model_config=model_config,
                selection_run_id=selection_run_id,
            )
            if not isinstance(selected_ids, list) or any(not isinstance(value, str) for value in selected_ids):
                raise ValueError("memory selector selected_ids must be a string list")
            if len(selected_ids) > MAX_AUTOMATIC_MEMORY_ITEMS:
                raise ValueError(
                    f"memory selector may automatically surface at most {MAX_AUTOMATIC_MEMORY_ITEMS} items"
                )
            by_id = {str(item.metadata["selection_candidate_id"]): item for item in prepared}
            unknown = [candidate_id for candidate_id in selected_ids if candidate_id not in by_id]
            if unknown:
                raise ValueError(f"memory selector returned unknown candidate ids: {unknown}")
            chosen: list[MemoryItem] = []
            seen: set[str] = set()
            for candidate_id in selected_ids:
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                chosen.append(by_id[candidate_id])
            self.last_selection_status = "model_selected"
            selected = self._annotate_selection(chosen, status="model_selected", reason=reason)
            self._record_selection_receipt(
                agent_id=agent_id,
                run_id=selection_run_id,
                candidates=prepared,
                selected=selected,
                status="model_selected",
                reason=reason,
            )
            return selected
        except Exception as exc:  # model failure must never trigger mechanical semantic selection
            logger.warning("[Retriever] model semantic selection failed; preserving ref-only coverage: %s", exc)
            self.last_selection_status = "failed"
            self.last_selection_error = type(exc).__name__
            self._record_selection_receipt(
                agent_id=agent_id,
                run_id=selection_run_id,
                candidates=prepared,
                selected=[],
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return []

    @staticmethod
    def _attach_selection_candidate_ids(items: list[MemoryItem]) -> list[MemoryItem]:
        prepared: list[MemoryItem] = []
        for index, item in enumerate(items):
            source_type = str(item.metadata.get("source_type") or item.kind.value)
            locator = str(
                item.metadata.get("entry_id")
                or item.metadata.get("page_id")
                or item.metadata.get("session_id")
                or item.source
                or index
            )
            digest = hashlib.sha256(
                f"{item.kind.value}\0{source_type}\0{locator}\0{item.source}\0{item.content}".encode("utf-8")
            ).hexdigest()[:20]
            candidate_id = f"memory_candidate:{item.kind.value}:{index}:{digest}"
            prepared.append(
                MemoryItem(
                    kind=item.kind,
                    content=item.content,
                    score=item.score,
                    source=item.source,
                    metadata={**item.metadata, "selection_candidate_id": candidate_id},
                )
            )
        return prepared

    @staticmethod
    def _annotate_selection(
        items: list[MemoryItem],
        *,
        status: str,
        reason: str = "",
        error: str = "",
    ) -> list[MemoryItem]:
        annotated: list[MemoryItem] = []
        for item in items:
            metadata = {**item.metadata, "semantic_selection_status": status}
            if reason:
                metadata["semantic_selection_reason"] = reason
            if error:
                metadata["semantic_selection_error"] = error
            annotated.append(
                MemoryItem(
                    kind=item.kind,
                    content=item.content,
                    score=item.score,
                    source=item.source,
                    metadata=metadata,
                )
            )
        return annotated

    async def _select_with_model(
        self,
        *,
        items: list[MemoryItem],
        agent_id: uuid.UUID,
        tenant_id: str | None,
        query: str,
        model_config: dict,
        selection_run_id: str,
    ) -> tuple[list[str], str]:
        from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context
        from app.services.semantic_input_coverage import prepare_covered_semantic_input

        configured = with_llm_usage_context(
            model_config,
            source="memory_semantic_selection",
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
        client = create_llm_client_from_config(configured)
        try:
            try:
                max_input_tokens = int(model_config.get("max_input_tokens") or 128_000)
            except (TypeError, ValueError):
                max_input_tokens = 128_000
            max_chars = max(max_input_tokens - 16_000, 8_000) * 3
            try:
                output_tokens = int(model_config.get("max_output_tokens") or 32_768)
            except (TypeError, ValueError):
                output_tokens = 32_768

            sections: list[tuple[str, str]] = []
            for item in items:
                descriptor = _selection_descriptor(item)
                candidate_id = str(descriptor["candidate_id"])
                sections.append((candidate_id, json.dumps(descriptor, ensure_ascii=False, indent=2, default=str)))

            async def review_chunk(_phase: str, prompt: str) -> str:
                response = await client.complete(
                    messages=[
                        LLMMessage(
                            role="system",
                            content=(
                                "Preserve every memory candidate ID, bounded description, load ref, provenance, "
                                "and observable activation signal for the final semantic selector. "
                                "Scores, order, recency, and graph signals are observations only."
                            ),
                        ),
                        LLMMessage(role="user", content=prompt),
                    ],
                    temperature=0.1,
                    max_tokens=output_tokens,
                )
                return str(response.content or "").strip()

            coverage_path = (
                self.data_root
                / str(agent_id)
                / "memory"
                / "control"
                / "retrieval_reviews"
                / f"{selection_run_id}.coverage.json"
            )
            self.last_selection_coverage_path = coverage_path.as_posix()
            covered = await prepare_covered_semantic_input(
                phase="memory_semantic_selection",
                sections=sections,
                max_chars=max_chars,
                coverage_path=coverage_path,
                review_chunk=review_chunk,
            )
            response = await client.complete(
                messages=[
                    LLMMessage(
                        role="system",
                        content=(
                            "You are the semantic authority for prompt-memory discovery. Review the complete "
                            "authorized memory index: candidate names, bounded descriptions, and load refs. Select "
                            "the candidates most likely to help answer the current query; select none when none is "
                            "useful. Select at most five candidate bodies for "
                            "automatic prompt surfacing. If more evidence may matter, choose the five best starting "
                            "points; the conversation model can continue with search_memory/load_memory. Do not "
                            "claim the index description is the full memory body. "
                            "Mechanical scores, order, recency, and graph signals are observations only. Return raw "
                            'JSON only: {"selected_ids":["memory_candidate:..."],"reason":"complete rationale"}'
                        ),
                    ),
                    LLMMessage(role="user", content=f"<current_query>\n{query}\n</current_query>\n\n{covered}"),
                ],
                temperature=0.1,
                max_tokens=output_tokens,
            )
            parsed = json.loads(str(response.content or "").strip())
            if not isinstance(parsed, dict):
                raise ValueError("memory selector response must be a JSON object")
            selected_ids = parsed.get("selected_ids")
            if not isinstance(selected_ids, list):
                raise ValueError("memory selector response requires selected_ids")
            reason = str(parsed.get("reason") or "").strip()
            return selected_ids, reason
        finally:
            await client.close()

    def _record_selection_receipt(
        self,
        *,
        agent_id: uuid.UUID,
        run_id: str,
        candidates: list[MemoryItem],
        selected: list[MemoryItem],
        status: str,
        reason: str = "",
        error: str = "",
    ) -> None:
        path = self.data_root / str(agent_id) / "memory" / "control" / "retrieval_reviews" / f"{run_id}.decision.json"
        payload = {
            "schema": "hive.memory.semantic-selection.v1",
            "status": status,
            "coverage_path": self.last_selection_coverage_path,
            "candidate_ids": [str(item.metadata["selection_candidate_id"]) for item in candidates],
            "candidate_sha256": {
                str(item.metadata["selection_candidate_id"]): hashlib.sha256(item.content.encode("utf-8")).hexdigest()
                for item in candidates
            },
            "selected_ids": [str(item.metadata["selection_candidate_id"]) for item in selected],
            "reason": reason,
            "error": error,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.last_selection_receipt = path.as_posix()
        except OSError as exc:
            logger.warning("[Retriever] failed to persist semantic-selection receipt: %s", exc)

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
                MemoryItem(
                    kind=item.kind,
                    content=item.content,
                    score=decision.score,
                    source=item.source,
                    metadata=metadata,
                )
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
                MemoryItem(
                    kind=item.kind,
                    content=item.content,
                    score=item.score,
                    source=item.source,
                    metadata={**item.metadata, "context_boost": round(factor, 6)},
                )
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
                MemoryItem(
                    kind=item.kind,
                    content=item.content,
                    score=item.score,
                    source=item.source,
                    metadata={**item.metadata, **record},
                )
            )
        return joined

    def _retrieve_explicit_overlay(self, agent_id: uuid.UUID, *, query: str = "") -> list[MemoryItem]:
        items: list[MemoryItem] = []
        for fact in search_explicit_overlay_entries(self.data_root, agent_id, query, limit=None):
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
                )
            )
        return items

    # -- Episodic layer: session summaries from DB --

    async def _retrieve_episodic(
        self,
        agent_id: uuid.UUID,
        session_id: str | None,
        *,
        previous_limit: int | None = None,
    ) -> list[MemoryItem]:
        del previous_limit  # compatibility-only; model selection owns relevance
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
                                metadata={
                                    "session_id": str(row[1]),
                                    "is_current_session": True,
                                    "description": _bounded_descriptor_text(row[0]),
                                },
                            )
                        )

                # Previous session summaries — complete authorized continuity evidence.
                prev_query = (
                    select(ChatSession.summary, ChatSession.id, ChatSession.last_message_at)
                    .where(
                        (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
                        ChatSession.summary.isnot(None),
                    )
                    .order_by(ChatSession.last_message_at.desc(), ChatSession.created_at.desc())
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
                                    "description": _bounded_descriptor_text(row[0]),
                                },
                            )
                        )

        except Exception as exc:
            logger.warning("Episodic retrieval failed: %s", exc)

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
