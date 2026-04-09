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


def test_record_skill_execution_promotes_candidate_after_two_recent_successes(tmp_path: Path) -> None:
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

    candidates = (tmp_path / "evolution" / "skill_candidates.md").read_text(encoding="utf-8")
    review = (tmp_path / "evolution" / "skill_review.md").read_text(encoding="utf-8")

    assert first["decision"] == "candidate"
    assert second["decision"] == "promote"
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
