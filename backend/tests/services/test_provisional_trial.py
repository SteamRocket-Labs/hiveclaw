from __future__ import annotations

from pathlib import Path


def test_provisional_trial_rolls_back_after_negative_signal_threshold(tmp_path: Path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.provisional_trial import evaluate_provisional_trial_signal

    workspace = tmp_path / "agent"

    first = evaluate_provisional_trial_signal(
        workspace=workspace,
        candidate_id="cand-1",
        rollback_ref="skills/demo/SKILL.md",
        signal={"kind": "negative_feedback", "reason": "user marked skill misleading"},
        negative_signal_count=1,
        negative_signal_threshold=2,
    )
    second = evaluate_provisional_trial_signal(
        workspace=workspace,
        candidate_id="cand-1",
        rollback_ref="skills/demo/SKILL.md",
        signal={"kind": "negative_feedback", "reason": "tool failure after skill use"},
        negative_signal_count=2,
        negative_signal_threshold=2,
    )

    entries = load_evolution_ledger(workspace)
    rollback_events = [entry for entry in entries if entry.get("schema") == "evolution_rollback_event.v1"]

    assert first["decision"] == "continue_trial"
    assert second["decision"] == "rolled_back"
    assert len(rollback_events) == 1
    assert rollback_events[0]["candidate_id"] == "cand-1"
    assert rollback_events[0]["restored_ref"] == "skills/demo/SKILL.md"
    assert rollback_events[0]["metadata"]["source"] == "provisional_trial"
    assert rollback_events[0]["metadata"]["signal"]["reason"] == "tool failure after skill use"


def test_provisional_skill_runtime_failures_trigger_rollback(tmp_path: Path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.skill_evolution_registry import ORIGIN_T3_AUTO_CREATED, upsert_skill_evolution_entry
    from app.services.skill_lifecycle import record_skill_runtime_usage

    workspace = tmp_path / "agent"
    upsert_skill_evolution_entry(
        workspace,
        skill_name="Demo Skill",
        target_path="skills/demo-skill/SKILL.md",
        skill_origin=ORIGIN_T3_AUTO_CREATED,
        evolvable=True,
        last_candidate_id="cand-provisional",
        state="provisional",
        metadata={"committed_by": "skill_distiller", "commit_status": "provisional"},
    )

    first = record_skill_runtime_usage(
        workspace,
        skill_name="Demo Skill",
        loaded_skill_names=["Demo Skill"],
        tool_names=["read_file"],
        status="failed",
        note="first provisional failure",
        source="test",
        occurred_at="2026-07-02T00:00:00+00:00",
    )
    second = record_skill_runtime_usage(
        workspace,
        skill_name="Demo Skill",
        loaded_skill_names=["Demo Skill"],
        tool_names=["read_file"],
        status="failed",
        note="second provisional failure",
        source="test",
        occurred_at="2026-07-02T00:01:00+00:00",
    )

    rollback_events = [
        entry for entry in load_evolution_ledger(workspace) if entry.get("schema") == "evolution_rollback_event.v1"
    ]

    assert first.get("trial_decision") is None
    assert second["trial_decision"] == "rolled_back"
    assert rollback_events[-1]["candidate_id"] == "cand-provisional"
    assert rollback_events[-1]["restored_ref"] == "skills/demo-skill/SKILL.md"
