"""E5: evaluator trust root — graders/baselines come from a PROTECTED source the
candidate-under-test cannot modify (red first).

Encodes the owner's finding (round2 §4.2): a git-tracked expected hash in the
SAME PR as the grader is self-deception — compare against the protected base, not
against a PR-embedded hash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evals.evaluator_integrity import (
    EVALUATOR_RELATIVE_PATHS,
    EvaluatorIntegrityError,
    assert_evaluator_outside_agent_workspace,
    compute_evaluator_hashes,
    main,
    verify_evaluator_integrity,
)


def test_unchanged_evaluator_is_trusted() -> None:
    trusted = {"app/services/evolution_verification.py": "H1", "app/evals/baseline.py": "H2"}
    report = verify_evaluator_integrity(current_hashes=dict(trusted), trusted_hashes=trusted)
    assert report.trusted is True
    assert report.changed_paths == []


def test_changed_grader_is_untrusted() -> None:
    trusted = {"app/services/evolution_verification.py": "BASE"}
    current = {"app/services/evolution_verification.py": "ATTACKER"}
    report = verify_evaluator_integrity(current_hashes=current, trusted_hashes=trusted)
    assert report.trusted is False
    assert "app/services/evolution_verification.py" in report.changed_paths


def test_in_pr_expected_hash_cannot_authorize_grader_change() -> None:
    # THE owner finding: even if the PR ships a self-consistent expected hash of
    # the NEW grader, integrity is computed against the TRUSTED base, so the change
    # is still detected. There is no "compare current to itself" path.
    trusted = {"app/services/evolution_verification.py": "BASE_HASH"}
    current = {"app/services/evolution_verification.py": "NEW_HASH_MATCHING_PR_MANIFEST"}
    report = verify_evaluator_integrity(current_hashes=current, trusted_hashes=trusted)
    assert report.trusted is False


def test_change_with_protected_authorization_is_trusted() -> None:
    trusted = {"app/services/evolution_verification.py": "BASE"}
    current = {"app/services/evolution_verification.py": "REVIEWED"}
    # authorized_changes is set by CI only when CODEOWNERS approved from the
    # protected context — never self-approved inside the candidate PR.
    report = verify_evaluator_integrity(current_hashes=current, trusted_hashes=trusted, authorized_changes=True)
    assert report.trusted is True


def test_changed_baseline_is_untrusted() -> None:
    trusted = {"app/evals/baselines/core_behavior_v1.json": "B0"}
    current = {"app/evals/baselines/core_behavior_v1.json": "B1"}
    report = verify_evaluator_integrity(current_hashes=current, trusted_hashes=trusted)
    assert report.trusted is False
    assert "app/evals/baselines/core_behavior_v1.json" in report.changed_paths


def test_missing_evaluator_file_is_a_change() -> None:
    trusted = {"app/evals/baseline.py": "H"}
    current: dict[str, str] = {}
    report = verify_evaluator_integrity(current_hashes=current, trusted_hashes=trusted)
    assert report.trusted is False


def test_compute_evaluator_hashes_stable_and_sensitive(tmp_path: Path) -> None:
    (tmp_path / "app" / "evals").mkdir(parents=True)
    target = tmp_path / "app" / "evals" / "baseline.py"
    target.write_text("x = 1\n", encoding="utf-8")
    h1 = compute_evaluator_hashes(tmp_path, relative_paths=("app/evals/baseline.py",))
    h2 = compute_evaluator_hashes(tmp_path, relative_paths=("app/evals/baseline.py",))
    assert h1 == h2  # stable
    target.write_text("x = 2\n", encoding="utf-8")
    h3 = compute_evaluator_hashes(tmp_path, relative_paths=("app/evals/baseline.py",))
    assert h3 != h1  # sensitive to content


def test_evaluator_paths_are_outside_agent_workspace() -> None:
    # Active guard: every real evaluator path must be non-agent-writable.
    for rel in EVALUATOR_RELATIVE_PATHS:
        assert_evaluator_outside_agent_workspace(rel)  # must not raise


def test_integrity_root_covers_behavior_gate_files() -> None:
    required = {
        "app/evals/adversarial_suite.py",
        "app/evals/artifact_gate.py",
        "app/evals/ci_gate.py",
        "app/evals/cost_budget.py",
        "app/evals/hermes_baseline.py",
        "app/evals/hive_live_runner.py",
        "app/evals/run.py",
    }
    assert required <= set(EVALUATOR_RELATIVE_PATHS)


def test_agent_workspace_path_is_rejected() -> None:
    with pytest.raises(EvaluatorIntegrityError):
        assert_evaluator_outside_agent_workspace("/data/agents/abc/workspace/grader.py")


def test_cli_writes_untrusted_integrity_report_for_changed_evaluator(tmp_path: Path) -> None:
    current = tmp_path / "current"
    trusted = tmp_path / "trusted"
    for root, content in ((current, "x = 2\n"), (trusted, "x = 1\n")):
        path = root / "app" / "evals" / "baseline.py"
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
    output = tmp_path / "integrity.json"

    exit_code = main(
        [
            "--current-root",
            str(current),
            "--trusted-root",
            str(trusted),
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["trusted"] is False
    assert "app/evals/baseline.py" in payload["changed_paths"]
