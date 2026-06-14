"""Synthesis quality gates + writer instruction (DR-6a, single source of truth).

Extracted verbatim from the retiring linear orchestrator (RC12 hallucinated-ref
neutralisation, RC13 quality gate, footnote conversion, prose density counters)
and the retiring reasoner (digest-synthesis instruction, claim compression) so
the workflow leaf presets — the ONE remaining Deep Research path — own them
without importing retired modules. Behaviour-preserving move, no edits.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.deep_research.language import (
    paragraph_language_consistency,
    resolve_output_language_code,
)
from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.prompt_craft import REASONING_CALIBRATION, WRITING_QUALITY
from app.services.deep_research.schemas import ResearchRequest, SourceType


_INLINE_SOURCE_TOKEN_RE = re.compile(
    r"\[(src_[a-zA-Z0-9_]{8,})\]"  # [src_ab12]
    r"|(?<![`\-])(src_[a-zA-Z0-9_]{8,})\b"  # bare src_ab12 in prose
)


def _apply_footnotes(report: str | None, ledger: EvidenceLedger) -> str | None:
    """Tier 2-5: rewrite inline [src_xxx] / bare src_xxx prose references as [^N]
    footnote markers and append a `## Footnotes` block at the end of the report.
    Backtick-quoted `src_xxx` entries (used inside `## Source Ledger`) are left intact
    so the ledger keeps its native form.
    """
    # Task3: strip a hallucinated tool-call envelope first — regardless of ledger
    # state — so report.md is always clean markdown, not a raw `<FileWriter ...>` blob.
    report = _strip_tool_call_envelope(report)
    if not report or not ledger.sources:
        return report

    # RC12: neutralize hallucinated citations (ids not in the ledger) before footnote
    # conversion, so a few fabricated refs do not one-vote-veto an otherwise grounded report.
    report = _strip_unknown_source_refs(report, ledger)

    used: dict[str, int] = {}

    def _replace(match: re.Match) -> str:
        sid = match.group(1) or match.group(2)
        if sid not in ledger.sources:
            return match.group(0)
        if sid not in used:
            used[sid] = len(used) + 1
        return f"[^{used[sid]}]"

    converted = _INLINE_SOURCE_TOKEN_RE.sub(_replace, report)

    if not used:
        return converted

    if "## Footnotes" in converted:
        return converted

    lines = ["", "## Footnotes", ""]
    for sid, num in sorted(used.items(), key=lambda kv: kv[1]):
        source = ledger.sources[sid]
        title = source.title or "Source"
        publisher = source.publisher or "Unknown publisher"
        url = source.url or ""
        lines.append(f"[^{num}]: {title} — {publisher} — {url}")
    return converted.rstrip() + "\n\n" + "\n".join(lines) + "\n"


_SOURCE_REF_RE = re.compile(r"\[src_[a-zA-Z0-9_]+\]|`src_[a-zA-Z0-9_]+`|\bsrc_[a-zA-Z0-9_]+")
_FOOTNOTE_REF_RE = re.compile(r"\[\^?\d+\]")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s+.*$", re.MULTILINE)
_TABLE_DIVIDER_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$", re.MULTILINE)

_PROSE_PROPER_RE = re.compile(
    r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]*)+\b"  # Multi-word Title Case (Issuer A, Federal Reserve)
    r"|\b[A-Z][a-z]+[A-Z][a-zA-Z]+\b"  # CamelCase (BlackRock, JPMorgan)
    r"|\b[A-Z]{2,6}\b"  # Acronyms (SEC, MAS, BUIDL)
)
_PROSE_INNER_TITLECASE_RE = re.compile(r"(?<=[a-z][\s,])[A-Z][a-z]{2,}\b")
_ZH_ENTITY_RE = re.compile(
    r"[一-鿿]{2,8}(?:公司|集团|局|委员会|银行|协会|交易所|证监会|央行|部|院|大学|证券|基金|保险)"
)


def _evaluate_synthesis_quality(
    report: str | None, *, request: ResearchRequest, ledger: EvidenceLedger
) -> tuple[str, str]:
    if not report:
        return "failed", "Synthesis quality failed: report is too short for a deep research deliverable."
    if not ledger.sources:
        return "failed", "Synthesis quality failed: no fetched source is available for source-grounded synthesis."
    target_language = resolve_output_language_code(request)
    # F5: a narrowed report (limited evidence + explicit coverage notice) is held to a lower floor
    # and skips the digit/entity density gates that assume full-coverage synthesis.
    is_narrowed = _COVERAGE_NOTICE_MARKER in report
    language_ok, foreign_paragraphs = paragraph_language_consistency(report, target_language)
    if not language_ok:
        return (
            "failed",
            (
                f"Synthesis quality failed: report mixes languages — {foreign_paragraphs} paragraph(s) are not in "
                f"the target output language ({target_language}). Rewrite the whole report in one language."
            ),
        )
    unknown_refs = _unknown_source_refs(report, ledger)
    if unknown_refs:
        return (
            "failed",
            "Synthesis quality failed: report cites unknown source ids not in the evidence ledger: "
            + ", ".join(unknown_refs[:8]),
        )
    floor = _narrowed_minimum_chars(request) if is_narrowed else _minimum_report_chars(request)
    if len(report.strip()) < floor:
        return "failed", "Synthesis quality failed: report is too short for a deep research deliverable."
    cited_source_ids = {source_id for source_id in ledger.sources if source_id in report}
    footnote_markers = len(re.findall(r"\[\^\d+\]", report))
    required_citations = min(max(2, len(ledger.sources) // 2), len(ledger.sources))
    # Tier 2-5: footnote markers count as citations too — the synthesis path now rewrites
    # inline [src_xxx] to [^N] and emits a Footnotes table.
    if max(len(cited_source_ids), footnote_markers) < required_citations:
        return (
            "failed",
            "Synthesis quality failed: report does not cite enough source ids or footnotes from the evidence ledger.",
        )
    required_sections = ("Executive", "Findings", "Source")
    if not all(section.casefold() in report.casefold() for section in required_sections):
        return "failed", "Synthesis quality failed: report is missing executive, findings, or source-grounded sections."
    if request.mode != "source_ledger_audit" and _looks_like_evidence_list_dump(report):
        return "failed", "Synthesis quality failed: report is an evidence-list dump, not analytical writing."
    if _looks_like_generic_summary(report):
        return "failed", "Synthesis quality failed: report is generic and lacks concrete source-grounded analysis."

    digit_count = _prose_digit_count(report)
    required_digits = _required_digit_count(request)
    if not is_narrowed and digit_count < required_digits:
        return (
            "failed",
            (
                f"Synthesis quality failed: report has only {digit_count} concrete numbers in prose; "
                f"deep research at mode={request.mode}/depth={request.depth or 'standard'} requires at least {required_digits}."
            ),
        )

    if not is_narrowed and request.mode != "source_ledger_audit":
        entity_count = _named_entity_count(report)
        required_entities = _required_entity_count(request)
        if entity_count < required_entities:
            return (
                "failed",
                (
                    f"Synthesis quality failed: report references only {entity_count} named entities (companies, "
                    f"regulators, products); analyst-grade synthesis requires at least {required_entities} for "
                    f"mode={request.mode}."
                ),
            )

    return "passed", ""


def _unknown_source_refs(report: str, ledger: EvidenceLedger) -> list[str]:
    refs = set(re.findall(r"src_[a-zA-Z0-9_]+", report or ""))
    return sorted(ref for ref in refs if ref not in ledger.sources)


def _strip_unknown_source_refs(report: str, ledger: EvidenceLedger) -> str:
    """RC12: neutralize hallucinated citations — remove [src_xxx] / `src_xxx` / bare src_xxx
    tokens whose id is not in the evidence ledger (model-fabricated refs), keeping the prose.
    A few invented ids then no longer fail an otherwise source-grounded report; genuine ledger
    refs are untouched (later converted to footnotes). If too few real refs remain, the
    citation-sufficiency gate still fails the report downstream."""
    unknown = {ref for ref in re.findall(r"src_[a-zA-Z0-9_]+", report or "") if ref not in ledger.sources}
    if not unknown:
        return report
    cleaned = report
    for ref in unknown:
        cleaned = re.sub(rf"\[\s*{re.escape(ref)}\s*\]", "", cleaned)  # [src_xxx]
        cleaned = cleaned.replace(f"`{ref}`", "")  # `src_xxx`
        cleaned = re.sub(rf"\b{re.escape(ref)}\b", "", cleaned)  # bare src_xxx
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)  # collapse doubled spaces left by removal
    cleaned = re.sub(r"\s+([.,;)])", r"\1", cleaned)  # tidy orphaned space before punctuation
    return cleaned


def _strip_tool_call_envelope(report: str | None) -> str | None:
    """Task3: neutralize a hallucinated tool-call envelope wrapping the report.

    With tools disabled (RC11) the synthesis LLM cannot emit a real write_file call,
    but some models still wrap the whole report in a *textual* pseudo tool-call, e.g.
    `<FileWriter path="..." content="# Title ...">` (f733867 left it unterminated, so
    the body ran to EOF). That raw string was persisted verbatim as report.md. A real
    report always opens with a markdown H1 (`# <title>` per the synthesis contract) and
    never with `<`, so this only fires on the malformed case and recovers the markdown.
    """
    if not report:
        return report
    stripped = report.lstrip()
    if not stripped.startswith("<"):
        return report  # normal markdown — leave untouched

    # Form A (observed): a content="..." attribute holds the markdown, often unterminated.
    attr = re.match(r'<[^>]*?\bcontent\s*=\s*"(?P<body>.*)$', stripped, re.DOTALL)
    if attr and attr.group("body").strip():
        return re.sub(r'"\s*/?>?\s*$', "", attr.group("body")).strip()

    # Form B: markdown wrapped between tags — recover from the first H1, drop close tag.
    h1 = re.search(r"(?m)^#\s+\S", stripped)
    if h1:
        return re.sub(r"</[A-Za-z_][\w.\-]*>\s*$", "", stripped[h1.start() :]).strip()

    return report


def _minimum_report_chars(request: ResearchRequest) -> int:
    depth = (request.depth or "").strip().lower()
    if depth in {"full", "flagship", "deep"}:
        return 1200
    if depth in {"quick", "light"}:
        return 700
    return 900


_COVERAGE_NOTICE_MARKER = "**Coverage notice:**"


def _narrowed_minimum_chars(request: ResearchRequest) -> int:
    """F5: a narrowed (coverage-limited) report is held to a lower floor than a full report —
    honestly reporting limited evidence should not be punished as an outright failure."""
    return max(400, _minimum_report_chars(request) // 3)


def _with_coverage_notice(report: str, plan, ledger: EvidenceLedger) -> str:
    """Prefix a narrowed report with an explicit coverage notice naming the lanes that still have
    no usable source (F5/RC4). Keeps the run honest: partial coverage is stated, not hidden."""
    covered = {source.lane_id for source in ledger.sources.values() if source.lane_id}
    uncovered = [lane.label or lane.lane_id for lane in plan.lanes if lane.lane_id not in covered]
    lines = [
        f"> {_COVERAGE_NOTICE_MARKER} Evidence was limited, so this is a narrowed report scoped to the "
        f"{len(ledger.sources)} source(s) that returned usable content."
    ]
    if uncovered:
        lines.append("> Research lanes still uncovered: " + ", ".join(uncovered) + ".")
    return "\n".join(lines) + "\n\n" + report.strip() + "\n"


def _required_digit_count(request: ResearchRequest) -> int:
    """Mode/depth-aware concrete-number threshold. source_ledger_audit relaxes since
    audit reports center on provenance, not market quantification."""
    if request.mode == "source_ledger_audit":
        return 8
    depth = (request.depth or "").strip().lower()
    if depth in {"full", "flagship", "deep"}:
        return 20
    if depth in {"quick", "light"}:
        return 8
    return 12


def _required_entity_count(request: ResearchRequest) -> int:
    """Mode-aware named-entity threshold. topic_deep_dive tolerates narrower coverage
    than industry_research, but both demand concrete actors."""
    if request.mode == "industry_research":
        return 8
    return 6


def _strip_for_prose(report: str) -> str:
    body = _SOURCE_REF_RE.sub("", report)
    body = _FOOTNOTE_REF_RE.sub("", body)
    body = _CODE_FENCE_RE.sub("", body)
    body = _HEADING_RE.sub("", body)
    body = _TABLE_DIVIDER_RE.sub("", body)
    return body


def _prose_digit_count(report: str) -> int:
    return sum(1 for ch in _strip_for_prose(report) if ch.isdigit())


def _named_entity_count(report: str) -> int:
    body = _strip_for_prose(report)
    entities: set[str] = set()
    entities.update(_PROSE_PROPER_RE.findall(body))
    entities.update(_PROSE_INNER_TITLECASE_RE.findall(body))
    entities.update(_ZH_ENTITY_RE.findall(body))
    return len(entities)


def _looks_like_evidence_list_dump(report: str) -> bool:
    """Heuristic: many ledger lines of the form "- `src_xxx`" paired with very few H2
    sections signals a pasted evidence list, not analytical writing."""
    ledger_lines = report.count("\n- `src_")
    section_count = report.count("\n## ")
    return ledger_lines >= 3 and section_count <= 5


def _looks_like_generic_summary(report: str) -> bool:
    lowered = " ".join(report.casefold().split())
    generic_phrases = (
        "big opportunity",
        "follow compliance",
        "manage risks",
        "early stage",
        "important trend",
    )
    return len(report) < 1800 and sum(1 for phrase in generic_phrases if phrase in lowered) >= 2


def build_digest_synthesis_instruction(request: ResearchRequest, language: str) -> str:
    """Final-report synthesis instruction — the anti-stitch DNA: integration, not summarization.

    Domain-general, borrowed from academic-research-skills `synthesis_agent`: explicit
    anti-patterns (sequential summarization / cherry-picking / unresolved contradictions),
    a convergence -> divergence -> resolution -> gap process, and evidence-weighted writing.
    """
    return (
        f"Write the FINAL Deep Research report in {language}. Translate all evidence into {language}; "
        "keep proper names, tickers, and identifiers in their original form. Never mix languages.\n\n"
        "CORE PRINCIPLE — INTEGRATION, NOT SUMMARIZATION. You are writing ONE coherent analyst report from "
        "multiple worker digests. Do NOT stitch, concatenate, or list the digests one after another. The worker "
        "digests are your EVIDENCE; the report is your ARGUMENT — it must carry a through-line thesis across "
        "dimensions, not place per-worker summaries side by side.\n"
        "Priority when these collide: (1) INTEGRATION — a through-line thesis carried across dimensions plus a "
        "clear so-what; (2) COVERAGE — every dimension represented; (3) DEPTH within any one dimension. Coverage "
        "is the floor, not the spine: never drop a dimension, but never let 'cover every dimension' degrade into "
        "'dimension 1 found X, dimension 2 found Y'.\n"
        "Forbidden patterns (reject your own draft if it does any of these):\n"
        "- Sequential summarization: 'Worker 1 found X. Worker 2 found Y.' Instead integrate across sources: "
        "'Converging evidence establishes X, operating through mechanism Y, though Z moderates it when ...'.\n"
        "- Worker-by-worker or source-by-source structure: never organize the final report as one block per worker, "
        "one block per lane, or one paragraph per source. That is a 资料汇编 / evidence packet, not research.\n"
        "- Cherry-picking: do not report only confirming evidence; surface disconfirming evidence and weigh it.\n"
        "- Unresolved contradictions: when sources disagree, resolve by comparing evidence quality, recency, and "
        "scope — or explicitly flag the disagreement as irreconcilable.\n"
        "- Covered but spineless: if a reader cannot restate your central judgment and what to DO about it, you "
        "have catalogued the dimensions, not analyzed them.\n\n"
        "Method before writing: first build an internal synthesis brief, then write. The brief must include: "
        "(1) CENTRAL THESIS; (2) EVIDENCE MATRIX mapping each major claim to source ids, evidence tier/grade, "
        "and the WARRANT explaining why the evidence supports the claim; (3) CONTRADICTION MAP with the strongest "
        "counter-evidence and how you resolve or downgrade it; (4) IMPLICATION MAP answering the so-what / what "
        "decision the evidence supports; (5) REPORT OUTLINE. Do not print the brief as JSON; use it to write the "
        "markdown report. Then write an integrated argument that leads with the strongest, best-supported themes "
        "and weights claims by evidence quality (primary/authoritative > strong secondary > press > weak).\n\n"
        "Hard requirements:\n"
        "- Weight by the per-source `evidence_tier`/`evidence_grade`: tier1 (primary/authoritative) and tier2 "
        "(strong secondary) carry the argument; a tier4 (blog/social) source must NOT be the sole support for any "
        "key claim — corroborate it or mark the claim inferred.\n"
        "- Cite ONLY source ids present in `sources` / worker digest source metadata; never invent ids. Every "
        "material claim cites a source id inline, e.g. [src_ab12].\n"
        "- Sections in order: `# <specific title>`, `## Executive Thesis` (state the central judgment up front), "
        "`## Method And Source Standard`, `## Cross-Cutting Analysis` (THE analytical core — organize by the 2-4 "
        "tensions/through-lines from step 5, NOT by worker; each subsection pulls evidence from MULTIPLE dimensions "
        "to advance the thesis), `## Key Findings` (the per-dimension evidence base — see coverage rule), "
        "`## Contradictions And Gaps`, `## Strategic Implications` (the so-what — what the findings MEAN and which "
        "decision they support, not a recap), `## Source Ledger`; add mode-specific sections when useful.\n"
        "- COVERAGE IS MANDATORY (the floor): `## Key Findings` must contain a distinct `###` subsection for EVERY "
        "research dimension present in the worker digests (one per worker topic/lane). Never collapse or silently "
        "drop a dimension — a thin dimension gets a short honest subsection, not omission. But these subsections "
        "are the EVIDENCE BASE supporting the thesis from `## Cross-Cutting Analysis`, not the report's spine. "
        "Integrate sources WITHIN each subsection (the no-stitching rule still holds).\n"
        "- `## Cross-Cutting Analysis` and `## Strategic Implications` must deliver JUDGMENT — what the evidence "
        "means, the trade-offs, the so-what — not restate the per-dimension findings.\n"
        "- Use concrete numbers, named entities, dates, and mechanisms. Separate verified findings from inferred "
        "implications and gaps.\n"
        "- If evidence is insufficient, say so explicitly instead of padding with generic prose.\n"
        "- OUTPUT FORMAT: return ONLY the markdown report, starting with `# `. Do NOT wrap it in a "
        'tool call or any XML/JSON envelope (no `<FileWriter>`, `<write_file>`, `content="..."`, or '
        "similar) — tools are disabled, so any such wrapper is persisted verbatim as garbage.\n\n"
        + _depth_expectation(request)
        + "\n\n"
        + REASONING_CALIBRATION
        + "\n\n"
        + WRITING_QUALITY
    )


def _source_types(value: Any) -> list[SourceType]:
    if not isinstance(value, list):
        return []
    parsed: list[SourceType] = []
    for item in value:
        try:
            parsed.append(SourceType(str(item)))
        except ValueError:
            continue
    return parsed


def _compress_claims_for_synthesis(claims: list, *, limit: int = 200) -> list[dict[str, Any]]:
    """Trim the claim ledger to its load-bearing fields for the synthesis payload.

    Workers already digested the evidence; the writer needs each claim's text +
    source ids + status, not the extraction evidence/notes. Contradictions are
    surfaced first because the report must resolve them.

    Task2: RC10 originally capped this at 60 on the theory that a 128K claim payload
    overflowed the writer and collapsed its output. RC11 later proved the real cause
    was tool exposure, not payload size — so the aggressive cap was over-fitting a
    misdiagnosis. With output budget restored (Task1) and long-context models, 200
    keeps effectively the whole ledger while still bounding a pathological run.
    """
    prioritized = sorted(claims, key=lambda c: 0 if getattr(c, "contradiction_group", None) else 1)
    compressed: list[dict[str, Any]] = []
    for claim in prioritized[:limit]:
        entry: dict[str, Any] = {
            "claim_id": getattr(claim, "claim_id", ""),
            "text": getattr(claim, "text", ""),
            "source_ids": list(getattr(claim, "source_ids", []) or []),
            "status": str(getattr(claim, "status", "")),
        }
        group = getattr(claim, "contradiction_group", None)
        if group:
            entry["contradiction_group"] = group
        compressed.append(entry)
    return compressed


def _depth_expectation(request: ResearchRequest) -> str:
    """Depth-aware report-scale expectation (F4/RC4).

    The production run failed synthesis twice below the 1200-char floor partly because the
    anti-padding guidance pushed the writer too terse. This makes the depth contract explicit:
    full depth = more substantiated content, not more filler.
    """
    depth = (request.depth or "standard").strip().lower()
    if depth in {"full", "flagship", "deep"}:
        return (
            "DEPTH EXPECTATION — this is a FULL-depth report. Develop every major section into a thorough, "
            "multi-section analyst report grounded in concrete evidence; go well beyond a skeleton or a short "
            "brief. Depth means MORE substantiated content (specific numbers, named entities, mechanisms, and "
            "resolved contradictions) — it is NOT a licence for filler or padding. Each paragraph must earn its place."
        )
    if depth in {"quick", "light"}:
        return (
            "DEPTH EXPECTATION — this is a QUICK-depth report. Be concise: lead with the most decision-relevant "
            "findings as a tight, well-supported brief."
        )
    return (
        "DEPTH EXPECTATION — this is a STANDARD-depth report. Develop each section with concrete evidence into a "
        "complete analyst brief, not a skeleton."
    )


# Task1: the final report must not truncate. A full analyst report (every worker
# dimension + cross-cutting analysis + so-what + ledger) runs well past a model's
# chat-default output ceiling — f733867 capped near 9K chars, one dimension short
# of its mandated Contradictions/Ledger sections. 32768 is a report-grade budget
# modern long-context models support; get_max_tokens still clamps it to the
# provider's per-provider output ceiling (ProviderSpec.max_output_tokens, bounded
# by the global ABSOLUTE_MAX_OUTPUT_TOKENS).
