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
    candidate_dir = workspace / "evolution" / "skill_candidates" / result["candidate_id"]
    assert (candidate_dir / "candidate_signal.md").exists()
    assert not (candidate_dir / "SKILL.md.draft").exists()
    assert not (candidate_dir / "SKILL.md").exists()
    assert (candidate_dir / "skill_pitch.md").exists()
    assert (candidate_dir / "eval_plan.md").exists()
    assert (candidate_dir / "failure_cases.md").exists()
    assert (candidate_dir / "manifest.json").exists()
    assert not (workspace / "skills").exists()

    entries = load_evolution_ledger(workspace)
    skill_candidate = [entry for entry in entries if entry.get("target_type") == "skill_candidate"][-1]
    eval_run = [entry for entry in entries if entry.get("event") == "eval_run" and entry.get("candidate_id") == result["candidate_id"]][-1]
    assert skill_candidate["metadata"]["schema"] == "skill_candidate_manifest.v1"
    assert skill_candidate["metadata"]["guard"]["allowed"] is True
    assert skill_candidate["metadata"]["progressive_disclosure"]["kind"] == "candidate_summary"
    report = eval_run["metadata"]["verification_report"]
    assert [check["type"] for check in report["checks"]] == ["skill_guard"]
    assert report["checks"][0]["evidence"]["guard"]["allowed"] is True


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
        metadata={
            "loaded_skill_name": "incident-response",
            "fast_reflection_signal": "下次这个流程先补 rollback 检查。",
        },
    )
    workspace = tmp_path / str(agent_id)
    fast_candidate = load_evolution_ledger(workspace)[0]

    result = propose_skill_candidate_from_fast_reflection(workspace=workspace, fast_candidate=fast_candidate)

    assert result["status"] == "skill_candidate_created"
    assert result["route"] == "patch_existing_skill"
    assert result["skill_name"] == "incident-response"
