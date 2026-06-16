"""E9: reward-hack adversarial suite — all four attacks must be blocked (red first).

The artifact attacks run the malicious candidate through a REAL subprocess (a
faithful stand-in for the microVM's exit_code semantics) — not a hardcoded fake —
so the gate genuinely catches code that exits non-zero or lies in stdout.
"""

from __future__ import annotations

import subprocess
import json

from app.evals.adversarial_suite import (
    attack_deleted_detection_marker,
    attack_fake_pass_claim,
    attack_modified_baseline,
    attack_modified_grader,
    main,
    run_adversarial_suite,
)
from app.services.code_execution.contracts import CodeExecutionResult


async def _local_execute(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
    # Really runs the candidate (benign: just exits / prints) so the gate sees a
    # true exit_code — faithful to what the microVM would return.
    proc = subprocess.run(  # noqa: S603
        command, cwd=str(work_dir), capture_output=True, text=True, timeout=timeout
    )
    return CodeExecutionResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)


async def test_deleted_detection_marker_blocked() -> None:
    assert await attack_deleted_detection_marker(execute=_local_execute) is True


async def test_fake_pass_claim_blocked() -> None:
    assert await attack_fake_pass_claim(execute=_local_execute) is True


def test_modified_grader_blocked() -> None:
    assert attack_modified_grader() is True


def test_modified_baseline_blocked() -> None:
    assert attack_modified_baseline() is True


async def test_full_suite_all_blocked() -> None:
    report = await run_adversarial_suite(execute=_local_execute)
    assert report["all_blocked"] is True
    assert all(report["attacks"].values())
    assert set(report["attacks"]) == {
        "deleted_detection_marker",
        "fake_pass_claim",
        "modified_grader",
        "modified_baseline",
    }


def test_cli_writes_adversarial_suite_report(tmp_path, monkeypatch) -> None:
    async def fake_suite():
        return {"all_blocked": True, "attacks": {"fake_pass_claim": True}}

    monkeypatch.setattr("app.evals.adversarial_suite._run_default_suite_for_cli", fake_suite)
    output = tmp_path / "adversarial.json"

    assert main(["--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["all_blocked"] is True
