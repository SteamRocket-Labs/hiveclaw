from __future__ import annotations

import uuid


def test_skill_flywheel_creates_candidate_draft_from_repeated_fast_reflection(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate
    from app.services.skill_flywheel import propose_skill_candidate_from_fast_reflection

    agent_id = uuid.uuid4()
    reflection = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        messages=[{"role": "user", "content": "This deploy checklist workflow keeps repeating."}],
        metadata={"repeated_workflow_signature": "read_file -> run_tests -> update_status"},
    )
    workspace = tmp_path / str(agent_id)
    fast_candidate = load_evolution_ledger(workspace)[0]

    result = propose_skill_candidate_from_fast_reflection(workspace=workspace, fast_candidate=fast_candidate)

    assert reflection["status"] == "candidate_created"
    assert result["status"] == "skill_candidate_created"
    assert result["route"] == "new_class_skill"
    assert (workspace / "evolution" / "skill_candidates" / result["candidate_id"] / "SKILL.md").exists()
    assert not (workspace / "skills").exists()

    entries = load_evolution_ledger(workspace)
    skill_candidate = [entry for entry in entries if entry.get("target_type") == "skill_candidate"][-1]
    assert skill_candidate["metadata"]["schema"] == "skill_candidate_manifest.v1"
    assert skill_candidate["metadata"]["guard"]["allowed"] is True
    assert skill_candidate["metadata"]["progressive_disclosure"]["kind"] == "candidate_summary"
    assert any(entry.get("event") == "eval_run" and entry.get("candidate_id") == result["candidate_id"] for entry in entries)


def test_skill_flywheel_prefers_loaded_skill_patch_route(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate
    from app.services.skill_flywheel import propose_skill_candidate_from_fast_reflection

    agent_id = uuid.uuid4()
    create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-2",
        messages=[{"role": "user", "content": "下次这个流程先补 rollback 检查。"}],
        metadata={"loaded_skill_name": "incident-response"},
    )
    workspace = tmp_path / str(agent_id)
    fast_candidate = load_evolution_ledger(workspace)[0]

    result = propose_skill_candidate_from_fast_reflection(workspace=workspace, fast_candidate=fast_candidate)

    assert result["status"] == "skill_candidate_created"
    assert result["route"] == "patch_existing_skill"
    assert result["skill_name"] == "incident-response"
