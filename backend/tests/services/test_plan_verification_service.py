from __future__ import annotations


def test_plan_verification_blocks_plans_without_success_criteria():
    from app.services.plan_verification_service import verify_plan_artifact

    result = verify_plan_artifact(plan_json={"steps": ["ship"]}, evidence_refs=["test://passed"])

    assert result["status"] == "blocked"
    assert result["passed"] is False
    assert result["reason"] == "plan_missing_success_criteria"


def test_plan_verification_passes_when_all_criteria_have_evidence():
    from app.services.plan_verification_service import verify_plan_artifact

    result = verify_plan_artifact(
        plan_json={"success_criteria": ["API exists", "tests pass"]},
        evidence_refs=["pytest://backend/tests/api/test_plan_verification_api.py"],
        completed_criteria=["API exists", "tests pass"],
    )

    assert result["status"] == "passed"
    assert result["passed"] is True
    assert result["missing_criteria"] == []


def test_plan_verification_blocks_missing_criteria_and_fails_explicit_failures():
    from app.services.plan_verification_service import verify_plan_artifact

    blocked = verify_plan_artifact(
        plan_json={"success_criteria": ["API exists", "frontend wired"]},
        evidence_refs=["pytest://backend"],
        completed_criteria=["API exists"],
    )
    failed = verify_plan_artifact(
        plan_json={"success_criteria": ["API exists"]},
        evidence_refs=["pytest://backend"],
        completed_criteria=["API exists"],
        failed_criteria=["frontend wired"],
    )

    assert blocked["status"] == "blocked"
    assert blocked["missing_criteria"] == ["frontend wired"]
    assert failed["status"] == "failed"
    assert failed["reason"] == "failed_criteria_present"
