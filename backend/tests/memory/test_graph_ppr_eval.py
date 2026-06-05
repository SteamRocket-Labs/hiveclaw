"""P9 tests — derived relation graph, wikilink navigation, KG+PPR retrieval,
and the memory eval benchmark (docs/agent-memory-md-first-spec.md §12 P9).

MD-first invariants under test:
- The graph is rebuilt from Markdown on every call — zero persisted derived
  state, deleting the module loses no durable data.
- Retrieval results carry source_refs back to Markdown paths (§1.3).
- Only status=active pages are retrievable.
"""

from __future__ import annotations

import uuid
from pathlib import Path

AGENT = uuid.uuid4()


def _write_page(root: Path, subdir: str, slug: str, body: str) -> Path:
    directory = root / str(AGENT) / "memory" / subdir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _seed_graph_corpus(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki",
        "memory-control-plane",
        "---\ntitle: Memory Control Plane\ntype: concept\ntags: [memory, governance]\nstatus: active\n---\n\n"
        "## Current Claim\n\nDistillers produce candidates; the control plane decides writes.\n\n"
        "## Scope\n\nHive memory engine governance.\n\n"
        "## Evidence\n\n- [decision] write gate governs T3 #governance\n\n"
        "## Contradictions\n\n(none)\n\n## Changes\n\n- [2026-06-04] created\n\n"
        "## Retrieval Tags\n\nmemory, governance\n\n"
        "## Relations\n\n- depends_on [[Memory Write Gate]]\n- supersedes [[Direct T3 Rewrite]]\n",
    )
    _write_page(
        tmp_path,
        "wiki",
        "memory-write-gate",
        "---\ntitle: Memory Write Gate\ntype: concept\ntags: [memory]\nstatus: active\n---\n\n"
        "## Current Claim\n\nprepare_memory_write classifies privacy and stamps lifecycle metadata.\n\n"
        "## Scope\n\nT3 writes.\n\n## Evidence\n\n- [code] privacy layer rejects PL4 #privacy\n\n"
        "## Contradictions\n\n(none)\n\n## Changes\n\n- [2026-06-04] created\n\n"
        "## Retrieval Tags\n\nwrite gate, privacy\n\n"
        "## Relations\n\n- depends_on [[Privacy Layer]]\n",
    )
    _write_page(
        tmp_path,
        "wiki",
        "privacy-layer",
        "---\ntitle: Privacy Layer\ntype: concept\ntags: [privacy]\nstatus: active\n---\n\n"
        "## Current Claim\n\nClassifies PL0-PL4 and masks PII placeholders.\n\n"
        "## Scope\n\nAll durable writes.\n\n## Evidence\n\n- [code] classify_and_mask #privacy\n\n"
        "## Contradictions\n\n(none)\n\n## Changes\n\n- [2026-06-04] created\n\n"
        "## Retrieval Tags\n\npii, masking\n",
    )
    _write_page(
        tmp_path,
        "wiki",
        "retired-claim",
        "---\ntitle: Retired Claim\ntype: concept\ntags: [old]\nstatus: archived\n---\n\n"
        "## Current Claim\n\nan archived governance claim about the control plane decides nothing\n\n"
        "## Scope\n\nx\n\n## Evidence\n\n- [x] y\n\n## Contradictions\n\n(none)\n\n"
        "## Changes\n\n- [2026-01-01] retired\n\n## Retrieval Tags\n\ngovernance\n",
    )
    _write_page(
        tmp_path,
        "scenes",
        "railway-deployments",
        "---\ntitle: Railway Deployments\ntype: scene\ntags: [deploy]\nstatus: active\n---\n\n"
        "## Narrative\n\nDeploys interact with the [[Memory Control Plane]] audit trail.\n\n"
        "## Evidence\n\n- [fact] cached layers bite #deploy\n\n## Changes\n\n- [2026-06-04] created\n",
    )


