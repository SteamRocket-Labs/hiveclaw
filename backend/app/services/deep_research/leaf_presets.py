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

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from app.agents.subagent import SubagentResult, SubagentSpawnContext
from app.config import get_settings
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.deep_research.extractor import clean_fetched_text, extract_claims_from_source
from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.schemas import ResearchRequest, SourceRecord, SourceType, to_jsonable
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

    # Shard bus (D2/D5): the digest + topic meta are what the synthesizer and
    # critic pre_process rebuild their inputs from — never the str()-rendered
    # step-output template.
    digest = ""
    if isinstance((outcome.output or {}).get("text"), str) and (outcome.output or {})["text"].strip():
        digest = (outcome.output or {})["text"]
    elif result is not None and result.content:
        digest = str(result.content)
    (shard / "digest.md").write_text(digest, encoding="utf-8")
    (shard / "meta.json").write_text(
        json.dumps(
            {
                "leaf_id": request.leaf_id or request.step_id,
                "topic": request.task[:200],
                "status": (result.status if result is not None else "failed"),
                "error": (result.error if result is not None else None),
                "tokens_used": (result.tokens_used if result is not None else 0),
                "source_ids": source_ids,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

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


# ── shard bus readers (DR-3) ───────────────────────────────────────


def _load_request(root: Path) -> ResearchRequest:
    """The launch wrote the confirmed ResearchRequest next to the run's
    artifacts; every non-explorer leaf rebuilds its domain context from it.
    Missing file = fail loud (the leaf fails, never degrades silently)."""
    payload = json.loads((root / "request.json").read_text(encoding="utf-8"))
    return ResearchRequest.from_arguments(payload)


def _source_from_row(row: dict) -> SourceRecord:
    data = dict(row)
    data["source_type"] = SourceType(data.get("source_type") or SourceType.UNKNOWN.value)
    return SourceRecord(**data)


def _load_shards(root: Path) -> tuple[list[dict[str, Any]], list[SourceRecord], list[dict]]:
    """Read every explorer shard: (worker_digests, sources, claim_rows)."""
    digests: list[dict[str, Any]] = []
    sources: list[SourceRecord] = []
    claims: list[dict] = []
    shards_dir = root / "shards"
    if not shards_dir.exists():
        return digests, sources, claims
    for shard in sorted(shards_dir.iterdir()):
        if not shard.is_dir():
            continue
        meta: dict[str, Any] = {}
        meta_path = shard / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        shard_sources: list[SourceRecord] = []
        sources_path = shard / "sources.jsonl"
        if sources_path.exists():
            for line in sources_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    shard_sources.append(_source_from_row(json.loads(line)))
        claims_path = shard / "claims.jsonl"
        if claims_path.exists():
            claims.extend(
                json.loads(line) for line in claims_path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
        digest_path = shard / "digest.md"
        digests.append(
            {
                "topic": meta.get("topic") or shard.name,
                "status": meta.get("status") or "ok",
                "error": meta.get("error"),
                "tokens_used": meta.get("tokens_used") or 0,
                "intermediate_report": digest_path.read_text(encoding="utf-8")[:40000] if digest_path.exists() else "",
                "sources": [_source_meta(record) for record in shard_sources],
            }
        )
        sources.extend(shard_sources)
    return digests, sources, claims


def _source_meta(record: SourceRecord) -> dict[str, Any]:
    return {
        "source_id": record.source_id,
        "title": record.title,
        "publisher": record.publisher,
        "url": record.url,
        "source_type": record.source_type.value,
        "lane_id": record.lane_id,
        "fetch_tool": record.fetch_tool,
        "evidence_tier": record.evidence_tier,
        "evidence_grade": record.evidence_grade,
    }


def _merged_ledger(root: Path, sources: list[SourceRecord], claim_rows: list[dict]) -> EvidenceLedger:
    """Materialise the top-level merged ledger (same filenames as the legacy
    artifact contract: sources.jsonl / claims.jsonl at the run root)."""
    for name in ("sources.jsonl", "claims.jsonl"):
        path = root / name
        if path.exists():
            path.unlink()  # idempotent re-merge (definition retry re-runs pre)
    ledger = EvidenceLedger(root)
    for record in sources:
        ledger.add_source(
            url=record.url,
            title=record.title,
            publisher=record.publisher,
            source_type=record.source_type,
            content=record.content,
            published_at=record.published_at,
            lane_id=record.lane_id,
            query=record.query,
            fetch_tool=record.fetch_tool,
            source_id=record.source_id,
        )
    for row in claim_rows:
        ledger.add_claim(
            text=str(row.get("text") or ""),
            status=str(row.get("status") or "unsupported"),
            source_ids=list(row.get("source_ids") or []),
            evidence=str(row.get("evidence") or ""),
            notes=str(row.get("notes") or ""),
            contradiction_group=row.get("contradiction_group"),
        )
    return ledger


def _load_merged_ledger(root: Path) -> EvidenceLedger:
    """Rebuild the merged ledger object from the run-root jsonl files without
    rewriting them (post_process side)."""
    ledger = EvidenceLedger.__new__(EvidenceLedger)
    ledger.artifact_dir = root
    ledger.sources_path = root / "sources.jsonl"
    ledger.claims_path = root / "claims.jsonl"
    ledger.sources = {}
    ledger.claims = []
    if ledger.sources_path.exists():
        for line in ledger.sources_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = _source_from_row(json.loads(line))
                ledger.sources[record.source_id] = record
    return ledger


# ── planner ────────────────────────────────────────────────────────

_PLANNER_SYSTEM_PROMPT = (
    "SUB-AGENT ROLE: Deep Research strategy planner. Tools are disabled; reason "
    "from the brief alone. Produce a compact per-topic retrieval strategy "
    "(angles to cover, source classes to prioritise, freshness requirements, "
    "known pitfalls). The explorers receive your brief verbatim — be concrete "
    "and domain-general, never pad."
)

DEEP_RESEARCH_PLANNER_PRESET = LeafPreset(
    system_prompt=_PLANNER_SYSTEM_PROMPT,
    disable_tools=True,  # RC11 family: pure reasoning leaf
)


# ── critic (devils advocate — BEFORE synthesis, P-Q2) ─────────────

_CRITIC_INSTRUCTION = (
    "You are the Devil's Advocate on a deep research run (any domain). Steel-man the emerging findings, "
    "then stress-test them BEFORE the final report is written. Be specific, constructive, and cite "
    "source ids where relevant; do not be gratuitously negative.\n"
    "Apply three lenses: (a) Toulmin — for each major claim, is there a WARRANT linking the evidence to "
    "the conclusion, or is it data without a warrant? (b) Epistemic calibration — does the certainty "
    "language match the evidence (preliminary evidence must not be stated as established)? (c) "
    "Inference-to-best-explanation — is the strongest ALTERNATIVE explanation addressed?\n\n"
    "Return JSON only:\n"
    "{\n"
    '  "cherry_picking": [str],\n'
    '  "confirmation_bias": [str],\n'
    '  "missing_warrants": [str],\n'
    '  "overclaims": [str],\n'
    '  "alternative_explanations": [str],\n'
    '  "strongest_counter_argument": str,\n'
    '  "whats_missing": [str],\n'
    '  "overrated_claims": [{"claim": str, "why": str}],\n'
    '  "so_what": str\n'
    "}\n"
)


async def _critic_pre_process(request: LeafRequest, ctx: SubagentSpawnContext) -> str:
    root = run_artifact_dir(ctx.parent_agent_id, request.run_id)
    research_request = _load_request(root)
    digests, sources, claims = _load_shards(root)
    payload = {
        "question": research_request.question,
        "mode": research_request.mode,
        "scope": research_request.scope,
        "worker_digests": [
            {"topic": d["topic"], "intermediate_report": d["intermediate_report"][:20000]} for d in digests
        ],
        "sources": [
            {
                "source_id": record.source_id,
                "publisher": record.publisher,
                "evidence_tier": record.evidence_tier,
                "evidence_grade": record.evidence_grade,
            }
            for record in sources
        ],
        "claims": claims[:80],
    }
    return f"{_CRITIC_INSTRUCTION}\n{json.dumps(payload, ensure_ascii=False)}"


def _parse_json_object(text: str) -> dict | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.startswith("json"):
            candidate = candidate[4:]
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _critic_post_process(
    request: LeafRequest,
    ctx: SubagentSpawnContext,
    result: SubagentResult | None,
    outcome: LeafOutcome,
) -> LeafOutcome:
    if not outcome.ok:
        return outcome
    text = (result.content if result is not None else "") or str((outcome.output or {}).get("text") or "")
    review = _parse_json_object(text)
    if review is None:
        # RC5 equivalent: a malformed critique must not silently drop the
        # adversarial pass — fail loud so the definition-level retry re-spawns.
        return LeafOutcome(
            ok=False,
            error="devils-advocate critique was not valid JSON",
            tokens_used=outcome.tokens_used,
        )
    root = run_artifact_dir(ctx.parent_agent_id, request.run_id)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "devils_advocate.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(review, ensure_ascii=False) + "\n")
    return LeafOutcome(
        ok=True,
        output={"text": text[:4000], "devils_advocate": review},
        result_ref=outcome.result_ref,
        tokens_used=outcome.tokens_used,
    )


DEEP_RESEARCH_CRITIC_PRESET = LeafPreset(
    system_prompt=(
        "SUB-AGENT ROLE: Deep Research devils advocate. Tools are disabled; "
        "stress-test the evidence you are given. Reply with the JSON object ONLY."
    ),
    disable_tools=True,  # RC11
    pre_process=_critic_pre_process,
    post_process=_critic_post_process,
)


# ── synthesizer (RC11 + RC12 + RC13 + artifact landing) ───────────


async def _synthesizer_pre_process(request: LeafRequest, ctx: SubagentSpawnContext) -> str:
    from app.services.deep_research.evaluator import ResearchEvaluator
    from app.services.deep_research.language import resolve_output_language_label
    from app.services.deep_research.reasoner import (
        _compress_claims_for_synthesis,
        build_digest_synthesis_instruction,
    )

    root = run_artifact_dir(ctx.parent_agent_id, request.run_id)
    research_request = _load_request(root)
    digests, sources, claim_rows = _load_shards(root)
    ledger = _merged_ledger(root, sources, claim_rows)
    evaluation = ResearchEvaluator().evaluate(request=research_request, ledger=ledger, round_index=1)

    devils_advocate: dict[str, Any] = {}
    da_path = root / "devils_advocate.jsonl"
    if da_path.exists():
        lines = [line for line in da_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if lines:
            devils_advocate = json.loads(lines[-1])

    payload = {
        "question": research_request.question,
        "mode": research_request.mode,
        "scope": research_request.scope,
        "depth": research_request.depth,
        "source_policy": research_request.source_policy,
        "plan": {
            "lanes": [{"lane_id": digest["topic"], "label": digest["topic"]} for digest in digests],
        },
        "worker_digests": digests,
        "source_notes": [],
        "lane_summaries": [],
        "sources": [_source_meta(record) for record in ledger.sources.values()],
        "claims": _compress_claims_for_synthesis(ledger.claims),
        "quality_gates": evaluation.quality_gates,
        "gaps": evaluation.gaps,
        "devils_advocate": devils_advocate,
    }
    instruction = build_digest_synthesis_instruction(research_request, resolve_output_language_label(research_request))
    if devils_advocate:
        instruction += (
            "\n\nADVERSARIAL REVIEW — a Devil's Advocate critique of the evidence is in `devils_advocate`. "
            "You MUST address it: neutralise the cherry_picking and confirmation_bias items; for every "
            "missing_warrants item supply the warrant or downgrade the claim; for every overclaim recalibrate "
            "the certainty language; weigh the alternative_explanations; fold the strongest_counter_argument "
            "into `## Contradictions And Gaps`; and either close or explicitly flag every whats_missing item. "
            "Do not silently ignore it."
        )
    return f"{instruction}\n\n{json.dumps(payload, ensure_ascii=False)}"


def _workflow_coverage_notice(report: str, ledger: EvidenceLedger, digests: list[dict[str, Any]]) -> str:
    """Workflow-shape `_with_coverage_notice`: lanes are explorer topics; a
    topic whose shard contributed no usable source is named as uncovered."""
    from app.services.deep_research.orchestrator import _COVERAGE_NOTICE_MARKER

    covered = {source.lane_id for source in ledger.sources.values() if source.lane_id}
    uncovered: list[str] = []
    for digest in digests:
        shard_ids = {meta.get("lane_id") for meta in digest.get("sources", [])}
        if not (shard_ids & covered) and not digest.get("sources"):
            uncovered.append(str(digest.get("topic") or "unknown topic"))
    lines = [
        f"> {_COVERAGE_NOTICE_MARKER} Evidence was limited, so this is a narrowed report scoped to the "
        f"{len(ledger.sources)} source(s) that returned usable content."
    ]
    if uncovered:
        lines.append("> Research topics still uncovered: " + ", ".join(uncovered) + ".")
    return "\n".join(lines) + "\n\n" + report.strip() + "\n"


async def _synthesizer_post_process(
    request: LeafRequest,
    ctx: SubagentSpawnContext,
    result: SubagentResult | None,
    outcome: LeafOutcome,
) -> LeafOutcome:
    from app.services.deep_research.orchestrator import (
        _apply_footnotes,
        _evaluate_synthesis_quality,
        _narrowed_minimum_chars,
    )

    if not outcome.ok:
        return outcome
    root = run_artifact_dir(ctx.parent_agent_id, request.run_id)
    research_request = _load_request(root)
    ledger = _load_merged_ledger(root)
    digests, _, _ = _load_shards(root)

    raw_report = (result.content if result is not None else "") or str((outcome.output or {}).get("text") or "")
    # Same order as the legacy path: envelope strip + RC12 happen inside
    # _apply_footnotes, then the RC13 quality gate judges the final text.
    report = _apply_footnotes(raw_report, ledger) or ""
    quality, reason = _evaluate_synthesis_quality(report, request=research_request, ledger=ledger)
    if quality != "passed":
        # F5 narrowed fallback: honest, coverage-annotated delivery for thin
        # evidence — then the LOWER floor re-judges it. Anything still failing
        # fails the leaf loudly; the definition-level retry owns attempt #2.
        candidate = report.strip()
        if ledger.sources and candidate and len(candidate) >= _narrowed_minimum_chars(research_request):
            narrowed = _workflow_coverage_notice(candidate, ledger, digests)
            narrowed_quality, _narrowed_reason = _evaluate_synthesis_quality(
                narrowed, request=research_request, ledger=ledger
            )
            if narrowed_quality == "passed":
                report, quality, reason = narrowed, narrowed_quality, ""
        if quality != "passed":
            return LeafOutcome(
                ok=False, error=reason or "synthesis quality gate failed", tokens_used=outcome.tokens_used
            )

    report_path = root / "report.md"
    report_path.write_text(report, encoding="utf-8")
    summary = ledger.summary() if hasattr(ledger, "summary") else {}
    final_payload = {
        "question": research_request.question,
        "status": "completed",
        "quality": quality,
        "report_path": str(report_path),
        "source_count": len(ledger.sources),
        "summary": summary,
        "sources": [to_jsonable(record) for record in ledger.sources.values()],
    }
    (root / "final.json").write_text(json.dumps(final_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return LeafOutcome(
        ok=True,
        output={
            "text": report[:4000],
            "report_path": str(report_path),
            "quality": quality,
            "source_count": len(ledger.sources),
        },
        result_ref=str(report_path),
        tokens_used=outcome.tokens_used,
    )


DEEP_RESEARCH_SYNTHESIZER_PRESET = LeafPreset(
    system_prompt=(
        "SUB-AGENT ROLE: Deep Research synthesis writer. Tools are disabled — "
        "write the report from the provided digests, ledger, and critique only."
    ),
    disable_tools=True,  # RC11: the production root cause this whole family fixes
    pre_process=_synthesizer_pre_process,
    post_process=_synthesizer_post_process,
)


def register_deep_research_leaf_presets() -> None:
    """Idempotent registration of all Deep Research leaf presets."""
    register_leaf_preset("deep_research_planner", DEEP_RESEARCH_PLANNER_PRESET)
    register_leaf_preset("deep_research_explorer", DEEP_RESEARCH_EXPLORER_PRESET)
    register_leaf_preset("deep_research_critic", DEEP_RESEARCH_CRITIC_PRESET)
    register_leaf_preset("deep_research_synthesizer", DEEP_RESEARCH_SYNTHESIZER_PRESET)
