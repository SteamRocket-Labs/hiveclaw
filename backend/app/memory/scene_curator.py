"""Scene curator — episode/scene consolidation (spec §4.4 / §12 P5 MVP).

Scenes live as Markdown under ``memory/scenes/<slug>.md``. The curator is the
first concrete home for the episode-consolidation lifecycle stage:

- LLM primary (injected async callable): decides update vs create vs hold,
  with anti-proliferation bias (prefer updating an existing scene).
- Mechanical support: similar-scene retrieval, schema validation, capacity
  signals, privacy gate on apply.
- Candidates first: :func:`curate_scene` only emits a
  :class:`ScenePatchCandidate`; :func:`apply_scene_patch` is the governed
  write that lands accepted patches. Ambiguity (LLM hold, missing LLM,
  invalid output) becomes a HELD candidate with a distillation-audit record.

No graph database, KG, or PPR — plain Markdown plus sidecars.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.memory.distillation_audit import write_distillation_audit
from app.memory.md_store import jaccard_similarity

logger = logging.getLogger(__name__)

LlmFn = Callable[[str, str], Awaitable[str]]

_SCENE_CAPACITY_CAP = 50  # anti-proliferation: force merge pressure past this
_SIMILAR_SCENE_THRESHOLD = 0.25
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_SCENE_SYSTEM_PROMPT = """\
<role>
You are the scene curator of an agent's memory engine. You consolidate atom
evidence (T2/T3 entries) into scene pages — coherent narrative episodes
stored as Markdown under memory/scenes/. You emit a PATCH CANDIDATE; the
Memory Control Plane applies accepted patches. You never write files.
</role>

<anti_proliferation>
Default to UPDATING an existing scene. Create a new scene only when no
existing scene covers the subject. When capacity pressure is flagged, prefer
merging into existing scenes. Preserve the evolution trail: keep a
`## Changes` section and append, never erase history. Produce a coherent
narrative, not a bullet dump.
</anti_proliferation>

<hold_rule>
When duplicate-vs-contradiction is ambiguous, when the atoms don't clearly
belong to one scene, or when you are not confident, answer action="hold"
with the reason. A held candidate is a correct outcome — never force a
low-confidence rewrite of existing Markdown.
</hold_rule>

