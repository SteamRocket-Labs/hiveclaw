"""Wiki/scene page retrieval — BM25 seeds + optional PPR expansion (P9).

Two methods, one contract:

- ``bm25``: lexical ranking over page title + retrieval tags + body.
- ``ppr``: HippoRAG-inspired pattern completion — BM25 hits become the
  personalization seeds, Personalized PageRank propagates relevance over
  the wikilink network, so pages linked to the query's topic surface even
  when they share no query terms (multi-hop).

The default method is chosen by the memory eval benchmark
(`app/memory/retrieval_eval.py`), not by taste — see DEFAULT_WIKI_METHOD.

Safety: only status=active pages are retrievable (archived/superseded pages
are de-indexed from recall, spec §4.9); page content already passed the
privacy gate at apply time (P5). Every hit carries a ``source_ref`` back to
its Markdown path (§1.3).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.memory.md_store import _bm25_score, _bm25_tokenize
from app.memory.relation_graph import RelationGraph, build_relation_graph, personalized_pagerank

logger = logging.getLogger(__name__)

# Outcome of the retrieval eval benchmark (multi-hop slice: PPR reaches
# linked pages lexical search cannot; direct-hit slice: parity). Re-run
# `evaluate_memory_retrieval()` after retrieval changes before editing this.
DEFAULT_WIKI_METHOD = "ppr"

_PREVIEW_CHARS = 160
# PPR scores are graph-mass values (sum≈1) — far smaller than BM25 scores.
# Hits whose total score is zero are dropped; this floor only filters
# numerically-dead nodes, not weak-but-reachable ones.
_SCORE_FLOOR = 1e-9


def _page_text(data_root: Path, agent_id: uuid.UUID, page_id: str) -> str:
    path = Path(data_root) / str(agent_id) / "memory" / f"{page_id}.md"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2]
    return text


def _preview(text: str) -> str:
    body = " ".join(_strip_frontmatter(text).split())
    return body[:_PREVIEW_CHARS]


def search_wiki_pages(
    data_root: Path,
    agent_id: uuid.UUID,
    query: str,
    *,
    method: str = DEFAULT_WIKI_METHOD,
    limit: int = 5,
    graph: RelationGraph | None = None,
) -> list[dict]:
    """Rank active wiki/scene pages for a query.

    Returns rows of ``{page_id, title, kind, score, method, preview,
    source_ref}`` — ``source_ref`` always resolves back to the Markdown
    truth source.
    """
    needle = (query or "").strip()
    if not needle:
        return []

    if graph is None:
        graph = build_relation_graph(data_root, agent_id)
    active_nodes = [node for node in graph.nodes if node.exists and node.status == "active"]
    if not active_nodes:
        return []

    texts: dict[str, str] = {}
    corpus_tokens: list[list[str]] = []
    for node in active_nodes:
        text = _page_text(data_root, agent_id, node.node_id)
        texts[node.node_id] = text
        corpus_tokens.append(_bm25_tokenize(f"{node.title} {node.tags} {_strip_frontmatter(text)}"))

    query_tokens = _bm25_tokenize(needle)
    if not query_tokens:
        return []
    bm25_scores = _bm25_score(query_tokens, corpus_tokens)
    lexical = {node.node_id: score for node, score in zip(active_nodes, bm25_scores)}

    normalized_method = (method or DEFAULT_WIKI_METHOD).strip().lower()
    if normalized_method == "auto":
        normalized_method = DEFAULT_WIKI_METHOD

    if normalized_method == "ppr":
        seeds = {node_id: score for node_id, score in lexical.items() if score > 0}
        ppr_scores = personalized_pagerank(graph.adjacency(), seeds)
        # Blend: graph relevance carries the ranking; a small lexical term
        # keeps direct hits ahead of equally-connected neighbors.
        max_lexical = max(lexical.values()) or 1.0
        final = {
            node_id: ppr_scores.get(node_id, 0.0) + 0.1 * (lexical.get(node_id, 0.0) / max_lexical)
            for node_id in lexical
        }
    else:
        final = lexical

    ranked = sorted(
        (item for item in final.items() if item[1] > _SCORE_FLOOR),
        key=lambda item: item[1],
        reverse=True,
    )

    nodes_by_id = {node.node_id: node for node in active_nodes}
    hits: list[dict] = []
    for node_id, score in ranked[: max(1, limit)]:
        node = nodes_by_id[node_id]
        hits.append(
            {
                "page_id": node_id,
                "title": node.title,
                "kind": node.kind,
                "score": round(score, 6),
                "method": normalized_method,
                "preview": _preview(texts.get(node_id, "")),
                "source_ref": f"memory/{node_id}.md",
            }
        )
    return hits
