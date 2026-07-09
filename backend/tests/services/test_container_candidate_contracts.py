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


# ── Extractor: atom extraction, not promotion ──


def test_extractor_prompt_is_atom_extraction_not_promotion() -> None:
    from app.services.extract_agent import EXTRACT_PROMPT

    prompt = _normalized(EXTRACT_PROMPT)

    # Identity: atom extraction producing candidates.
    assert "atom" in prompt
    assert "candidate" in prompt
    # Authority boundary: extraction never promotes to soul/skill/workflow.
    assert "do not promote" in prompt
    # Container vocabulary is shared by live memory governance gates (spec §6).
    assert "container" in prompt
    assert "memory_append" in prompt
    assert "soul_candidate" in prompt
    assert "skill_candidate" in prompt
    assert "workflow_candidate" in prompt
    assert "artifact_only" in prompt


def test_extractor_parses_container_candidate_metadata() -> None:
    from app.services.extract_agent import _parse_extractions

    parsed = _parse_extractions(
        "\n".join(
            [
                "[strategy][ev=user_stated][conf=0.90][container=skill_candidate] "
                "Three-phase research workflow proven across 3 tasks",
                "[feedback][container=soul_candidate] User requires plain text only — confirmed 3 times",
                "[reference][container=memory_append] Hive backend runs on port 8008",
                "[error][container=not_a_container] web_search timeout on CJK queries",
            ]
        )
    )

    assert len(parsed) == 4
    assert parsed[0]["container_candidate"] == "skill_candidate"
    assert parsed[1]["container_candidate"] == "soul_candidate"
    assert parsed[2]["container_candidate"] == "memory_append"
    # Invalid vocabulary is dropped, not silently stored.
    assert "container_candidate" not in parsed[3]


def test_t2_entry_round_trips_container_candidate() -> None:
    from app.memory.t2_store import format_t2_entry, parse_t2_entry_line

    line = format_t2_entry(
        category="strategy",
        content="Research → design → verify workflow reduces review iterations",
        source="web",
        timestamp="2026-06-04",
        container_candidate="skill_candidate",
    )
    assert "[container=skill_candidate]" in line

    parsed = parse_t2_entry_line(line)
    assert parsed is not None
    assert parsed["container_candidate"] == "skill_candidate"


def test_t2_append_persists_container_candidate(tmp_path) -> None:
    import uuid

    from app.memory.t2_store import append_t2_entries, load_t2_entries

    agent_id = uuid.uuid4()
    written = append_t2_entries(
        tmp_path,
        agent_id,
        extractions=[
            {
                "category": "strategy",
                "content": "Workflow candidate captures repeatable deployment validation steps.",
                "container_candidate": "workflow_candidate",
            }
        ],
        source="web",
    )
    assert written == 1

    entries, _ = load_t2_entries(tmp_path, agent_id)
    assert len(entries) == 1
    assert entries[0]["container_candidate"] == "workflow_candidate"


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
