"""Derived relation graph over wiki/scene Markdown (spec §12 P9).

MD-first invariants:
- The graph is a derived accelerator rebuilt from Markdown on every call —
  zero persisted state; deleting this module loses no durable data.
- The KG is the wikilink network the P5 curators (LLM) already author. This
  module performs deterministic syntax parsing only (`## Relations` typed
  edges and inline `[[wikilinks]]`, spec §3.1) — it never invents semantic
  edges, so there is no second truth source.
- Forward references (links to pages not yet created) are kept as
  exists=False nodes (§3.1: they mark pages worth writing, not errors).

`personalized_pagerank` is the HippoRAG-inspired pattern-completion ranker
(§1.3): seeds from a lexical match, multi-hop propagation over the link
network, scores resolve back to Markdown paths.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_WIKILINK_RE = re.compile(r"\[\[([^\]\[]+)\]\]")
_RELATION_LINE_RE = re.compile(r"^-\s+(?P<rel>[a-z_]+)\s+\[\[(?P<target>[^\]\[]+)\]\]\s*$")
_RELATIONS_HEADER_RE = re.compile(r"^##\s+Relations\s*$")
_SECTION_HEADER_RE = re.compile(r"^##\s+")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Two-plane knowledge network dirs (spec §1.1) — the default since the C7
# cutover; the legacy wiki/scenes directories are archive-only.
KNOWLEDGE_PAGE_DIRS = ("knowledge", "milestones")
_PAGE_DIRS = KNOWLEDGE_PAGE_DIRS

_LEGACY_KIND_BY_DIR = {"wiki": "wiki", "scenes": "scene"}
_KNOWLEDGE_LINK_PREFIX = "k:"


@dataclass(slots=True)
class GraphNode:
    node_id: str  # "wiki/<slug>" | "scenes/<slug>"
    kind: str  # wiki | scene
    slug: str
    title: str
    status: str
    exists: bool
    tags: str = ""


@dataclass(slots=True)
class GraphEdge:
    source: str
    target: str
    rel_type: str  # typed relation from ## Relations, or "references" for inline links


@dataclass(slots=True)
class RelationGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    def node_map(self) -> dict[str, GraphNode]:
        return {node.node_id: node for node in self.nodes}

    def adjacency(self, *, undirected: bool = True, active_only: bool = True) -> dict[str, list[str]]:
        """Neighbor lists for PPR. Undirected by default — relevance flows
        both ways along a link even though the relation itself is directed."""
        allowed = {node.node_id for node in self.nodes if node.exists and (not active_only or node.status == "active")}
        neighbors: dict[str, set[str]] = {node_id: set() for node_id in allowed}
        for edge in self.edges:
            if edge.source in allowed and edge.target in allowed:
                neighbors[edge.source].add(edge.target)
                if undirected:
                    neighbors[edge.target].add(edge.source)
        return {node_id: sorted(targets) for node_id, targets in neighbors.items()}

    def links_for(self, page_id: str) -> dict[str, list[dict]]:
        """Outgoing/incoming link rows for page-detail navigation."""
        nodes = self.node_map()

        def _row(target_id: str, rel_type: str) -> dict:
            node = nodes.get(target_id)
            return {
                "page_id": target_id,
                "title": node.title if node else target_id.rsplit("/", 1)[-1].replace("-", " ").title(),
                "rel_type": rel_type,
                "exists": bool(node and node.exists),
                "status": node.status if node else "",
            }

        outgoing = [_row(edge.target, edge.rel_type) for edge in self.edges if edge.source == page_id]
        incoming = [_row(edge.source, edge.rel_type) for edge in self.edges if edge.target == page_id]
        return {"outgoing": outgoing, "incoming": incoming}


def slugify_title(value: str) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return slug[:80] or "concept"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, text[match.end() :]


def _parse_page_links(body: str) -> list[tuple[str, str]]:
    """Return (rel_type, target_title) pairs from one page body.

    `## Relations` rows yield their typed edge; every other inline
    `[[wikilink]]` yields a "references" edge. Relations rows are excluded
    from the inline pass so a typed edge is not double-counted.
    """
    links: list[tuple[str, str]] = []
    relation_titles: set[str] = set()

    in_relations = False
    for line in body.splitlines():
        stripped = line.strip()
        if _RELATIONS_HEADER_RE.match(stripped):
            in_relations = True
            continue
        if in_relations and _SECTION_HEADER_RE.match(stripped):
            in_relations = False
        if in_relations:
            match = _RELATION_LINE_RE.match(stripped)
            if match:
                target = match.group("target").strip()
                links.append((match.group("rel").strip(), target))
                relation_titles.add(target)

    body_without_relations: list[str] = []
    in_relations = False
    for line in body.splitlines():
        stripped = line.strip()
        if _RELATIONS_HEADER_RE.match(stripped):
            in_relations = True
            continue
        if in_relations and _SECTION_HEADER_RE.match(stripped):
            in_relations = False
        if not in_relations:
            body_without_relations.append(line)

    for target in _WIKILINK_RE.findall("\n".join(body_without_relations)):
        cleaned = target.strip()
        if cleaned and cleaned not in relation_titles:
            links.append(("references", cleaned))
    return links


def build_relation_graph(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    page_dirs: tuple[str, ...] = _PAGE_DIRS,
) -> RelationGraph:
    """Rebuild the page graph from the given memory page directories.

    Defaults to the legacy wiki/scenes layout; pass ``KNOWLEDGE_PAGE_DIRS``
    for the two-plane knowledge/milestones network. ``[[k:Title]]`` links
    resolve into the knowledge dir; unresolved targets become
    forward-reference nodes in the first (primary) page dir (spec §3.4:
    forward references mark pages worth writing, not errors).
    """
    mem_dir = Path(data_root) / str(agent_id) / "memory"
    graph = RelationGraph()
    slug_to_node: dict[str, GraphNode] = {}
    pending_links: list[tuple[str, str, str]] = []  # (source_id, rel_type, target_title)
    fallback_dir = "knowledge" if "knowledge" in page_dirs else page_dirs[0]

    for subdir in page_dirs:
        directory = mem_dir / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            frontmatter, body = _parse_frontmatter(text)
            slug = path.stem
            node = GraphNode(
                node_id=f"{subdir}/{slug}",
                kind=_LEGACY_KIND_BY_DIR.get(subdir, subdir),
                slug=slug,
                title=frontmatter.get("title") or slug.replace("-", " ").title(),
                status=(frontmatter.get("status") or "active").strip().lower(),
                exists=True,
                tags=frontmatter.get("tags", ""),
            )
            graph.nodes.append(node)
            slug_to_node[slug] = node
            for rel_type, target_title in _parse_page_links(body):
                pending_links.append((node.node_id, rel_type, target_title))

    # Resolve link targets: title slugs match page filenames across page_dirs;
    # unresolved targets become forward-reference nodes in the primary dir.
    seen_missing: set[str] = set()
    for source_id, rel_type, target_title in pending_links:
        cleaned_title = target_title
        if cleaned_title.lower().startswith(_KNOWLEDGE_LINK_PREFIX):
            cleaned_title = cleaned_title[len(_KNOWLEDGE_LINK_PREFIX) :].strip()
        target_slug = slugify_title(cleaned_title)
        node = slug_to_node.get(target_slug)
        if node is not None:
            target_id = node.node_id
        else:
            target_id = f"{fallback_dir}/{target_slug}"
            if target_slug not in seen_missing:
                seen_missing.add(target_slug)
                graph.nodes.append(
                    GraphNode(
                        node_id=target_id,
                        kind=_LEGACY_KIND_BY_DIR.get(fallback_dir, fallback_dir),
                        slug=target_slug,
                        title=cleaned_title,
                        status="missing",
                        exists=False,
                    )
                )
        graph.edges.append(GraphEdge(source=source_id, target=target_id, rel_type=rel_type))

    return graph


def personalized_pagerank(
    adjacency: dict[str, list[str]],
    seeds: dict[str, float],
    *,
    damping: float = 0.85,
    iterations: int = 30,
) -> dict[str, float]:
    """Power-iteration PPR over a neighbor-list graph. Pure Python, no deps.

    `seeds` is the personalization vector (lexical match strength). Dangling
    nodes restart at the seeds so mass is never lost. Returns a score per
    node; with empty seeds every node scores 0.0.
    """
    nodes = list(adjacency.keys())
    if not nodes:
        return {}

    seed_total = sum(max(0.0, weight) for weight in seeds.values() if weight)
    if seed_total <= 0:
        return {node: 0.0 for node in nodes}
    personalization = {node: max(0.0, seeds.get(node, 0.0)) / seed_total for node in nodes}

    scores = dict(personalization)
    for _ in range(iterations):
        next_scores = {node: (1.0 - damping) * personalization[node] for node in nodes}
        for node in nodes:
            neighbors = adjacency.get(node) or []
            mass = damping * scores[node]
            if neighbors:
                share = mass / len(neighbors)
                for neighbor in neighbors:
                    if neighbor in next_scores:
                        next_scores[neighbor] += share
            else:
                # Dangling node: restart its mass at the personalization vector.
                for target, weight in personalization.items():
                    next_scores[target] += mass * weight
        scores = next_scores

    return {node: round(score, 6) for node, score in scores.items()}
