"""Deep Research leaf presets (DR-2+, docs/deep-research-workflow-unification.md).

The explorer preset migrates ``RuntimeResearchWorker``'s governed shape onto
the leaf preset registry: the same web allow-list and recursion guards ride
the spec into the REAL ``spawn_subagent`` (invariant I3), while the
deterministic refinement the old worker did in-process — RC2 binary/PDF
rejection, fetched-text cleaning, title/publisher/source-type inference,
tier/grade — runs in ``post_process`` on the system side and lands in a
per-leaf ledger shard (D2: shards merge before synthesis; the LLM never
writes artifacts itself).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.agents.subagent import SubagentResult, SubagentSpawnContext
from app.config import get_settings
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.deep_research.extractor import clean_fetched_text, extract_claims_from_source
from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.worker import (
    _MAX_SOURCE_CONTENT_CHARS,
    _MAX_SOURCES_PER_WORKER,
    RESEARCH_WORKER_ALLOWED_TOOLS,
    RESEARCH_WORKER_EXCLUDED_TOOLS,
    _extract_title,
    _has_usable_content,
    _infer_source_type,
    _looks_like_binary_or_pdf,
    _publisher_from_url,
)
from app.services.workflow_leaf_presets import LeafPreset, register_leaf_preset

logger = logging.getLogger(__name__)

DEEP_RESEARCH_ARTIFACT_SUBDIR = "deep_research"

_EXPLORER_SYSTEM_PROMPT = (
    "SUB-AGENT ROLE: Deep Research workflow explorer. "
    "You may browse only with the provided read-only web tools. "
    "Do not delegate, do not write files, do not call Deep Research recursively. "
    "Search snippets are discovery only; fetched pages are evidence. "
    "Integrate findings across sources — never produce a per-page list. "
    "Report disconfirming evidence; do not cherry-pick. "
    "Cite source URLs in prose; the runtime assigns durable ids later."
)


def run_artifact_dir(agent_id: uuid.UUID | str, run_id: uuid.UUID | str) -> Path:
    """Run-scoped Deep Research artifact root, derivable from (agent, run)
    alone so a daemon resume reconstructs it with no extra context."""
    return (
        Path(get_settings().AGENT_DATA_DIR)
        / str(agent_id)
        / "runtime_artifacts"
        / "workflow_runs"
        / str(run_id)
        / DEEP_RESEARCH_ARTIFACT_SUBDIR
    )


def _shard_dir(ctx: SubagentSpawnContext, request: LeafRequest) -> Path:
    leaf_key = request.leaf_id or request.step_id
    return run_artifact_dir(ctx.parent_agent_id, request.run_id) / "shards" / leaf_key


def _refine_raw_source(raw: dict, ledger: EvidenceLedger, *, query: str, lane_id: str) -> str | None:
    """Old worker._source_from_tool_event, system side: RC2 guard → clean →
    usable gate → cap → grade via ledger.add_source. Returns the source_id."""
    url = str(raw.get("url") or "").strip()
    content = str(raw.get("content") or "")
    if not url or _looks_like_binary_or_pdf(content):
        return None
    cleaned = clean_fetched_text(content)
    if not _has_usable_content(cleaned):
        return None
    if len(cleaned) > _MAX_SOURCE_CONTENT_CHARS:
        cleaned = cleaned[:_MAX_SOURCE_CONTENT_CHARS]
    record = ledger.add_source(
        url=url,
        title=_extract_title(content) or url,
        publisher=_publisher_from_url(url),
        source_type=_infer_source_type(url),
        content=cleaned,
        lane_id=lane_id,
        query=query,
        fetch_tool=str(raw.get("tool_name") or ""),
    )
    return record.source_id


async def _explorer_post_process(
    request: LeafRequest,
    ctx: SubagentSpawnContext,
    result: SubagentResult | None,
    outcome: LeafOutcome,
) -> LeafOutcome:
    """Refine the spawn's raw captured sources into this leaf's ledger shard.

    Runs even when the subagent failed — partially fetched sources are still
    evidence for the merge (the old orchestrator kept a failed worker's
    sources too). Shard writes are deterministic and per-leaf, so concurrent
    fanout leaves never contend (D2).
    """
    raw_sources = list(result.sources) if result is not None else []
    if not raw_sources:
        return outcome

    shard = _shard_dir(ctx, request)
    ledger = EvidenceLedger(shard)
    source_ids: list[str] = []
    for raw in raw_sources[:_MAX_SOURCES_PER_WORKER]:
        try:
            source_id = _refine_raw_source(raw, ledger, query=request.task[:200], lane_id=request.leaf_id or "")
        except Exception as exc:  # refinement must never fail the leaf
            logger.warning("[DR-leaf] source refinement failed (skipped): %s", exc)
            continue
        if source_id is not None:
            source_ids.append(source_id)
            extract_claims_from_source(ledger, ledger.sources[source_id])

    if not outcome.ok:
        return outcome  # shard written; the failure verdict stands untouched
    output = dict(outcome.output or {})
    output.update(
        {
            "source_count": len(source_ids),
            "source_ids": source_ids,
            "ledger_shard": str(shard),
        }
    )
    return LeafOutcome(ok=True, output=output, result_ref=outcome.result_ref, tokens_used=outcome.tokens_used)


DEEP_RESEARCH_EXPLORER_PRESET = LeafPreset(
    allowed_tools=RESEARCH_WORKER_ALLOWED_TOOLS,
    excluded_tools=RESEARCH_WORKER_EXCLUDED_TOOLS,
    system_prompt=_EXPLORER_SYSTEM_PROMPT,
    disable_tools=False,
    post_process=_explorer_post_process,
    options={"max_sources": _MAX_SOURCES_PER_WORKER, "max_source_chars": _MAX_SOURCE_CONTENT_CHARS},
)


def register_deep_research_leaf_presets() -> None:
    """Idempotent registration of all Deep Research leaf presets."""
    register_leaf_preset("deep_research_explorer", DEEP_RESEARCH_EXPLORER_PRESET)
