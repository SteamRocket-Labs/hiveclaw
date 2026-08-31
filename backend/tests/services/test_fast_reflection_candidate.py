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
        metadata={
            "tenant_id": str(uuid.uuid4()),
            "final_response": "收到。",
            "skill_candidate_loop_enabled": False,
            "fast_reflection_classification": {
                "method": "learning_brain_agent",
                "signal_type": "user_preference_correction",
                "lesson": "用这个项目时以后都用 npm，不要用 yarn。",
                "confidence": 0.97,
                "learning_brain_decision": {
                    "container": "memory_candidate",
                    "promotion_intent": "candidate",
                },
            },
        },
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


def test_fast_reflection_preserves_full_model_judgment_and_evidence(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    lesson_tail = "LESSON_DECISIVE_TAIL"
    earliest_tail = "EARLIEST_EVIDENCE_TAIL"
    final_tail = "FINAL_RESPONSE_DECISIVE_TAIL"
    lesson = "l" * 1400 + lesson_tail
    messages = [
        {"role": "user", "content": "e" * 700 + earliest_tail},
        *({"role": "assistant", "content": f"message-{index}"} for index in range(7)),
    ]

    create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-full",
        messages=messages,
        metadata={
            "skill_candidate_loop_enabled": False,
            "final_response": "f" * 1400 + final_tail,
            "fast_reflection_classification": {
                "method": "learning_brain_agent",
                "signal_type": "workflow_correction",
                "lesson": lesson,
                "confidence": 0.95,
            },
        },
    )

    candidate = load_evolution_ledger(tmp_path / str(agent_id))[0]
    metadata = candidate["metadata"]
    assert metadata["lesson"] == lesson
    assert earliest_tail in metadata["message_digest"]
    assert final_tail in metadata["final_response"]


def test_fast_reflection_marker_fallback_is_not_a_learning_candidate(tmp_path) -> None:
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-marker",
        messages=[
            {"role": "assistant", "content": "I will always use yarn for this repo."},
            {"role": "user", "content": "不是，用这个项目时以后都用 npm，不要用 yarn。"},
        ],
        metadata={},
    )

    assert result == {"status": "skipped", "reason": "low_signal"}
    assert not (tmp_path / str(agent_id) / "evolution" / "evolution_ledger.jsonl").exists()


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


def test_heartbeat_reflection_never_creates_fast_reflection_candidate(tmp_path) -> None:
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="heartbeat-reflection-session",
        messages=[
            {"role": "user", "content": "Heartbeat reflected on a distillation cycle."},
            {"role": "assistant", "content": "Reusable deploy checklist: build, migrate, restart, verify."},
        ],
        metadata={
            "source": "heartbeat_reflection",
            "skill_candidate_loop_enabled": True,
            "fast_reflection_classification": {
                "method": "learning_brain_agent",
                "signal_type": "repeated_task_pattern",
                "lesson": "Reusable deploy checklist: build, migrate, restart, verify.",
                "confidence": 0.95,
            },
        },
    )

    assert result == {"status": "skipped", "reason": "system_reflection_source"}
    workspace = tmp_path / str(agent_id)
    assert not (workspace / "evolution" / "evolution_ledger.jsonl").exists()
    assert not (workspace / "memory").exists()
    assert not (workspace / "skills").exists()


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


def test_model_nominated_repeated_workflow_bridges_to_skill_candidate(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-wf",
        messages=[{"role": "user", "content": "deploy steps: build then migrate then restart, same as last time"}],
        metadata={
            "repeated_workflow_signature": "deploy -> build -> migrate -> restart",
            "fast_reflection_classification": {
                "method": "learning_brain_agent",
                "signal_type": "repeated_task_pattern",
                "lesson": "Use the governed deploy sequence: build, migrate, restart, verify.",
                "confidence": 0.94,
                "learning_brain_decision": {
                    "container": "skill_candidate",
                    "promotion_intent": "candidate",
                    "skill_decision": {
                        "action": "new",
                        "candidate_name": "governed-deploy-workflow",
                        "target_skill": "",
                        "reason": "The complete evidence shows a reusable procedure.",
                    },
                },
            },
        },
    )

    assert result["status"] == "candidate_created"
    assert result["signal_type"] == "repeated_task_pattern"
    skill = result["skill_candidate"]
    assert skill["status"] == "skill_candidate_created"
    assert skill["verification_passed"] is True

    workspace = tmp_path / str(agent_id)
    candidate_targets = {
        entry.get("target_type") for entry in load_evolution_ledger(workspace) if entry.get("event") == "candidate"
    }
    assert {"fast_reflection", "skill_candidate"} <= candidate_targets
    candidate_dir = workspace / "evolution" / "skill_candidates" / skill["candidate_id"]
    assert (candidate_dir / "candidate_signal.md").exists()
    assert not (candidate_dir / "SKILL.md.draft").exists()
    assert (candidate_dir / "manifest.json").exists()
    assert not (candidate_dir / "SKILL.md").exists()