# ── A. Derived relation graph ──


def test_graph_parses_typed_relations_and_inline_wikilinks(tmp_path: Path) -> None:
    from app.memory.relation_graph import build_relation_graph

    _seed_graph_corpus(tmp_path)
    graph = build_relation_graph(tmp_path, AGENT)

    nodes = {node.node_id: node for node in graph.nodes}
    assert "wiki/memory-control-plane" in nodes
    assert "scenes/railway-deployments" in nodes

    edges = {(edge.source, edge.rel_type, edge.target) for edge in graph.edges}
    assert ("wiki/memory-control-plane", "depends_on", "wiki/memory-write-gate") in edges
    # Inline wikilink becomes an untyped references edge.
    assert ("scenes/railway-deployments", "references", "wiki/memory-control-plane") in edges


def test_graph_keeps_forward_references_as_missing_nodes(tmp_path: Path) -> None:
    from app.memory.relation_graph import build_relation_graph

    _seed_graph_corpus(tmp_path)
    graph = build_relation_graph(tmp_path, AGENT)

    nodes = {node.node_id: node for node in graph.nodes}
    # [[Direct T3 Rewrite]] page does not exist — forward reference kept.
    assert "wiki/direct-t3-rewrite" in nodes
    assert nodes["wiki/direct-t3-rewrite"].exists is False
    assert nodes["wiki/memory-control-plane"].exists is True


def test_graph_is_pure_derivation_with_no_persisted_state(tmp_path: Path) -> None:
    from app.memory.relation_graph import build_relation_graph

    _seed_graph_corpus(tmp_path)
    mem_dir = tmp_path / str(AGENT) / "memory"
    before = sorted(p.relative_to(mem_dir).as_posix() for p in mem_dir.rglob("*") if p.is_file())
    build_relation_graph(tmp_path, AGENT)
    after = sorted(p.relative_to(mem_dir).as_posix() for p in mem_dir.rglob("*") if p.is_file())
    assert before == after  # rebuildable accelerator — never writes


def test_graph_links_for_page_exposes_both_directions(tmp_path: Path) -> None:
    from app.memory.relation_graph import build_relation_graph

    _seed_graph_corpus(tmp_path)
    graph = build_relation_graph(tmp_path, AGENT)
    links = graph.links_for("wiki/memory-control-plane")

    outgoing = {(link["rel_type"], link["page_id"]) for link in links["outgoing"]}
    incoming = {(link["rel_type"], link["page_id"]) for link in links["incoming"]}
    assert ("depends_on", "wiki/memory-write-gate") in outgoing
    assert ("references", "scenes/railway-deployments") in incoming


# ── C. PPR ──


def test_personalized_pagerank_propagates_multi_hop() -> None:
    from app.memory.relation_graph import personalized_pagerank

    adjacency = {
        "a": ["b"],
        "b": ["c"],
        "c": [],
        "isolated": [],
    }
    scores = personalized_pagerank(adjacency, seeds={"a": 1.0})
    assert scores["a"] > scores["b"] > scores["c"] > scores["isolated"]
    assert scores["c"] > 0  # two hops away still reachable


def test_personalized_pagerank_handles_empty_inputs() -> None:
    from app.memory.relation_graph import personalized_pagerank

    assert personalized_pagerank({}, seeds={}) == {}
    scores = personalized_pagerank({"a": []}, seeds={})
    assert scores == {"a": 0.0}


# ── C. Wiki retrieval (BM25 seeds + PPR expansion) ──


def test_wiki_search_bm25_finds_direct_hit(tmp_path: Path) -> None:
    from app.memory.wiki_retrieval import search_wiki_pages

    _seed_graph_corpus(tmp_path)
    hits = search_wiki_pages(tmp_path, AGENT, "privacy masking PII", method="bm25", limit=3)

    assert hits
    assert hits[0]["page_id"] == "wiki/privacy-layer"
    assert hits[0]["source_ref"] == "memory/wiki/privacy-layer.md"


