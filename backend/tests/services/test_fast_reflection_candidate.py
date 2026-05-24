from __future__ import annotations

import uuid


def test_fast_reflection_candidate_records_user_correction_without_durable_memory_write(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-1",
        messages=[
            {"role": "assistant", "content": "I will always use yarn for this repo."},
            {"role": "user", "content": "不是，用这个项目时以后都用 npm，不要用 yarn。"},
        ],
        metadata={"tenant_id": str(uuid.uuid4()), "final_response": "收到。"},
    )

    assert result["status"] == "candidate_created"
    assert result["signal_type"] == "user_preference_correction"

    workspace = tmp_path / str(agent_id)
    entries = load_evolution_ledger(workspace)
    assert len(entries) == 1
    candidate = entries[0]
    assert candidate["event"] == "candidate"
    assert candidate["target_type"] == "fast_reflection"
    assert candidate["manifest"]["schema"] == "hive_evolution_manifest.v1"
    assert candidate["metadata"]["schema"] == "fast_reflection_candidate.v1"
    assert candidate["metadata"]["signal_type"] == "user_preference_correction"
    assert not (workspace / "memory" / "t2").exists()
    assert not (workspace / "skills").exists()


def test_fast_reflection_skips_low_signal_chatter(tmp_path) -> None:
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-2",
        messages=[
            {"role": "user", "content": "谢谢"},
            {"role": "assistant", "content": "不客气。"},
        ],
        metadata={},
    )

    assert result == {"status": "skipped", "reason": "low_signal"}
    assert not (tmp_path / str(agent_id) / "evolution" / "evolution_ledger.jsonl").exists()


def test_repeated_workflow_signal_bridges_to_skill_candidate(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-wf",
        messages=[{"role": "user", "content": "deploy steps: build then migrate then restart, same as last time"}],
        metadata={"repeated_workflow_signature": "deploy -> build -> migrate -> restart"},
    )

    assert result["status"] == "candidate_created"
    assert result["signal_type"] == "repeated_task_pattern"
    skill = result["skill_candidate"]
    assert skill["status"] == "skill_candidate_created"
    assert skill["verification_passed"] is True

    workspace = tmp_path / str(agent_id)
    candidate_targets = {
        entry.get("target_type")
        for entry in load_evolution_ledger(workspace)
        if entry.get("event") == "candidate"
    }
    assert {"fast_reflection", "skill_candidate"} <= candidate_targets
    assert (workspace / "evolution" / "skill_candidates" / skill["candidate_id"] / "SKILL.md").exists()


def test_user_preference_correction_does_not_bridge_to_skill(tmp_path) -> None:
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-pref",
        messages=[{"role": "user", "content": "以后不要用 emoji"}],
        metadata={},
    )

    assert result["status"] == "candidate_created"
    assert result["signal_type"] == "user_preference_correction"
    assert result["skill_candidate"]["status"] == "skipped"