def test_repeated_workflow_metadata_is_evidence_not_a_semantic_fallback(tmp_path) -> None:
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    agent_id = uuid.uuid4()
    result = create_fast_reflection_candidate(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="session-wf-no-model",
        messages=[{"role": "user", "content": "deploy steps: build then migrate then restart"}],
        metadata={"repeated_workflow_signature": "deploy -> build -> migrate -> restart"},
    )

    assert result == {"status": "skipped", "reason": "low_signal"}
    assert not (tmp_path / str(agent_id) / "evolution" / "evolution_ledger.jsonl").exists()


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
            "fast_reflection_classification": {
                "method": "learning_brain_agent",
                "signal_type": "repeated_task_pattern",
                "lesson": "Use the governed deploy sequence: build, migrate, restart, verify.",
                "confidence": 0.94,
                "learning_brain_decision": {
                    "container": "skill_candidate",
                    "promotion_intent": "candidate",
                    "skill_decision": {
                        "action": "new",
                        "candidate_name": "governed-deploy-workflow",
                        "target_skill": "",
                        "reason": "Reusable procedure.",
                    },
                },
            },
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
        entry.get("target_type") for entry in load_evolution_ledger(workspace) if entry.get("event") == "candidate"
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
        metadata={
            "skill_candidate_loop_enabled": True,
            "fast_reflection_classification": {
                "method": "learning_brain_agent",
                "signal_type": "user_preference_correction",
                "lesson": "以后不要用 emoji",
                "confidence": 0.96,
                "learning_brain_decision": {
                    "container": "memory_candidate",
                    "promotion_intent": "candidate",
                },
            },
        },
    )

    assert result["status"] == "candidate_created"
    assert result["signal_type"] == "user_preference_correction"
    assert result["skill_candidate"]["status"] == "skipped"
    assert result["skill_candidate"]["reason"] == "model_did_not_nominate_skill"


def test_fast_reflection_replay_of_same_committed_response_is_idempotent(tmp_path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.fast_reflection_service import create_fast_reflection_candidate
    from app.services.session_learning import load_session_learning_projections

    agent_id = uuid.uuid4()
    session_id = "session-response-replay"
    response_commit = {
        "schema": "hive.response_commit.v1",
        "committed": True,
        "commit_kind": "web_chat_terminal_outcome",
        "idempotency_key": "response-complete:run-1:result-1",
        "source_refs": ["runtime_task:run-1", "model_result:result-1"],
    }
    call = {
        "data_root": tmp_path,
        "agent_id": agent_id,
        "session_id": session_id,
        "messages": [{"role": "user", "content": "下次这个项目统一使用 pnpm。"}],
        "metadata": {
            "source": "web",
            "source_refs": response_commit["source_refs"],
            "response_commit": response_commit,
            "skill_candidate_loop_enabled": False,
            "fast_reflection_classification": {
                "method": "learning_brain_agent",
                "signal_type": "user_preference_correction",
                "lesson": "Use pnpm for this repository.",
                "confidence": 0.99,
            },
        },
    }

    first = create_fast_reflection_candidate(**call)
    replay = create_fast_reflection_candidate(**call)

    ledger_candidates = [
        entry
        for entry in load_evolution_ledger(tmp_path / str(agent_id))
        if entry.get("event") == "candidate" and entry.get("target_type") == "fast_reflection"
    ]
    projections = load_session_learning_projections(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
    )

    assert {
        "returned_candidate_ids": [first.get("candidate_id"), replay.get("candidate_id")],
        "ledger_candidate_ids": [entry.get("candidate_id") for entry in ledger_candidates],
        "projection_candidate_ids": [entry.get("candidate_id") for entry in projections],
    } == {
        "returned_candidate_ids": [first["candidate_id"], first["candidate_id"]],
        "ledger_candidate_ids": [first["candidate_id"]],
        "projection_candidate_ids": [first["candidate_id"]],
    }
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
