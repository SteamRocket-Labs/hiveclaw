"""Scene / Wiki curator MVP tests (docs/agent-memory-md-first-spec.md §12 P5).

Acceptance:
- Scene curator can update or hold a scene patch using existing T3 entries
  and source refs.
- Wiki curator can produce a Markdown concept page patch with claim, scope,
  evidence, contradictions, changes, and retrieval tags.
- Both curators emit candidates first; governed write APIs apply accepted
  patches.
- Ambiguous scene merge or wiki claim conflict becomes a held candidate with
  audit reason.
- No graph database, KG, or PPR is required for this phase.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.memory.scene_curator import (
    ScenePatchCandidate,
    apply_scene_patch,
    curate_scene,
)
from app.memory.wiki_curator import (
    WikiPatchCandidate,
    apply_wiki_patch,
    curate_wiki_page,
)

AGENT = uuid.uuid4()


def _llm_returning(payload: dict):
    async def llm(system: str, user: str) -> str:
        return json.dumps(payload)

    return llm


# ── Scene curator ──


@pytest.mark.asyncio
async def test_scene_curator_creates_scene_candidate(tmp_path: Path) -> None:
    candidate = await curate_scene(
        tmp_path,
        AGENT,
        atoms=[
            {"content": "Deployed hive backend to Railway with cached layers", "source_ref": "t2:insights#1"},
            {"content": "Railway deploy failed on set -e in entrypoint", "source_ref": "t2:errors#2"},
        ],
        llm=_llm_returning(
            {
                "action": "create",
                "scene_slug": "railway-deployments",
                "title": "Railway Deployments",
                "patch_markdown": (
                    "---\ntitle: Railway Deployments\ntype: scene\ntags: [deploy, railway]\nstatus: active\n---\n\n"
                    "## Narrative\n\nDeploys hit cached layers and set -e traps.\n\n"
                    "## Evidence\n\n- [fact] cached layers bite #deploy\n\n## Changes\n\n- [2026-06-04] created\n"
                ),
                "reason": "two related deploy episodes, no existing scene",
                "confidence": 0.85,
            }
        ),
    )

    assert isinstance(candidate, ScenePatchCandidate)
    assert candidate.status == "proposed"
    assert candidate.action == "create"
    assert candidate.scene_path.endswith("railway-deployments.md")
    assert candidate.source_refs == ["t2:insights#1", "t2:errors#2"]
    assert candidate.reason


@pytest.mark.asyncio
async def test_scene_curator_updates_existing_scene(tmp_path: Path) -> None:
    scenes_dir = tmp_path / str(AGENT) / "memory" / "scenes"
    scenes_dir.mkdir(parents=True)
    (scenes_dir / "railway-deployments.md").write_text(
        "---\ntitle: Railway Deployments\ntype: scene\nstatus: active\n---\n\n## Narrative\n\nold\n",
        encoding="utf-8",
    )

    candidate = await curate_scene(
        tmp_path,
        AGENT,
        atoms=[{"content": "New deploy gotcha: log viewing requires service id", "source_ref": "t2:insights#9"}],
        llm=_llm_returning(
            {
                "action": "update",
                "scene_slug": "railway-deployments",
                "title": "Railway Deployments",
                "patch_markdown": (
                    "---\ntitle: Railway Deployments\ntype: scene\ntags: [deploy]\nstatus: active\n---\n\n"
                    "## Narrative\n\nold + log viewing gotcha.\n\n## Changes\n\n- [2026-06-04] merged log gotcha\n"
                ),
                "reason": "existing scene covers the same subject — update, not create",
                "confidence": 0.8,
            }
        ),
    )

    assert candidate.status == "proposed"
    assert candidate.action == "update"


@pytest.mark.asyncio
async def test_scene_curator_holds_without_llm(tmp_path: Path) -> None:
    candidate = await curate_scene(
        tmp_path,
        AGENT,
        atoms=[{"content": "ambiguous episode", "source_ref": "t2:x"}],
        llm=None,
    )
    assert candidate.status == "held"
    assert "llm" in candidate.reason.lower() or "adjudicat" in candidate.reason.lower()

    # Held candidates leave an audit record.
    audit = (tmp_path / str(AGENT) / "memory" / "distillation_audit.jsonl").read_text(encoding="utf-8")
    assert "held" in audit
    assert "scene" in audit


@pytest.mark.asyncio
async def test_scene_curator_holds_on_invalid_llm_output(tmp_path: Path) -> None:
    async def broken_llm(system: str, user: str) -> str:
        return "not json at all"

    candidate = await curate_scene(
        tmp_path,
        AGENT,
        atoms=[{"content": "episode", "source_ref": "t2:x"}],
        llm=broken_llm,
    )
    assert candidate.status == "held"


@pytest.mark.asyncio
async def test_scene_curator_respects_llm_hold_decision(tmp_path: Path) -> None:
    candidate = await curate_scene(
        tmp_path,
        AGENT,
        atoms=[{"content": "could be dup or contradiction", "source_ref": "t2:x"}],
        llm=_llm_returning(
            {
                "action": "hold",
                "scene_slug": "",
                "title": "",
                "patch_markdown": "",
                "reason": "duplicate vs contradiction is ambiguous",
                "confidence": 0.3,
            }
        ),
    )
    assert candidate.status == "held"
    assert "ambiguous" in candidate.reason


def test_apply_scene_patch_writes_governed_file(tmp_path: Path) -> None:
    candidate = ScenePatchCandidate(
        status="proposed",
        action="create",
        scene_path="memory/scenes/railway-deployments.md",
        patch_markdown="---\ntitle: Railway Deployments\ntype: scene\nstatus: active\n---\n\n## Narrative\n\nx\n",
        source_refs=["t2:insights#1"],
        reason="ok",
        confidence=0.85,
    )
    result = apply_scene_patch(tmp_path, AGENT, candidate)
    assert result["applied"] is True
    body = (tmp_path / str(AGENT) / "memory" / "scenes" / "railway-deployments.md").read_text(encoding="utf-8")
    assert "Railway Deployments" in body


def test_apply_scene_patch_rejects_path_traversal(tmp_path: Path) -> None:
    candidate = ScenePatchCandidate(
        status="proposed",
        action="create",
        scene_path="memory/scenes/../../soul.md",
        patch_markdown="---\ntitle: Escape\ntype: scene\nstatus: active\n---\n\n## Narrative\n\nsafe\n",
        source_refs=["t2:insights#1"],
        reason="bad path",
        confidence=0.85,
    )
    result = apply_scene_patch(tmp_path, AGENT, candidate)
    assert result["applied"] is False
    assert not (tmp_path / str(AGENT) / "soul.md").exists()


def test_apply_scene_patch_rejects_credentials(tmp_path: Path) -> None:
    candidate = ScenePatchCandidate(
        status="proposed",
        action="create",
        scene_path="memory/scenes/leaky.md",
        patch_markdown="---\ntitle: Leaky\ntype: scene\nstatus: active\n---\n\napi_key=sk-live-abcdef1234567890abcd\n",
        source_refs=["t2:x"],
        reason="ok",
        confidence=0.9,
    )
    result = apply_scene_patch(tmp_path, AGENT, candidate)
    assert result["applied"] is False
    assert not (tmp_path / str(AGENT) / "memory" / "scenes" / "leaky.md").exists()


def test_apply_scene_patch_refuses_held_candidate(tmp_path: Path) -> None:
    candidate = ScenePatchCandidate(
        status="held",
        action="hold",
        scene_path="memory/scenes/nope.md",
        patch_markdown="x",
        source_refs=[],
        reason="held",
        confidence=0.0,
    )
    result = apply_scene_patch(tmp_path, AGENT, candidate)
    assert result["applied"] is False


# ── Wiki curator ──

_WIKI_PAGE = (
    "---\ntitle: Memory Control Plane\ntype: concept\ntags: [memory, governance]\nstatus: active\n---\n\n"
    "## Current Claim\n\nDistillers produce candidates; the control plane decides writes.\n\n"
    "## Scope\n\nHive memory engine.\n\n"
    "## Evidence\n\n- [decision] write gate governs T3 #governance\n\n"
    "## Contradictions\n\n(none)\n\n"
    "## Changes\n\n- [2026-06-04] page created\n\n"
    "## Retrieval Tags\n\nmemory, governance, write-gate\n"
)


@pytest.mark.asyncio
async def test_wiki_curator_produces_full_page_patch(tmp_path: Path) -> None:
    candidate = await curate_wiki_page(
        tmp_path,
        AGENT,
        concept="Memory Control Plane",
        evidence_entries=[
            {"content": "write gate governs all durable writes", "source_ref": "t3:feedback#1"},
        ],
        llm=_llm_returning(
            {
                "action": "upsert",
                "concept_slug": "memory-control-plane",
                "page_markdown": _WIKI_PAGE,
                "reason": "evidence supports the claim",
                "confidence": 0.9,
            }
        ),
    )

    assert isinstance(candidate, WikiPatchCandidate)
    assert candidate.status == "proposed"
    for section in (
        "## Current Claim",
        "## Scope",
        "## Evidence",
        "## Contradictions",
        "## Changes",
        "## Retrieval Tags",
    ):
        assert section in candidate.page_markdown


@pytest.mark.asyncio
async def test_wiki_curator_holds_on_missing_sections(tmp_path: Path) -> None:
    candidate = await curate_wiki_page(
        tmp_path,
        AGENT,
        concept="Memory Control Plane",
        evidence_entries=[{"content": "x", "source_ref": "t3:x"}],
        llm=_llm_returning(
            {
                "action": "upsert",
                "concept_slug": "memory-control-plane",
                "page_markdown": "## Current Claim\n\nonly a claim, no other sections\n",
                "reason": "thin",
                "confidence": 0.9,
            }
        ),
    )
    assert candidate.status == "held"
    assert "section" in candidate.reason.lower()


@pytest.mark.asyncio
async def test_wiki_curator_holds_low_confidence_claim_change(tmp_path: Path) -> None:
    candidate = await curate_wiki_page(
        tmp_path,
        AGENT,
        concept="Memory Control Plane",
        evidence_entries=[{"content": "weak counter-evidence", "source_ref": "t3:y"}],
        llm=_llm_returning(
            {
                "action": "upsert",
                "concept_slug": "memory-control-plane",
                "page_markdown": _WIKI_PAGE,
                "reason": "uncertain",
                "confidence": 0.3,
            }
        ),
    )
    assert candidate.status == "held"
    assert "confidence" in candidate.reason.lower()


def test_apply_wiki_patch_writes_page(tmp_path: Path) -> None:
    candidate = WikiPatchCandidate(
        status="proposed",
        action="upsert",
        page_path="memory/wiki/memory-control-plane.md",
        page_markdown=_WIKI_PAGE,
        source_refs=["t3:feedback#1"],
        reason="ok",
        confidence=0.9,
        concept="Memory Control Plane",
    )
    result = apply_wiki_patch(tmp_path, AGENT, candidate)
    assert result["applied"] is True
    body = (tmp_path / str(AGENT) / "memory" / "wiki" / "memory-control-plane.md").read_text(encoding="utf-8")
    assert "## Current Claim" in body


def test_apply_wiki_patch_rejects_path_traversal(tmp_path: Path) -> None:
    candidate = WikiPatchCandidate(
        status="proposed",
        action="upsert",
        page_path="memory/wiki/../../soul.md",
        page_markdown=_WIKI_PAGE,
        source_refs=["t3:feedback#1"],
        reason="bad path",
        confidence=0.9,
        concept="Memory Control Plane",
    )
    result = apply_wiki_patch(tmp_path, AGENT, candidate)
    assert result["applied"] is False
    assert not (tmp_path / str(AGENT) / "soul.md").exists()


def test_apply_wiki_patch_refuses_held(tmp_path: Path) -> None:
    candidate = WikiPatchCandidate(
        status="held",
        action="hold",
        page_path="memory/wiki/x.md",
        page_markdown="x",
        source_refs=[],
        reason="held",
        confidence=0.0,
        concept="X",
    )
    result = apply_wiki_patch(tmp_path, AGENT, candidate)
    assert result["applied"] is False


# ── Runtime wiring: heartbeat curation tick ──


def _seed_t3(tmp_path: Path, agent_id: uuid.UUID, count: int) -> None:
    mem_dir = tmp_path / str(agent_id) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(
        f"- [2026-06-0{(i % 4) + 1}][entry_id=mem_seed{i}][concept=strategy] curate scene tick fact {i} about lane {i}"
        for i in range(count)
    )
    (mem_dir / "strategies.md").write_text("# Strategies\n\n" + lines + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_curation_tick_skips_below_threshold(tmp_path: Path) -> None:
    from app.services.memory_curation import run_scene_wiki_curation_tick

    agent_id = uuid.uuid4()
    _seed_t3(tmp_path, agent_id, 2)

    summary = await run_scene_wiki_curation_tick(agent_id, None, data_root=tmp_path)
    assert summary["status"] == "skipped"
    assert summary["new_entries"] == 2


@pytest.mark.asyncio
async def test_curation_tick_does_not_advance_cursor_on_infrastructure_hold(tmp_path: Path) -> None:
    from app.services.memory_curation import run_scene_wiki_curation_tick

    agent_id = uuid.uuid4()
    _seed_t3(tmp_path, agent_id, 4)

    # tenant_id=None → no LLM caller → infrastructure hold. This must remain
    # retryable; only semantic/applied decisions are terminal cursor progress.
    summary = await run_scene_wiki_curation_tick(agent_id, None, data_root=tmp_path)
    assert summary["status"] == "ran"
    assert summary["scene"]["status"] == "held"

    audit = (tmp_path / str(agent_id) / "memory" / "distillation_audit.jsonl").read_text(encoding="utf-8")
    assert "scene_curation" in audit

    # Second tick sees the same entries again because the missing LLM is a
    # transient infrastructure failure, not a terminal semantic decision.
    again = await run_scene_wiki_curation_tick(agent_id, None, data_root=tmp_path)
    assert again["status"] == "ran"
    assert again["batch"] == 4


@pytest.mark.asyncio
async def test_curation_tick_never_raises(tmp_path: Path, monkeypatch) -> None:
    from app.services import memory_curation

    def boom(*args, **kwargs):
        raise RuntimeError("manifest exploded")

    monkeypatch.setattr("app.memory.md_store.build_t3_entry_manifest", boom)
    agent_id = uuid.uuid4()
    _seed_t3(tmp_path, agent_id, 4)

    summary = await memory_curation.run_scene_wiki_curation_tick(agent_id, None, data_root=tmp_path)
    assert summary["status"] == "error"
    assert "RuntimeError" in summary["error"]