def test_wiki_search_ppr_surfaces_multi_hop_neighbor(tmp_path: Path) -> None:
    from app.memory.wiki_retrieval import search_wiki_pages

    _seed_graph_corpus(tmp_path)
    # Query words hit control-plane/write-gate; privacy-layer is linked two
    # hops away and never mentions "governance" — PPR pulls it in.
    ppr_hits = search_wiki_pages(tmp_path, AGENT, "governance write gate", method="ppr", limit=4)
    ppr_ids = [hit["page_id"] for hit in ppr_hits]
    assert "wiki/privacy-layer" in ppr_ids

    bm25_hits = search_wiki_pages(tmp_path, AGENT, "governance write gate", method="bm25", limit=4)
    bm25_ids = [hit["page_id"] for hit in bm25_hits]
    assert "wiki/privacy-layer" not in bm25_ids  # the lexical method cannot reach it


def test_wiki_search_excludes_non_active_pages(tmp_path: Path) -> None:
    from app.memory.wiki_retrieval import search_wiki_pages

    _seed_graph_corpus(tmp_path)
    for method in ("bm25", "ppr"):
        hits = search_wiki_pages(tmp_path, AGENT, "governance control plane", method=method, limit=10)
        assert all(hit["page_id"] != "wiki/retired-claim" for hit in hits)


def test_wiki_search_empty_corpus_returns_empty(tmp_path: Path) -> None:
    from app.memory.wiki_retrieval import search_wiki_pages

    assert search_wiki_pages(tmp_path, uuid.uuid4(), "anything", method="ppr") == []


# ── D. Memory eval benchmark ──


def test_retrieval_eval_reports_bm25_vs_ppr(tmp_path: Path) -> None:
    from app.memory.retrieval_eval import evaluate_memory_retrieval

    report = evaluate_memory_retrieval(data_root=tmp_path)

    for method in ("bm25", "ppr"):
        assert 0.0 <= report["methods"][method]["recall_at_3"] <= 1.0
        assert 0.0 <= report["methods"][method]["mrr"] <= 1.0
    assert report["cases"], "benchmark must contain cases"
    assert any(case["kind"] == "multi_hop" for case in report["cases"])
    # The experiment's reason to exist: PPR must not lose to BM25 on the
    # multi-hop slice (it reaches linked pages lexical search cannot).
    assert report["multi_hop"]["ppr_recall"] >= report["multi_hop"]["bm25_recall"]
    assert report["default_method"] in ("bm25", "ppr")


def test_retirement_safety_eval_protects_critical_memory(tmp_path: Path) -> None:
    from app.memory.retrieval_eval import evaluate_retirement_safety

    report = evaluate_retirement_safety(data_root=tmp_path)

    assert report["passed"] is True
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["protected_entries_never_candidates"]["passed"] is True
    assert checks["promoted_entries_never_candidates"]["passed"] is True
    assert checks["cap_eviction_archives_everything"]["passed"] is True


# ── B. Navigation consumers ──


def test_knowledge_page_detail_includes_links(tmp_path: Path) -> None:
    from app.services.knowledge_read_model import get_knowledge_page

    _seed_graph_corpus(tmp_path)
    page = get_knowledge_page(tmp_path, AGENT, "wiki/memory-control-plane")

    assert page is not None
    outgoing = {(link["rel_type"], link["page_id"]) for link in page["links"]["outgoing"]}
    incoming = {(link["rel_type"], link["page_id"]) for link in page["links"]["incoming"]}
    assert ("depends_on", "wiki/memory-write-gate") in outgoing
    assert ("references", "scenes/railway-deployments") in incoming
    # Forward references are navigable as "not yet created".
    missing = [link for link in page["links"]["outgoing"] if not link["exists"]]
    assert any(link["page_id"] == "wiki/direct-t3-rewrite" for link in missing)
