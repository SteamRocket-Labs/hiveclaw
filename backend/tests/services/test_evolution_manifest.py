from __future__ import annotations


def test_evolution_manifest_requires_thin_contract_fields() -> None:
    from app.services.evolution_manifest import validate_evolution_manifest

    errors = validate_evolution_manifest(
        {
            "schema": "hive_evolution_manifest.v1",
            "change_type": "skill",
            "target_type": "skill",
            "target_id": "deploy-checklist",
            "source_refs": ["runtime_task:rt-1"],
            "trace_refs": ["trace:rt-1"],
            "eval_refs": [],
            "rollback_plan": {"strategy": "restore previous skill revision"},
            "risk_level": "medium",
        }
    )

    assert errors == []

    missing_errors = validate_evolution_manifest(
        {
            "schema": "hive_evolution_manifest.v1",
            "change_type": "skill",
            "target_type": "skill",
            "target_id": "deploy-checklist",
        }
    )

    assert "source_refs must contain at least one reference" in missing_errors
    assert "trace_refs must contain at least one reference" in missing_errors
    assert "eval_refs must be present as a list" in missing_errors
    assert "rollback_plan.strategy is required" in missing_errors


def test_record_evolution_candidate_attaches_manifest(tmp_path) -> None:
    from app.services.evolution_ledger import record_evolution_candidate
    from app.services.evolution_manifest import validate_evolution_manifest

    candidate = record_evolution_candidate(
        tmp_path,
        target_type="skill",
        target_id="deploy-checklist",
        diff="+ safer deploy checklist",
        source_attempt_ids=["rt-1"],
        baseline_version="skill@v1",
    )

    manifest = candidate["manifest"]
    assert manifest["schema"] == "hive_evolution_manifest.v1"
    assert manifest["target_type"] == "skill"
    assert manifest["target_id"] == "deploy-checklist"
    assert manifest["source_refs"] == ["runtime_task:rt-1"]
    assert manifest["trace_refs"] == ["trace:rt-1"]
    assert validate_evolution_manifest(manifest) == []


def test_validate_evolution_ledger_rejects_candidate_without_manifest(tmp_path) -> None:
    import json

    from app.services.evolution_validation import validate_evolution_ledger

    ledger_path = tmp_path / "evolution" / "evolution_ledger.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "schema": "evolution_candidate.v1",
                "event": "candidate",
                "candidate_id": "cand-missing-manifest",
                "target_type": "skill",
                "target_id": "unsafe-skill",
                "source_attempt_ids": ["rt-1"],
                "diff_hash": "abc",
                "diff_preview": "+ unsafe",
                "metadata": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = validate_evolution_ledger(tmp_path, write_report=False)

    failed_ids = {check["id"] for check in report["checks"] if check["status"] == "fail"}
    assert "candidate_has_valid_manifest" in failed_ids
