"""Deprecated wiki-page curator for derived/compat concept pages.

Accepted T3 truth is now restricted to ``memory/t3/episodes.md``,
``user.md``, ``worker.md``, and ``capabilities.md``. Wiki pages under
``memory/wiki/<concept>.md`` are derived/compat read-model pages only; the live
curation lane is T3 Consolidator -> Memory Gate -> Platform Gate.

When used by migration/eval paths, a wiki page holds a derived view of semantic
claims:

    ## Current Claim / ## Scope / ## Evidence / ## Contradictions
    / ## Changes / ## Retrieval Tags

- LLM primary (injected async callable) drafts the page patch from evidence
  entries plus the existing page.
- Mechanical support: section-schema validation, low-confidence claim guard
  (a weak claim change is held, never silently overwrites Current Claim),
  privacy gate on apply.
- Candidates first: :func:`curate_wiki_page` emits a
  :class:`WikiPatchCandidate`; :func:`apply_wiki_patch` is the governed
  write. Holds always leave a distillation-audit record.

No graph database, KG, or PPR — `[[wikilinks]]` stay plain Markdown.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.memory.distillation_audit import write_distillation_audit

logger = logging.getLogger(__name__)

LlmFn = Callable[[str, str], Awaitable[str]]

# Below this confidence a claim upsert is a semantic risk: hold it and let
# evidence accumulate (spec §4 wiki consolidation failure handling).
_CLAIM_CONFIDENCE_FLOOR = 0.5

REQUIRED_SECTIONS = (
    "## Current Claim",
    "## Scope",
    "## Evidence",
    "## Contradictions",
    "## Changes",
    "## Retrieval Tags",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_WIKI_SYSTEM_PROMPT = """\
<role>
You are the wiki curator of an agent's memory engine. You consolidate
evidence entries into a concept page — the agent's current semantic claim
about one concept, stored as Markdown under memory/wiki/. You emit a PATCH
CANDIDATE; the Memory Control Plane applies accepted patches. You never
write files.
</role>

<page_contract>
The page must contain ALL of these sections, in this order, after a YAML
frontmatter block (title/type/tags/status):
## Current Claim — one crisp, current claim
## Scope — where the claim applies (and where it does not)
## Evidence — `- [category] content #tag` lines with source attribution
## Contradictions — open conflicts; never silently drop them
## Changes — dated change log, append-only
## Retrieval Tags — comma-separated retrieval keywords
</page_contract>

<claim_safety>
When new evidence CONTRADICTS the existing Current Claim and your confidence
is not high, do NOT rewrite the claim — add the evidence under
## Contradictions and keep the existing claim, or answer action="hold" with
the reason. Overwriting a claim on weak evidence corrupts semantic memory.
Inline [[wikilinks]] to related concepts are allowed.
</claim_safety>

