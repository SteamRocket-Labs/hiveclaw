"""P0 term-freeze contracts (docs/agent-memory-md-first-spec.md §12 P0).

Distillers produce candidates; the Memory Control Plane decides writes,
activation, promotion, retirement, and audit. These tests freeze the four
distiller identities and the `container_candidate` prompt contract:

- Extractor   = atom extraction, not promotion
- Heartbeat   = Memory Curator, not final skill/workflow writer
- Dream       = Reconsolidator + IdentityPromoter, not free identity editor
- SkillDistiller = consumes evidence-backed candidates, not raw patterns
"""

from __future__ import annotations

import inspect
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


# ── Heartbeat: T3 Consolidator, not final skill/workflow writer ──


def test_heartbeat_template_is_memory_curator_contract() -> None:
    template = (PROJECT_ROOT / "backend" / "app" / "templates" / "HEARTBEAT.md").read_text(encoding="utf-8")
    prompt = _normalized(template)

    assert "t3 consolidator" in prompt
    # Heartbeat authors semantic T3 job artifacts; it is not the final writer.
    assert "not the physical committer" in prompt
    assert "consolidation_pitch.md" in prompt
    assert "revised_patch.md" in prompt
    assert "memory gate" in prompt
    assert "platform gate" in prompt
    assert "do not create skill files or workflow json" in prompt


# ── Dream: Reconsolidator + IdentityPromoter, not free identity editor ──


def test_dream_prompt_is_reconsolidator_and_identity_promoter() -> None:
    from app.services.auto_dream import _AUTO_DREAM_SYSTEM_PROMPT

    prompt = _normalized(_AUTO_DREAM_SYSTEM_PROMPT)

    assert "reconsolidator" in prompt
    assert "identitypromoter" in prompt or "identity promoter" in prompt
    assert "not a free identity editor" in prompt
    # Dream proposes candidates / lifecycle patches; it does not own final writes.
    assert "candidate" in prompt
    assert "lifecycle" in prompt


# ── SkillDistiller: consumes evidence-backed candidates ──


def test_skill_distiller_prompt_consumes_candidate_evidence() -> None:
    from app.services import skill_distiller

    source = _normalized(inspect.getsource(skill_distiller._draft_skill_with_llm))

    assert "evidence-backed" in source
    assert "skill_candidate" in source or "skill candidate" in source
    # The distiller adjudicates candidates; it does not invent raw ungoverned patterns.
    assert "ungoverned" in source or "do not invent" in source
