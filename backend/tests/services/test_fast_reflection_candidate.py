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


def test_fast_reflection_prefers_llm_classification_over_marker_fallback(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-smart",
        messages=[{"role": "user", "content": "不是，我只是说这个 deploy checklist 以后重复执行。"}],
        metadata={
            "skill_candidate_loop_enabled": False,
            "fast_reflection_classification": {
                "method": "llm_classifier",
                "signal_type": "repeated_task_pattern",
                "lesson": "Reusable deploy checklist: build, migrate, restart, then verify.",
                "confidence": 0.86,
            },
        },
    )

    workspace = tmp_path / str(agent_id)
    candidate = load_evolution_ledger(workspace)[0]

    assert result["status"] == "candidate_created"
    assert result["signal_type"] == "repeated_task_pattern"
    assert result["classification_method"] == "llm_classifier"
    assert candidate["metadata"]["classification_method"] == "llm_classifier"
    assert candidate["metadata"]["lesson"] == "Reusable deploy checklist: build, migrate, restart, then verify."


def test_fast_reflection_persists_learning_brain_decision(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-brain",
        messages=[{"role": "user", "content": "下次部署按 build -> migrate -> restart -> healthcheck。"}],
        metadata={
            "skill_candidate_loop_enabled": False,
            "fast_reflection_classification": {
                "method": "learning_brain_agent",
                "signal_type": "repeated_task_pattern",
                "lesson": "Reuse the governed deploy flow: build, migrate, restart, then healthcheck.",
                "confidence": 0.92,
                "learning_brain_decision": {
                    "schema": "fast_reflection_learning_brain_decision.v1",
                    "signal_type": "repeated_task_pattern",
                    "lesson": "Reuse the governed deploy flow: build, migrate, restart, then healthcheck.",
                    "confidence": 0.92,
                    "container": "skill_candidate",
                    "promotion_intent": "candidate",
                    "rationale": "The user corrected a repeated deployment procedure.",
                    "evidence_refs": ["message:0"],
                    "boundary_checks": {
                        "no_credentials": True,
                        "not_direct_memory_write": True,
                    },
                },
            },
        },
    )

    workspace = tmp_path / str(agent_id)
    candidate = load_evolution_ledger(workspace)[0]

    assert result["classification_method"] == "learning_brain_agent"
    assert candidate["metadata"]["learning_brain_decision"]["container"] == "skill_candidate"
    assert candidate["metadata"]["learning_brain_decision"]["promotion_intent"] == "candidate"
    assert candidate["metadata"]["learning_brain_decision"]["evidence_refs"] == ["message:0"]


def test_fast_reflection_llm_low_signal_suppresses_marker_fallback(tmp_path) -> None:
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-low",
        messages=[{"role": "user", "content": "不是，这句话不是长期偏好，只是当前上下文。"}],
        metadata={
            "fast_reflection_classification": {
                "method": "llm_classifier",
                "signal_type": "low_signal",
                "lesson": "",
                "confidence": 0.91,
            },
        },
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


def test_skill_candidate_loop_flag_disables_skill_bridge_only(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate
    from app.services.session_learning import render_active_session_learning_projection

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-disabled",
        messages=[{"role": "user", "content": "deploy steps: build then migrate then restart, same as last time"}],
        metadata={
            "repeated_workflow_signature": "deploy -> build -> migrate -> restart",
            "skill_candidate_loop_enabled": False,
        },
    )

    assert result["status"] == "candidate_created"
    assert result["signal_type"] == "repeated_task_pattern"
    assert result["skill_candidate"] == {
        "status": "skipped",
        "reason": "skill_candidate_loop_disabled",
    }

    workspace = tmp_path / str(agent_id)
    candidate_targets = [
        entry.get("target_type")
        for entry in load_evolution_ledger(workspace)
        if entry.get("event") == "candidate"
    ]
    assert candidate_targets == ["fast_reflection"]
    assert not (workspace / "evolution" / "skill_candidates").exists()
    projection = render_active_session_learning_projection(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-disabled",
    )
    assert "deploy" in projection


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
