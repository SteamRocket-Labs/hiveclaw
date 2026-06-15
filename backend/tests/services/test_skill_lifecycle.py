from __future__ import annotations

from pathlib import Path


def test_record_skill_lifecycle_event_appends_review_log(tmp_path: Path) -> None:
    from app.services.skill_lifecycle import record_skill_lifecycle_event

    record_skill_lifecycle_event(
        tmp_path,
        skill_name="deploy-checklist",
        status="promoted",
        note="Saved via save_skill.",
    )
    record_skill_lifecycle_event(
        tmp_path,
        skill_name="deploy-checklist",
        status="patched",
        note="Updated rollback guidance.",
    )

    review_path = tmp_path / "evolution" / "skill_review.md"
    content = review_path.read_text(encoding="utf-8")

    assert review_path == Path(tmp_path) / "evolution" / "skill_review.md"
    assert "# Skill Review" in content
    assert "[promoted] deploy-checklist: Saved via save_skill." in content
    assert "[patched] deploy-checklist: Updated rollback guidance." in content


def test_record_skill_execution_promotes_candidate_after_three_recent_successes(tmp_path: Path) -> None:
    from app.services.skill_lifecycle import record_skill_execution

    first = record_skill_execution(
        tmp_path,
        skill_name="deploy-checklist",
        workflow_signature="deploy-checklist",
        status="success",
        used_skill=False,
        note="First stable run.",
        occurred_at="2026-04-01T10:00:00Z",
    )
    second = record_skill_execution(
        tmp_path,
        skill_name="deploy-checklist",
        workflow_signature="deploy-checklist",
        status="success",
        used_skill=False,
        note="Second stable run.",
        occurred_at="2026-04-05T10:00:00Z",
    )
    third = record_skill_execution(
        tmp_path,
        skill_name="deploy-checklist",
        workflow_signature="deploy-checklist",
        status="success",
        used_skill=False,
        note="Third stable run.",
        occurred_at="2026-04-09T10:00:00Z",
    )

    candidates = (tmp_path / "evolution" / "skill_candidates.md").read_text(encoding="utf-8")
    review = (tmp_path / "evolution" / "skill_review.md").read_text(encoding="utf-8")

    assert first["decision"] == "candidate"
    assert second["decision"] == "candidate"
    assert third["decision"] == "promote"
    assert "workflow_signature: deploy-checklist" in candidates
    assert "[promote] deploy-checklist" in review


def test_record_skill_execution_marks_patch_candidate_after_repeated_loaded_failures(tmp_path: Path) -> None:
    from app.services.skill_lifecycle import record_skill_execution

    first = record_skill_execution(
        tmp_path,
        skill_name="incident-response",
        workflow_signature="incident-response",
        status="failed",
        used_skill=True,
        note="Loaded skill still missed rollback notes.",
        blocker="missing rollback guidance",
        occurred_at="2026-04-01T10:00:00Z",
    )
    second = record_skill_execution(
        tmp_path,
        skill_name="incident-response",
        workflow_signature="incident-response",
        status="workaround",
        used_skill=True,
        note="Temporary workaround added rollback notes manually.",
        blocker="missing rollback guidance",
        occurred_at="2026-04-03T10:00:00Z",
    )

    candidates = (tmp_path / "evolution" / "skill_candidates.md").read_text(encoding="utf-8")
    review = (tmp_path / "evolution" / "skill_review.md").read_text(encoding="utf-8")

    assert first["decision"] == "candidate"
    assert second["decision"] == "patch"
    assert "patch_candidate_count: 2" in candidates
    assert "[patch] incident-response" in review


