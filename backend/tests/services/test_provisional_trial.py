from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_candidate_manifest(workspace: Path, candidate_id: str, skill_name: str, target_path: str) -> None:
    path = workspace / "evolution" / "skill_candidates" / candidate_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "skill_candidate_package.v1",
                "candidate_id": candidate_id,
                "skill_name": skill_name,
                "target_path": target_path,
                "status": "provisional",
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )


def test_trial_path_rejects_unsafe_candidate_identity_instead_of_normalizing_collision() -> None:
    from app.services.provisional_trial import trial_rel_path

    with pytest.raises(ValueError, match="safe path component"):
        trial_rel_path("../candidate")


def _install_provisional(
    workspace: Path,
    *,
    candidate_id: str = "cand-trial",
    skill_name: str = "Demo Skill",
    baseline: bytes | None = b"# Demo\nold\n",
    candidate: bytes = b"# Demo\nnew\n",
) -> tuple[str, str]:
    from app.services.agent_asset_transaction import AgentAssetTransaction
    from app.services.provisional_trial import initialize_provisional_trial
    from app.services.skill_evolution_registry import ORIGIN_T3_AUTO_CREATED, upsert_skill_evolution_entry

    target_path = "skills/demo-skill/SKILL.md"
    target = workspace / target_path
    if baseline is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(baseline)
        baseline_entry = upsert_skill_evolution_entry(
            workspace,
            skill_name=skill_name,
            target_path=target_path,
            skill_origin=ORIGIN_T3_AUTO_CREATED,
            state="active",
        )
    else:
        baseline_entry = None
    _write_candidate_manifest(workspace, candidate_id, skill_name, target_path)

    with AgentAssetTransaction(workspace, operation="test_provisional_install") as transaction:
        transaction.stage_bytes(target_path, candidate)
        upsert_skill_evolution_entry(
            workspace,
            skill_name=skill_name,
            target_path=target_path,
            skill_origin=ORIGIN_T3_AUTO_CREATED,
            state="provisional",
            last_candidate_id=candidate_id,
            metadata={"commit_status": "provisional"},
            transaction=transaction,
        )
        initialize_provisional_trial(
            workspace,
            candidate_id=candidate_id,
            skill_name=skill_name,
            target_path=target_path,
            candidate_content=candidate,
            baseline_content=baseline,
            baseline_registry_entry=baseline_entry,
            started_at="2026-07-11T00:00:00+00:00",
            transaction=transaction,
        )
        transaction.commit()
    return skill_name, target_path


def _runtime_signal(
    workspace: Path,
    *,
    status: str,
    minute: int,
    trace_id: str,
) -> dict:
    from app.services.skill_lifecycle import record_skill_runtime_usage

    return record_skill_runtime_usage(
        workspace,
        skill_name="Demo Skill",
        loaded_skill_names=["Demo Skill"],
        tool_names=["load_skill", "read_file"],
        status=status,
        note=f"trial {status}",
        source="test",
        session_id="session-1",
        runtime_task_id="task-1",
        trace_id=trace_id,
        occurred_at=f"2026-07-11T00:{minute:02d}:00+00:00",
    )


def test_provisional_trial_promotes_after_three_distinct_successes(tmp_path: Path) -> None:
    from app.services.provisional_trial import load_provisional_trial
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    workspace = tmp_path / "agent"
    _, target_path = _install_provisional(workspace)

    first = _runtime_signal(workspace, status="success", minute=1, trace_id="trace-1")
    second = _runtime_signal(workspace, status="success", minute=2, trace_id="trace-2")
    third = _runtime_signal(workspace, status="success", minute=3, trace_id="trace-3")

    trial = load_provisional_trial(workspace, "cand-trial")
    entry = get_skill_evolution_entry(workspace, "Demo Skill")
    manifest = json.loads(
        (workspace / "evolution/skill_candidates/cand-trial/manifest.json").read_text(encoding="utf-8")
    )
    assert first["trial_decision"] == "continue_trial"
    assert second["trial_decision"] == "continue_trial"
    assert third["trial_decision"] == "promoted"
    assert trial is not None and trial["state"] == "promoted"
    assert len(trial["signals"]["positive"]) == 3
    assert entry is not None and entry["state"] == "active"
    assert entry["metadata"]["commit_status"] == "active"
    assert manifest["status"] == "promoted"
    assert (workspace / target_path).read_bytes() == b"# Demo\nnew\n"


def test_provisional_trial_deduplicates_runtime_evidence(tmp_path: Path) -> None:
    from app.services.provisional_trial import load_provisional_trial

    workspace = tmp_path / "agent"
    _install_provisional(workspace)

    first = _runtime_signal(workspace, status="success", minute=1, trace_id="same-trace")
    duplicate = _runtime_signal(workspace, status="success", minute=1, trace_id="same-trace")

    trial = load_provisional_trial(workspace, "cand-trial")
    assert first["positive_signal_count"] == 1
    assert duplicate["trial_decision"] == "duplicate_signal"
    assert trial is not None and len(trial["signals"]["positive"]) == 1