<output_contract>
Return EXACTLY ONE raw JSON object, no fences, no prose:
{
  "action": "upsert|hold",
  "concept_slug": "<kebab-case-slug, empty when hold>",
  "page_markdown": "<FULL page content with frontmatter and all six sections; empty when hold>",
  "reason": "<why>",
  "confidence": 0.0
}
</output_contract>
"""


@dataclass(slots=True)
class WikiPatchCandidate:
    status: str  # proposed | held
    action: str  # upsert | hold
    page_path: str
    page_markdown: str
    source_refs: list[str]
    reason: str
    confidence: float
    concept: str = ""


def wiki_dir(data_root: Path, agent_id: uuid.UUID) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / "wiki"


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return slug[:80] or "concept"


def _resolve_candidate_wiki_path(data_root: Path, agent_id: uuid.UUID, page_path: str) -> Path | None:
    prefix = "memory/wiki/"
    normalized = str(page_path or "").replace("\\", "/")
    if not normalized.startswith(prefix):
        return None
    relative = normalized.removeprefix(prefix)
    rel_path = Path(relative)
    if rel_path.is_absolute() or ".." in rel_path.parts or len(rel_path.parts) != 1 or rel_path.suffix.lower() != ".md":
        return None
    base = wiki_dir(data_root, agent_id).resolve()
    target = (base / rel_path).resolve()
    if base not in target.parents:
        return None
    return target


def _held(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    concept: str,
    reason: str,
    source_refs: list[str],
    detail: dict | None = None,
) -> WikiPatchCandidate:
    write_distillation_audit(
        data_root,
        agent_id,
        stage="wiki_curation",
        outcome="held",
        reason=reason,
        detail={"concept": concept, **(detail or {})},
    )
    return WikiPatchCandidate(
        status="held",
        action="hold",
        page_path="",
        page_markdown="",
        source_refs=source_refs,
        reason=reason,
        confidence=0.0,
        concept=concept,
    )


async def curate_wiki_page(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    concept: str,
    evidence_entries: list[dict],
    llm: LlmFn | None,
) -> WikiPatchCandidate:
    """Draft a wiki page patch for one concept from evidence entries.

    The LLM owns claim synthesis; mechanical code provides full input
    visibility (existing page + evidence) and validates the six-section
    schema plus the claim-confidence floor. Failure paths hold with audit.
    """
    source_refs = [
        str(e.get("source_ref") or "").strip() for e in evidence_entries if str(e.get("source_ref") or "").strip()
    ]
    if not concept.strip():
        return _held(data_root, agent_id, concept=concept, reason="empty concept", source_refs=source_refs)
    if llm is None:
        return _held(
            data_root,
            agent_id,
            concept=concept,
            reason="no LLM adjudicator available for wiki consolidation",
            source_refs=source_refs,
        )

    slug = _slugify(concept)
    existing_path = wiki_dir(data_root, agent_id) / f"{slug}.md"
    existing = (
        existing_path.read_text(encoding="utf-8", errors="replace") if existing_path.exists() else "(no page yet)"
    )

    evidence_lines = "\n".join(f"- [{e.get('source_ref') or '-'}] {e.get('content') or ''}" for e in evidence_entries)
    user_prompt = (
        f"<concept>{concept}</concept>\n\n"
        f"<existing_page>\n{existing}\n</existing_page>\n\n"
        f"<evidence_entries>\n{evidence_lines or '(none)'}\n</evidence_entries>\n\n"
        "Produce the full updated page, or hold."
    )

    try:
        raw = await llm(_WIKI_SYSTEM_PROMPT, user_prompt)
        payload = json.loads(_extract_json(raw))
    except Exception as exc:  # noqa: BLE001 — any LLM/parse failure must hold, not corrupt the page
        return _held(
            data_root,
            agent_id,
            concept=concept,
            reason=f"wiki LLM output invalid ({type(exc).__name__})",
            source_refs=source_refs,
            detail={"error": str(exc)[:500]},
        )

    action = str(payload.get("action") or "").strip().lower()
    reason = str(payload.get("reason") or "")
    confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
    if action != "upsert":
        return _held(
            data_root, agent_id, concept=concept, reason=reason or "wiki curator held", source_refs=source_refs
        )

    page_markdown = str(payload.get("page_markdown") or "").strip()
    missing = [section for section in REQUIRED_SECTIONS if section not in page_markdown]
    if missing:
        return _held(
            data_root,
            agent_id,
            concept=concept,
            reason=f"page patch missing required sections: {', '.join(missing)}",
            source_refs=source_refs,
        )

    if confidence < _CLAIM_CONFIDENCE_FLOOR:
        return _held(
            data_root,
            agent_id,
            concept=concept,
            reason=(
                f"claim confidence {confidence:.2f} below floor {_CLAIM_CONFIDENCE_FLOOR} — "
                "evidence recorded for audit; existing claim untouched"
            ),
            source_refs=source_refs,
            detail={"proposed_reason": reason},
        )

    final_slug = _slugify(str(payload.get("concept_slug") or slug))
    return WikiPatchCandidate(
        status="proposed",
        action="upsert",
        page_path=f"memory/wiki/{final_slug}.md",
        page_markdown=page_markdown,
        source_refs=source_refs,
        reason=reason,
        confidence=confidence,
        concept=concept,
    )


def apply_wiki_patch(data_root: Path, agent_id: uuid.UUID, candidate: WikiPatchCandidate) -> dict:
    """Governed write: land an accepted wiki patch under memory/wiki/."""
    if candidate.status != "proposed" or candidate.action != "upsert":
        write_distillation_audit(
            data_root,
            agent_id,
            stage="wiki_apply",
            outcome="refused",
            reason=f"candidate not applicable (status={candidate.status}, action={candidate.action})",
            detail={"concept": candidate.concept},
        )
        return {"applied": False, "reason": "candidate not applicable"}

    from app.services.privacy_layer import PrivacyLayer

    privacy = PrivacyLayer().classify_and_mask(candidate.page_markdown)
    if privacy.rejected:
        write_distillation_audit(
            data_root,
            agent_id,
            stage="wiki_apply",
            outcome="rejected",
            reason=f"privacy gate: {privacy.reason}",
            detail={"page_path": candidate.page_path},
        )
        return {"applied": False, "reason": f"privacy gate: {privacy.reason}"}

    target = _resolve_candidate_wiki_path(data_root, agent_id, candidate.page_path)
    if target is None:
        write_distillation_audit(
            data_root,
            agent_id,
            stage="wiki_apply",
            outcome="refused",
            reason="invalid wiki path",
            detail={"page_path": candidate.page_path, "concept": candidate.concept},
        )
        return {"applied": False, "reason": "invalid wiki path"}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(privacy.sanitized_text.rstrip() + "\n", encoding="utf-8")
    write_distillation_audit(
        data_root,
        agent_id,
        stage="wiki_apply",
        outcome="applied",
        reason=candidate.reason,
        detail={"page_path": candidate.page_path, "concept": candidate.concept},
    )
    return {"applied": True, "path": str(target)}


def _extract_json(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().removesuffix("```").rstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in LLM output")
    return text[start : end + 1]
