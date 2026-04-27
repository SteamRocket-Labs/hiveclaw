from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


def test_harness_validation_report_detects_complete_h4_h5_evidence(tmp_path):
    from app.services.evolution_ledger import (
        decide_promotion,
        record_eval_run,
        record_evolution_candidate,
        record_promotion_decision,
    )
    from app.services.evolution_validation import validate_evolution_ledger
    from app.services.harness_validation_report import audit_agent_harness_snapshot
    from app.services.long_task_runtime import append_long_task_progress_artifact, write_long_task_plan_artifact
    from app.services.long_task_validation import validate_long_task_run

    agent_id = uuid4()
    tenant_id = uuid4()
    runtime_task_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Research Agent",
        tenant_id=tenant_id,
        heartbeat_enabled=True,
    )
    runtime_task = SimpleNamespace(
        id=runtime_task_id,
        status="completed",
        task_type="objective",
        parent_agent_id=agent_id,
        child_agent_id=None,
        result_summary="completed",
        metadata_json={},
        created_at=None,
    )

    write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        objective_id="obj-1",
        spec="Run a long research task",
        acceptance_criteria=["report exists"],
        verification_commands=["pytest tests/services/test_harness_validation_report.py"],
        risk_gates=[],
        data_root=tmp_path,
    )
    append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="completed",
        delta="Report complete",
        output_paths=["workspace/report.md"],
        data_root=tmp_path,
    )
    validate_long_task_run(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        runtime_task={"status": "completed", "metadata": {}},
        data_root=tmp_path,
        write_report=True,
    )

    workspace = tmp_path / str(agent_id)
    candidate = record_evolution_candidate(
        workspace,
        target_type="skill",
        target_id="research-loop",
        diff="+ better research workflow",
        source_attempt_ids=[runtime_task_id.hex],
        baseline_version="v1",
    )
    eval_run = record_eval_run(
        workspace,
        candidate_id=candidate["candidate_id"],
        dataset="skill_distiller.internal",
        reward=0.91,
        baseline_reward=0.80,
        passed=True,
        traces=[runtime_task_id.hex],
        critical_regressions=0,
    )
    decision = decide_promotion(eval_run, min_reward_delta=0.05)
    record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision=decision["decision"],
        reason=decision["reason"],
        rollback_ref="skills/research-loop/SKILL.md@v1",
    )
    validate_evolution_ledger(workspace, write_report=True)

    report = audit_agent_harness_snapshot(
        agent=agent,
        runtime_tasks=[runtime_task],
        enabled_trigger_count=1,
        data_root=tmp_path,
    )

    assert report["agent_id"] == str(agent_id)
    assert report["h4"]["totals"]["long_tasks"] == 1
    assert report["h4"]["totals"]["passed"] == 1
    assert report["h4"]["long_tasks"][0]["validation_report_present"] is True
    assert report["h5"]["ledger_present"] is True
    assert report["h5"]["validation_report_present"] is True
    assert report["h5"]["validation_passed"] is True
    assert report["findings"] == []


def test_harness_validation_report_flags_missing_reports_and_failed_evolution(tmp_path):
    from app.services.evolution_ledger import record_evolution_candidate, record_promotion_decision
    from app.services.harness_validation_report import audit_agent_harness_snapshot
    from app.services.long_task_runtime import append_long_task_progress_artifact, write_long_task_plan_artifact

    agent_id = uuid4()
    runtime_task_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Ops Agent", tenant_id=uuid4(), heartbeat_enabled=True)
    runtime_task = SimpleNamespace(
        id=runtime_task_id,
        status="completed",
        task_type="objective",
        parent_agent_id=agent_id,
        child_agent_id=None,
        result_summary="done",
        metadata_json={},
        created_at=None,
    )

    write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        objective_id="obj-2",
        spec="Run ops task",
        acceptance_criteria=["evidence exists"],
        verification_commands=["pytest"],
        risk_gates=[],
        data_root=tmp_path,
    )
    append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="running",
        delta="Still running",
        data_root=tmp_path,
    )

    workspace = tmp_path / str(agent_id)
    candidate = record_evolution_candidate(
        workspace,
        target_type="skill",
        target_id="unsafe-skill",
        diff="+ unvalidated behavior",
        source_attempt_ids=[],
        baseline_version=None,
    )
    record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision="promote",
        reason="shortcut",
        rollback_ref=None,
    )

    report = audit_agent_harness_snapshot(
        agent=agent,
        runtime_tasks=[runtime_task],
        enabled_trigger_count=1,
        data_root=tmp_path,
    )

    categories = {finding["category"] for finding in report["findings"]}
    assert "long_task_validation_failed" in categories
    assert "long_task_validation_report_missing" in categories
    assert "evolution_validation_failed" in categories
    assert "evolution_validation_report_missing" in categories
    assert not (workspace / "evolution" / "evolution_validation_report.json").exists()
    assert not (
        tmp_path
        / str(agent_id)
        / "runtime_artifacts"
        / "long_tasks"
        / runtime_task_id.hex
        / "validation_report.json"
    ).exists()


def test_harness_validation_report_warns_autonomy_without_harness_evidence(tmp_path):
    from app.services.harness_validation_report import audit_agent_harness_snapshot

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Idle Agent", tenant_id=uuid4(), heartbeat_enabled=True)

    report = audit_agent_harness_snapshot(
        agent=agent,
        runtime_tasks=[],
        enabled_trigger_count=0,
        data_root=tmp_path,
    )

    assert report["h4"]["totals"]["long_tasks"] == 0
    assert report["h5"]["ledger_present"] is False
    assert "autonomy_without_harness_evidence" in {
        finding["category"] for finding in report["findings"]
    }


def test_build_harness_validation_payload_aggregates_agent_reports(tmp_path):
    from datetime import datetime, timezone

    from app.services.harness_validation_report import build_harness_validation_payload

    payload = build_harness_validation_payload(
        generated_at=datetime.now(timezone.utc),
        lookback_hours=168,
        agent_reports=[
            {
                "agent_id": "a1",
                "findings": [{"severity": "error", "category": "x"}],
                "h4": {"totals": {"long_tasks": 2, "passed": 1, "failed": 1, "validation_reports_present": 1}},
                "h5": {"ledger_present": True, "validation_passed": False, "validation_report_present": True},
            },
            {
                "agent_id": "a2",
                "findings": [{"severity": "warning", "category": "y"}],
                "h4": {"totals": {"long_tasks": 0, "passed": 0, "failed": 0, "validation_reports_present": 0}},
                "h5": {"ledger_present": False, "validation_passed": None, "validation_report_present": False},
            },
        ],
    )

    assert payload["schema"] == "harness_validation_report.v1"
    assert payload["lookback_hours"] == 168
    assert payload["totals"]["agents"] == 2
    assert payload["totals"]["findings"] == 2
    assert payload["totals"]["errors"] == 1
    assert payload["totals"]["warnings"] == 1
    assert payload["totals"]["h4"]["long_tasks"] == 2
    assert payload["totals"]["h5"]["ledgers_present"] == 1
