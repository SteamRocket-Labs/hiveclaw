from app.services.skill_lifecycle import SkillCandidateRecord


def _record(
    name: str,
    *,
    status: str = "success",
    blocker: str = "",
    promote: int = 0,
    patch: int = 0,
    updated: str = "2026-07-10T00:00:00+00:00",
) -> SkillCandidateRecord:
    return SkillCandidateRecord(
        skill_name=name,
        workflow_signature=name,
        promote_candidates=[str(index) for index in range(promote)],
        patch_candidates=[str(index) for index in range(patch)],
        last_status=status,
        last_note="",
        blocker=blocker,
        last_updated_at=updated,
    )


def test_candidate_stage_keeps_every_record_visible_and_thresholds_are_observations_only() -> None:
    from app.services.skill_distillation_stages import rank_skill_candidates

    records = [
        _record("promote", promote=3),
        _record("patch-old", patch=2, updated="2026-07-09T00:00:00+00:00"),
        _record("patch-new", patch=3, updated="2026-07-10T00:00:00+00:00"),
        _record("blocked", patch=8, blocker="unsafe"),
        _record("mixed", promote=5, patch=1),
    ]

    ranked = rank_skill_candidates(records)

    assert [item.skill_name for item in ranked.reviewable] == [
        "promote",
        "patch-old",
        "patch-new",
        "blocked",
        "mixed",
    ]
    assert records[0].promote_candidates == ["0", "1", "2"]


def test_cursor_stage_is_deterministic_for_reordered_evidence() -> None:
    from app.services.skill_distillation_stages import advance_distiller_cursor

    rows = [
        {"occurred_at": "2026-07-09T10:00:00+00:00", "session_id": "b"},
        {"occurred_at": "2026-07-10T10:00:00+00:00", "session_id": "a"},
    ]

    assert advance_distiller_cursor(("2026-07-08T00:00:00+00:00", "old"), rows) == (
        "2026-07-10T10:00:00+00:00",
        "a",
    )
    assert advance_distiller_cursor(("2026-07-08T00:00:00+00:00", "old"), reversed(rows)) == (
        "2026-07-10T10:00:00+00:00",
        "a",
    )
