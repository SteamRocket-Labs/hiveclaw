from __future__ import annotations

import json
import multiprocessing
from pathlib import Path


def _skill_usage_writer(workspace: str, index: int) -> None:
    from app.services.skill_lifecycle import record_skill_runtime_usage

    record_skill_runtime_usage(
        Path(workspace),
        skill_name=f"skill-{index}",
        loaded_skill_names=[f"skill-{index}"],
        tool_names=["load_skill", f"tool_{index}"],
        status="success",
        note=f"concurrent usage {index}",
        source="concurrency_test",
        occurred_at=f"2026-07-11T10:00:{index:02d}Z",
        session_id=f"session-{index}",
    )


def _skill_candidate_packages(workspace: Path) -> list[Path]:
    root = workspace / "evolution" / "skill_candidates"
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


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


def test_record_skill_execution_keeps_repeated_successes_as_model_review_evidence(tmp_path: Path) -> None:
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

    review = (tmp_path / "evolution" / "skill_review.md").read_text(encoding="utf-8")
    packages = _skill_candidate_packages(tmp_path)
    manifests = [json.loads((package / "manifest.json").read_text(encoding="utf-8")) for package in packages]

    assert first["decision"] == "candidate"
    assert second["decision"] == "candidate"
    assert third["decision"] == "candidate"
    assert not (tmp_path / "evolution" / "skill_candidates.md").exists()
    assert {manifest["skill_name"] for manifest in manifests} == {"deploy-checklist"}
    assert manifests[0]["status"] == "candidate"
    assert manifests[0]["metadata"]["workflow_signature"] == "deploy-checklist"
    assert (packages[0] / "candidate_signal.md").exists()
    assert not (packages[0] / "SKILL.md.draft").exists()
    assert (packages[0] / "skill_pitch.md").exists()
    assert (packages[0] / "eval_plan.md").exists()
    assert (packages[0] / "failure_cases.md").exists()
    assert manifests[0]["metadata"]["promote_candidate_count"] == 3
    assert "[candidate] deploy-checklist" in review


def test_record_skill_execution_keeps_repeated_failures_as_model_review_evidence(tmp_path: Path) -> None:
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

    review = (tmp_path / "evolution" / "skill_review.md").read_text(encoding="utf-8")
    packages = _skill_candidate_packages(tmp_path)
    manifest = json.loads((packages[0] / "manifest.json").read_text(encoding="utf-8"))

    assert first["decision"] == "candidate"
    assert second["decision"] == "candidate"
    assert not (tmp_path / "evolution" / "skill_candidates.md").exists()
    assert manifest["skill_name"] == "incident-response"
    assert manifest["status"] == "candidate"
    assert manifest["metadata"]["patch_candidate_count"] == 2
    assert "[candidate] incident-response" in review


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
    packages = _skill_candidate_packages(tmp_path)
    manifest = json.loads((packages[0] / "manifest.json").read_text(encoding="utf-8"))

    assert first["decision"] == "candidate"
    assert second["decision"] == "candidate"
    assert first["workflow_signature"] == second["workflow_signature"] == "load_skill+read_file+write_file"
    assert '"source": "web_chat"' in usage_log
    assert '"session_id": "session-1"' in usage_log
    assert not (tmp_path / "evolution" / "skill_candidates.md").exists()
    assert manifest["metadata"]["patch_candidate_count"] == 2


def test_record_skill_runtime_usage_does_not_author_patch_evidence_from_counts(tmp_path: Path) -> None:
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

    assert result["decision"] == "candidate"
    assert "evidence_ref" not in result
    assert not (tmp_path / "evolution" / "skill_promotion_evidence.jsonl").exists()


def test_record_skill_runtime_usage_does_not_author_promotion_from_counts(tmp_path: Path) -> None:
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

    assert result["decision"] == "candidate"
    assert "evidence_ref" not in result
    assert not (tmp_path / "evolution" / "skill_promotion_evidence.jsonl").exists()


def test_record_skill_runtime_usage_preserves_noop_as_model_review_evidence(tmp_path: Path) -> None:
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

    assert result["decision"] == "candidate"
    assert not (tmp_path / "evolution" / "skill_candidates.md").exists()
    assert (tmp_path / "evolution" / "skill_usage.jsonl").exists()
    manifests = list((tmp_path / "evolution" / "skill_candidates").glob("*/manifest.json"))
    assert len(manifests) == 1
    package = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert package["metadata"]["last_status"] == "noop"


def test_skill_runtime_usage_serializes_usage_review_and_candidates_across_processes(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import read_agent_asset_revision

    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_skill_usage_writer, args=(str(tmp_path), index)) for index in range(10)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    usage_rows = (tmp_path / "evolution/skill_usage.jsonl").read_text(encoding="utf-8").splitlines()
    review_rows = [
        line
        for line in (tmp_path / "evolution/skill_review.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]
    manifests = list((tmp_path / "evolution/skill_candidates").glob("*/manifest.json"))
    assert len(usage_rows) == 10
    assert len(review_rows) == 10
    assert len(manifests) == 10
    assert read_agent_asset_revision(tmp_path) == 10