<output_contract>
Return EXACTLY ONE raw JSON object, no fences, no prose:
{
  "action": "create|update|hold",
  "scene_slug": "<kebab-case-slug, empty when hold>",
  "title": "<scene title, empty when hold>",
  "patch_markdown": "<FULL new page content with frontmatter (title/type/tags/status), ## Narrative, ## Evidence, ## Changes; empty when hold>",
  "reason": "<why this action>",
  "confidence": 0.0
}
</output_contract>
"""


@dataclass(slots=True)
class ScenePatchCandidate:
    status: str  # proposed | held
    action: str  # create | update | hold
    scene_path: str
    patch_markdown: str
    source_refs: list[str]
    reason: str
    confidence: float
    similar_scenes: list[str] = field(default_factory=list)


def scenes_dir(data_root: Path, agent_id: uuid.UUID) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / "scenes"


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return slug[:80] or "scene"


def _list_scenes(data_root: Path, agent_id: uuid.UUID) -> dict[str, str]:
    root = scenes_dir(data_root, agent_id)
    if not root.exists():
        return {}
    pages: dict[str, str] = {}
    for path in sorted(root.glob("*.md")):
        try:
            pages[path.name] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return pages


def find_similar_scenes(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    text: str,
    limit: int = 3,
) -> list[tuple[str, float]]:
    """Rank existing scenes by lexical similarity to the atom batch."""
    hits: list[tuple[str, float]] = []
    for name, body in _list_scenes(data_root, agent_id).items():
        score = jaccard_similarity(text, body)
        if score >= _SIMILAR_SCENE_THRESHOLD:
            hits.append((name, round(score, 3)))
    hits.sort(key=lambda item: item[1], reverse=True)
    return hits[:limit]


def _held(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    reason: str,
    source_refs: list[str],
    detail: dict | None = None,
) -> ScenePatchCandidate:
    write_distillation_audit(
        data_root,
        agent_id,
        stage="scene_curation",
        outcome="held",
        reason=reason,
        detail=detail,
    )
    return ScenePatchCandidate(
        status="held",
        action="hold",
        scene_path="",
        patch_markdown="",
        source_refs=source_refs,
        reason=reason,
        confidence=0.0,
    )


async def curate_scene(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    atoms: list[dict],
    llm: LlmFn | None,
) -> ScenePatchCandidate:
    """Consolidate an atom batch into one scene patch candidate.

    The LLM owns the semantic decision (update/create/hold). Mechanical code
    builds full-visibility context (atoms + similar scenes + capacity) and
    validates the output schema. No LLM, broken output, or an explicit hold
    all produce a HELD candidate with an audit record — never a silent write.
    """
    source_refs = [str(a.get("source_ref") or "").strip() for a in atoms if str(a.get("source_ref") or "").strip()]
    atom_text = "\n".join(str(a.get("content") or "") for a in atoms).strip()
    if not atom_text:
        return _held(data_root, agent_id, reason="no atom content to consolidate", source_refs=source_refs)

    if llm is None:
        return _held(
            data_root,
            agent_id,
            reason="no LLM adjudicator available for scene consolidation",
            source_refs=source_refs,
        )

    similar = find_similar_scenes(data_root, agent_id, text=atom_text)
    pages = _list_scenes(data_root, agent_id)
    similar_blocks = "\n\n".join(f"### {name} (similarity={score})\n{pages.get(name, '')}" for name, score in similar)
    capacity_note = (
        f"CAPACITY PRESSURE: {len(pages)} scenes ≥ cap {_SCENE_CAPACITY_CAP} — merge into existing scenes."
        if len(pages) >= _SCENE_CAPACITY_CAP
        else f"{len(pages)} existing scenes (cap {_SCENE_CAPACITY_CAP})."
    )

    atom_lines = "\n".join(f"- [{a.get('source_ref') or '-'}] {a.get('content') or ''}" for a in atoms)
    user_prompt = (
        f"<atoms>\n{atom_lines}\n</atoms>\n\n"
        f"<similar_scenes>\n{similar_blocks or '(none)'}\n</similar_scenes>\n\n"
        f"<capacity>\n{capacity_note}\n</capacity>\n\n"
        "Decide: update an existing scene, create a new one, or hold."
    )

    try:
        raw = await llm(_SCENE_SYSTEM_PROMPT, user_prompt)
        payload = json.loads(_extract_json(raw))
    except Exception as exc:  # noqa: BLE001 — any LLM/parse failure must hold, not corrupt scenes
        return _held(
            data_root,
            agent_id,
            reason=f"scene LLM output invalid ({type(exc).__name__})",
            source_refs=source_refs,
        )

    action = str(payload.get("action") or "").strip().lower()
    if action == "hold" or action not in {"create", "update"}:
        return _held(
            data_root,
            agent_id,
            reason=str(payload.get("reason") or "scene curator held the patch"),
            source_refs=source_refs,
            detail={"action": action},
        )

    slug = _slugify(str(payload.get("scene_slug") or payload.get("title") or ""))
    patch_markdown = str(payload.get("patch_markdown") or "").strip()
    if not patch_markdown or not patch_markdown.startswith("---"):
        return _held(
            data_root,
            agent_id,
            reason="scene patch missing frontmatter",
            source_refs=source_refs,
            detail={"action": action, "slug": slug},
        )

    target = scenes_dir(data_root, agent_id) / f"{slug}.md"
    if action == "update" and not target.exists():
        action = "create"  # updating a non-existent page is a create in practice

    return ScenePatchCandidate(
        status="proposed",
        action=action,
        scene_path=f"memory/scenes/{slug}.md",
        patch_markdown=patch_markdown,
        source_refs=source_refs,
        reason=str(payload.get("reason") or ""),
        confidence=max(0.0, min(1.0, float(payload.get("confidence") or 0.0))),
        similar_scenes=[name for name, _score in similar],
    )


def apply_scene_patch(data_root: Path, agent_id: uuid.UUID, candidate: ScenePatchCandidate) -> dict:
    """Governed write: land an accepted scene patch under memory/scenes/.

    Refuses held candidates and privacy-gate violations; both leave audit
    records. Raw agent file writes under memory/ are already refused at the
    workspace tool layer — this is the only scene write path.
    """
    if candidate.status != "proposed" or candidate.action not in {"create", "update"}:
        write_distillation_audit(
            data_root,
            agent_id,
            stage="scene_apply",
            outcome="refused",
            reason=f"candidate not applicable (status={candidate.status}, action={candidate.action})",
        )
        return {"applied": False, "reason": "candidate not applicable"}

    from app.services.privacy_layer import PrivacyLayer

    privacy = PrivacyLayer().classify_and_mask(candidate.patch_markdown)
    if privacy.rejected:
        write_distillation_audit(
            data_root,
            agent_id,
            stage="scene_apply",
            outcome="rejected",
            reason=f"privacy gate: {privacy.reason}",
            detail={"scene_path": candidate.scene_path},
        )
        return {"applied": False, "reason": f"privacy gate: {privacy.reason}"}

    relative = candidate.scene_path.removeprefix("memory/scenes/")
    target = scenes_dir(data_root, agent_id) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(privacy.sanitized_text.rstrip() + "\n", encoding="utf-8")
    write_distillation_audit(
        data_root,
        agent_id,
        stage="scene_apply",
        outcome="applied",
        reason=candidate.reason,
        detail={"scene_path": candidate.scene_path, "action": candidate.action},
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