def test_provisional_patch_rolls_back_real_content_and_registry(tmp_path: Path) -> None:
    from app.services.evolution_ledger import load_evolution_ledger
    from app.services.provisional_trial import load_provisional_trial
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    workspace = tmp_path / "agent"
    _, target_path = _install_provisional(workspace)

    first = _runtime_signal(workspace, status="failed", minute=1, trace_id="trace-fail-1")
    second = _runtime_signal(workspace, status="failed", minute=2, trace_id="trace-fail-2")

    trial = load_provisional_trial(workspace, "cand-trial")
    entry = get_skill_evolution_entry(workspace, "Demo Skill")
    manifest = json.loads(
        (workspace / "evolution/skill_candidates/cand-trial/manifest.json").read_text(encoding="utf-8")
    )
    rollback_events = [
        item for item in load_evolution_ledger(workspace) if item.get("schema") == "evolution_rollback_event.v1"
    ]
    assert first["trial_decision"] == "continue_trial"
    assert second["trial_decision"] == "rolled_back"
    assert (workspace / target_path).read_bytes() == b"# Demo\nold\n"
    assert trial is not None and trial["state"] == "rolled_back"
    assert entry is not None and entry["state"] == "active"
    assert entry["active_version_hash"] == trial["rollback"]["baseline_version_hash"]
    assert manifest["status"] == "rolled_back"
    assert rollback_events[-1]["restored_ref"] == trial["rollback"]["ref"]


def test_provisional_new_skill_rollback_deletes_candidate_version(tmp_path: Path) -> None:
    from app.services.provisional_trial import load_provisional_trial
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    workspace = tmp_path / "agent"
    _, target_path = _install_provisional(workspace, baseline=None)

    _runtime_signal(workspace, status="failed", minute=1, trace_id="trace-fail-1")
    result = _runtime_signal(workspace, status="workaround", minute=2, trace_id="trace-fail-2")

    trial = load_provisional_trial(workspace, "cand-trial")
    entry = get_skill_evolution_entry(workspace, "Demo Skill")
    assert result["trial_decision"] == "rolled_back"
    assert not (workspace / target_path).exists()
    assert trial is not None and trial["rollback"]["action"] == "delete"
    assert entry is not None and entry["state"] == "rolled_back"


def test_provisional_trial_version_drift_blocks_automatic_decision(tmp_path: Path) -> None:
    from app.services.provisional_trial import load_provisional_trial
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    workspace = tmp_path / "agent"
    _, target_path = _install_provisional(workspace)
    manually_edited = b"# Demo\nmanual owner edit\n"
    (workspace / target_path).write_bytes(manually_edited)

    result = _runtime_signal(workspace, status="success", minute=1, trace_id="trace-drift")

    trial = load_provisional_trial(workspace, "cand-trial")
    entry = get_skill_evolution_entry(workspace, "Demo Skill")
    assert result["trial_decision"] == "needs_review"
    assert (workspace / target_path).read_bytes() == manually_edited
    assert trial is not None and trial["state"] == "needs_review"
    assert entry is not None and entry["state"] == "needs_review"


def test_legacy_provisional_without_backup_never_claims_false_rollback(tmp_path: Path) -> None:
    from app.services.provisional_trial import load_provisional_trial
    from app.services.skill_evolution_registry import (
        ORIGIN_T3_AUTO_CREATED,
        get_skill_evolution_entry,
        upsert_skill_evolution_entry,
    )

    workspace = tmp_path / "agent"
    target_path = "skills/demo-skill/SKILL.md"
    target = workspace / target_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# Demo\nlegacy provisional\n")
    _write_candidate_manifest(workspace, "legacy-candidate", "Demo Skill", target_path)
    upsert_skill_evolution_entry(
        workspace,
        skill_name="Demo Skill",
        target_path=target_path,
        skill_origin=ORIGIN_T3_AUTO_CREATED,
        state="provisional",
        last_candidate_id="legacy-candidate",
    )

    _runtime_signal(workspace, status="failed", minute=1, trace_id="trace-fail-1")
    result = _runtime_signal(workspace, status="failed", minute=2, trace_id="trace-fail-2")

    trial = load_provisional_trial(workspace, "legacy-candidate")
    entry = get_skill_evolution_entry(workspace, "Demo Skill")
    assert result["trial_decision"] == "needs_review"
    assert target.read_bytes() == b"# Demo\nlegacy provisional\n"
    assert trial is not None and trial["rollback"]["action"] == "manual_review"
    assert entry is not None and entry["state"] == "needs_review"


def test_provisional_trial_expiry_requires_review_instead_of_late_promotion(tmp_path: Path) -> None:
    from app.services.provisional_trial import load_provisional_trial
    from app.services.skill_evolution_registry import get_skill_evolution_entry

    workspace = tmp_path / "agent"
    _install_provisional(workspace)

    from app.services.skill_lifecycle import record_skill_runtime_usage

    result = record_skill_runtime_usage(
        workspace,
        skill_name="Demo Skill",
        loaded_skill_names=["Demo Skill"],
        tool_names=["load_skill"],
        status="success",
        note="arrived after the bounded trial window",
        source="test",
        trace_id="late-trace",
        occurred_at="2026-07-26T00:00:00+00:00",
    )

    trial = load_provisional_trial(workspace, "cand-trial")
    entry = get_skill_evolution_entry(workspace, "Demo Skill")
    assert result["trial_decision"] == "needs_review"
    assert trial is not None and trial["state"] == "needs_review"
    assert entry is not None and entry["state"] == "needs_review"