def test_record_skill_runtime_usage_derives_workflow_and_patch_signal(tmp_path: Path) -> None:
    from app.services.skill_lifecycle import record_skill_runtime_usage

    first = record_skill_runtime_usage(
        tmp_path,
        skill_name="incident-response",
        loaded_skill_names=["incident-response"],
        tool_names=["load_skill", "read_file", "write_file"],
        status="failed",
        note="Loaded skill missed rollback notes.",
        occurred_at="2026-04-01T10:00:00Z",
        session_id="session-1",
        source="web_chat",
    )
    second = record_skill_runtime_usage(
        tmp_path,
        skill_name="incident-response",
        loaded_skill_names=["incident-response"],
        tool_names=["write_file", "load_skill", "read_file"],
        status="workaround",
        note="Manual workaround added rollback notes.",
        occurred_at="2026-04-02T10:00:00Z",
        session_id="session-2",
        source="web_chat",
    )

    usage_log = (tmp_path / "evolution" / "skill_usage.jsonl").read_text(encoding="utf-8")
    candidates = (tmp_path / "evolution" / "skill_candidates.md").read_text(encoding="utf-8")

    assert first["decision"] == "candidate"
    assert second["decision"] == "patch"
    assert first["workflow_signature"] == second["workflow_signature"] == "load_skill+read_file+write_file"
    assert '"source": "web_chat"' in usage_log
    assert '"session_id": "session-1"' in usage_log
    assert "patch_candidate_count: 2" in candidates


def test_record_skill_runtime_usage_writes_promotion_evidence_for_patch(tmp_path: Path) -> None:
    from app.services.skill_lifecycle import record_skill_runtime_usage

    record_skill_runtime_usage(
        tmp_path,
        skill_name="incident-response",
        loaded_skill_names=["incident-response"],
        tool_names=["load_skill", "read_file", "write_file"],
        status="failed",
        note="Loaded skill missed rollback notes.",
        occurred_at="2026-04-01T10:00:00Z",
        source="web_chat",
        session_id="session-1",
        runtime_task_id="rt-1",
        trace_id="trace-1",
    )
    result = record_skill_runtime_usage(
        tmp_path,
        skill_name="incident-response",
        loaded_skill_names=["incident-response"],
        tool_names=["write_file", "load_skill", "read_file"],
        status="workaround",
        note="Manual workaround added rollback notes.",
        occurred_at="2026-04-02T10:00:00Z",
        source="web_chat",
        session_id="session-2",
        runtime_task_id="rt-2",
        trace_id="trace-2",
    )

    evidence_path = tmp_path / "evolution" / "skill_promotion_evidence.jsonl"
    evidence = evidence_path.read_text(encoding="utf-8")

    assert result["decision"] == "patch"
    assert result["evidence_ref"] == "evolution/skill_promotion_evidence.jsonl"
    assert '"schema": "skill_promotion_evidence.v1"' in evidence
    assert '"decision": "patch"' in evidence
    assert '"runtime_task_id": "rt-2"' in evidence
    assert '"trace:trace-2"' in evidence


def test_record_skill_runtime_usage_writes_promotion_evidence_for_promote(tmp_path: Path) -> None:
    from app.services.skill_lifecycle import record_skill_runtime_usage

    for index in range(3):
        result = record_skill_runtime_usage(
            tmp_path,
            skill_name="deploy-checklist",
            loaded_skill_names=["deploy-checklist"],
            tool_names=["load_skill", "read_file"],
            status="success",
            note=f"Stable run {index}.",
            occurred_at=f"2026-04-0{index + 1}T10:00:00Z",
            source="trigger",
            session_id=f"session-{index}",
            runtime_task_id=f"rt-{index}",
            trace_id=f"trace-{index}",
        )

    evidence = (tmp_path / "evolution" / "skill_promotion_evidence.jsonl").read_text(encoding="utf-8")

    assert result["decision"] == "promote"
    assert '"decision": "promote"' in evidence
    assert '"skill_name": "deploy-checklist"' in evidence
    assert '"runtime_task:rt-2"' in evidence


def test_record_skill_runtime_usage_ignores_noop_without_polluting_candidates(tmp_path: Path) -> None:
    from app.services.skill_lifecycle import record_skill_runtime_usage

    result = record_skill_runtime_usage(
        tmp_path,
        skill_name="research",
        loaded_skill_names=["research"],
        tool_names=["load_skill"],
        status="noop",
        note="No durable outcome.",
        occurred_at="2026-04-01T10:00:00Z",
        session_id="session-1",
        source="web_chat",
    )

    assert result["decision"] == "ignored"
    assert not (tmp_path / "evolution" / "skill_candidates.md").exists()
    assert (tmp_path / "evolution" / "skill_usage.jsonl").exists()
