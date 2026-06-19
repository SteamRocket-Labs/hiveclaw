"""Skill/Workflow candidate lane tests (docs/agent-memory-md-first-spec.md §12 P4).

Acceptance:
- Heartbeat only records skill/workflow candidate signals.
- SkillDistiller consumes `skill_candidate`.
- Workflow promotion consumes `workflow_candidate`.
- Promoted T3 strategy entries mark `promoted_to_skill` or `promoted_to_workflow`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _write_strategies(root: Path, agent_id: uuid.UUID, body: str) -> Path:
    mem_dir = root / str(agent_id) / "memory"
    (mem_dir / "t3").mkdir(parents=True, exist_ok=True)
    path = mem_dir / "t3" / "capabilities.md"
    path.write_text(body, encoding="utf-8")
    return path


# ── Heartbeat lane: candidate signals only, no direct skill writes ──


@pytest.mark.asyncio
async def test_heartbeat_executor_blocks_save_skill(monkeypatch) -> None:
    from app.services.heartbeat import _build_heartbeat_tool_executor

    calls: list[str] = []

    async def fake_execute_tool(tool_name, args, agent_id, creator_id):
        calls.append(tool_name)
        return "executed"

    monkeypatch.setattr("app.services.heartbeat.execute_tool", fake_execute_tool)

    executor = _build_heartbeat_tool_executor(uuid.uuid4(), uuid.uuid4())
    out = await executor("save_skill", {"name": "smuggled"})

    assert "save_skill" not in calls  # never reached real execution
    assert "BLOCKED" in out or "candidate" in out.lower()
    assert "T3 consolidation pitch" in out

    # Other tools still pass through.
    passthrough = await executor("read_file", {"path": "soul.md"})
    assert passthrough == "executed"
    assert calls == ["read_file"]


def test_heartbeat_template_routes_skill_evidence_to_candidate_lane() -> None:
    template = (PROJECT_ROOT / "backend" / "app" / "templates" / "HEARTBEAT.md").read_text(encoding="utf-8")
    # The curator records candidate signals; it never calls save_skill itself.
    assert "save_skill" not in template
    assert "skill_candidate" in template


def test_heartbeat_skill_opportunity_hint_records_candidate_signal() -> None:
    import inspect

    from app.services import heartbeat

    source = inspect.getsource(heartbeat._build_evolution_context)
    # The opportunity nudge must route through T3 job artifact evidence,
    # not instruct the LLM to call the save_skill tool.
    assert "call `save_skill`" not in source
    assert "Skill Candidate Opportunity" in source
    assert "skill_candidate" in source
    assert "consolidation_pitch.md" in source
    assert "save_memory" not in source


# ── Memory candidate readers ──


def test_load_memory_skill_candidates_reads_unpromoted_markers(tmp_path) -> None:
    from app.services.skill_distiller import load_memory_skill_candidates

    agent_id = uuid.uuid4()
    _write_strategies(
        tmp_path,
        agent_id,
        "# Strategies\n\n"
        "- [2026-06-01][container=skill_candidate][entry_id=mem_aaa] Research → design → verify workflow proven across 3 PRs\n"
        "- [2026-06-02] plain strategy without candidate marker\n"
        "- [2026-06-03][container=workflow_candidate][entry_id=mem_bbb] Nightly digest pipeline needs durable state\n"
        "- [2026-06-04][container=skill_candidate][promoted_to=skill][entry_id=mem_ccc] already promoted method\n",
    )

    candidates = load_memory_skill_candidates(tmp_path, agent_id)
    contents = [c["content"] for c in candidates]
    assert any("Research → design → verify" in c for c in contents)
    assert all("already promoted" not in c for c in contents)
    assert all("Nightly digest" not in c for c in contents)  # workflow lane, not skill lane
    assert candidates[0]["entry_id"] == "mem_aaa"


def test_load_memory_workflow_candidates_reads_workflow_lane(tmp_path) -> None:
    from app.services.skill_distiller import load_memory_workflow_candidates

    agent_id = uuid.uuid4()
    _write_strategies(
        tmp_path,
        agent_id,
        "# Strategies\n\n"
        "- [2026-06-03][container=workflow_candidate][entry_id=mem_bbb] Nightly digest pipeline needs durable state\n"
        "- [2026-06-04][container=workflow_candidate][promoted_to=workflow][entry_id=mem_ddd] promoted pipeline\n",
    )

    candidates = load_memory_workflow_candidates(tmp_path, agent_id)
    assert len(candidates) == 1
    assert "Nightly digest" in candidates[0]["content"]


def test_mark_t3_entry_promoted_stamps_marker(tmp_path) -> None:
    from app.memory.md_store import mark_t3_entry_promoted
    from app.services.skill_distiller import load_memory_skill_candidates

    agent_id = uuid.uuid4()
    path = _write_strategies(
        tmp_path,
        agent_id,
        "# Strategies\n\n"
        "- [2026-06-01][container=skill_candidate][entry_id=mem_aaa] Research → design → verify workflow proven across 3 PRs\n",
    )

    ok = mark_t3_entry_promoted(
        tmp_path,
        agent_id,
        entry_id="mem_aaa",
        promoted_to="skill",
        target="market-research",
    )
    assert ok

    body = path.read_text(encoding="utf-8")
    assert "[promoted_to=skill]" in body
    assert "[promoted_target=market-research]" in body
    # Promoted entries leave the candidate pool.
    assert load_memory_skill_candidates(tmp_path, agent_id) == []


# ── Workflow candidate lane: recorded into the evolution ledger ──


def test_record_workflow_candidates_writes_evolution_ledger(tmp_path) -> None:
    from app.services.skill_distiller import record_workflow_candidates_from_memory

    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    _write_strategies(
        tmp_path,
        agent_id,
        "# Strategies\n\n"
        "- [2026-06-03][container=workflow_candidate][entry_id=mem_bbb] Nightly digest pipeline needs durable state\n",
    )

    recorded = record_workflow_candidates_from_memory(tmp_path, agent_id, workspace=workspace)
    assert recorded == 1

    ledger = (workspace / "evolution" / "evolution_ledger.jsonl").read_text(encoding="utf-8")
    assert "workflow" in ledger
    assert "mem_bbb" in ledger

    # Idempotent: re-recording the same candidate does not duplicate it.
    again = record_workflow_candidates_from_memory(tmp_path, agent_id, workspace=workspace)
    assert again == 0
