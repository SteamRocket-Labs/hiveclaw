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

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _write_self(root: Path, agent_id: uuid.UUID, body: str) -> Path:
    mem_dir = root / str(agent_id) / "memory"
    (mem_dir / "self").mkdir(parents=True, exist_ok=True)
    path = mem_dir / "self" / "self.md"
    path.write_text(body, encoding="utf-8")
    return path


SELF_WITH_CANDIDATES = """## 方法

### 研究三段法 — 熟练
<!-- id: mem_aaa -->
Research → design → verify workflow proven across 3 PRs
- skill候选: 跨 3 个 PR 验证有效
- 证据: t2-a1b2

### 普通方法 — 一般
<!-- id: mem_plain -->
plain strategy without candidate marker

### 夜间摘要管线 — 一般
<!-- id: mem_bbb -->
Nightly digest pipeline needs durable state
- workflow候选: 需要持久状态

### 已固化方法 — 熟练
<!-- id: mem_ccc -->
already promoted method
- skill候选: 曾经的候选
- 已固化 → [[skill-market-research]]
"""


# ── Heartbeat lane: candidate signals only, no direct skill writes ──


def test_heartbeat_has_no_skill_write_executor() -> None:
    source = (PROJECT_ROOT / "backend" / "app" / "services" / "heartbeat.py").read_text(encoding="utf-8")

    assert "_build_heartbeat_tool_executor" not in source
    assert "execute_tool" not in source
    assert "save_skill" not in source


def test_heartbeat_template_routes_skill_evidence_to_candidate_lane() -> None:
    template = (PROJECT_ROOT / "backend" / "app" / "templates" / "HEARTBEAT.md").read_text(encoding="utf-8")
    # The curator records candidate signals; it never calls save_skill itself.
    assert "save_skill" not in template
    assert "skill_candidate" in template


def test_heartbeat_does_not_mechanically_author_skill_candidate_signal() -> None:
    import inspect

    from app.services import heartbeat

    source = inspect.getsource(heartbeat._build_evolution_context)
    assert "call `save_skill`" not in source
    assert "Skill Candidate Opportunity" not in source
    assert "_SKILL_THRESHOLD" not in source
    assert "_skill_already_covers_tools" not in source
    assert "skill_opportunity_cooldown" not in source
    assert "save_memory" not in source


# ── Memory candidate readers ──


def test_load_memory_skill_candidates_reads_unpromoted_markers(tmp_path) -> None:
    from app.services.skill_distiller import load_memory_skill_candidates

    agent_id = uuid.uuid4()
    _write_self(tmp_path, agent_id, SELF_WITH_CANDIDATES)

    candidates = load_memory_skill_candidates(tmp_path, agent_id)
    contents = [c["content"] for c in candidates]
    assert any("Research → design → verify" in c for c in contents)
    assert all("already promoted" not in c for c in contents)
    assert all("Nightly digest" not in c for c in contents)  # workflow lane, not skill lane
    assert candidates[0]["entry_id"] == "mem_aaa"


def test_load_memory_workflow_candidates_reads_workflow_lane(tmp_path) -> None:
    from app.services.skill_distiller import load_memory_workflow_candidates

    agent_id = uuid.uuid4()
    _write_self(tmp_path, agent_id, SELF_WITH_CANDIDATES)

    candidates = load_memory_workflow_candidates(tmp_path, agent_id)
    assert len(candidates) == 1
    assert "Nightly digest" in candidates[0]["content"]


def test_mark_profile_entry_promoted_stamps_two_way_link(tmp_path) -> None:
    from app.memory.plane_read import mark_profile_entry_promoted
    from app.services.skill_distiller import load_memory_skill_candidates

    agent_id = uuid.uuid4()
    path = _write_self(tmp_path, agent_id, SELF_WITH_CANDIDATES)

    ok = mark_profile_entry_promoted(
        tmp_path, agent_id, entry_id="mem_aaa", promoted_to="skill", target="market-research"
    )
    assert ok
    content = path.read_text(encoding="utf-8")
    assert "已固化 → [[skill-market-research]]" in content
    assert "Research → design → verify" in content  # narrative stays (two-way link)
    # candidate no longer qualifies
    candidates = load_memory_skill_candidates(tmp_path, agent_id)
    assert all(c["entry_id"] != "mem_aaa" for c in candidates)


def test_mark_profile_entry_promoted_is_idempotent(tmp_path) -> None:
    from app.memory.plane_read import mark_profile_entry_promoted

    agent_id = uuid.uuid4()
    path = _write_self(tmp_path, agent_id, SELF_WITH_CANDIDATES)
    assert mark_profile_entry_promoted(tmp_path, agent_id, entry_id="mem_aaa", promoted_to="skill", target="x")
    assert mark_profile_entry_promoted(tmp_path, agent_id, entry_id="mem_aaa", promoted_to="skill", target="x")

    assert path.read_text(encoding="utf-8").count("已固化 → [[skill-x]]") == 1


def test_record_workflow_candidates_writes_evolution_ledger(tmp_path) -> None:
    from app.services.skill_distiller import record_workflow_candidates_from_memory

    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    _write_self(tmp_path, agent_id, SELF_WITH_CANDIDATES)

    recorded = record_workflow_candidates_from_memory(tmp_path, agent_id, workspace=workspace)
    assert recorded == 1

    ledger = (workspace / "evolution" / "evolution_ledger.jsonl").read_text(encoding="utf-8")
    assert "workflow" in ledger
    assert "mem_bbb" in ledger

    # Idempotent: re-recording the same candidate does not duplicate it.
    again = record_workflow_candidates_from_memory(tmp_path, agent_id, workspace=workspace)
    assert again == 0
