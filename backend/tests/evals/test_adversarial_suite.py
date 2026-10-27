"""Reward-hack adversarial suite — all retained attacks must be blocked.

The artifact attacks run the malicious candidate through a REAL subprocess (a
faithful stand-in for the microVM's exit_code semantics) — not a hardcoded fake —
so the gate genuinely catches code that exits non-zero or lies in stdout.
"""

from __future__ import annotations

import subprocess
import json

import pytest

from app.evals import adversarial_suite
from app.evals.adversarial_suite import (
    attack_deleted_detection_marker,
    attack_fake_pass_claim,
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


async def test_full_suite_all_blocked() -> None:
    report = await run_adversarial_suite(execute=_local_execute)
    assert report["all_blocked"] is True
    assert all(report["attacks"].values())
    assert set(report["attacks"]) == {
        "deleted_detection_marker",
        "fake_pass_claim",
        "modified_grader",
    }


def test_cli_writes_adversarial_suite_report(tmp_path, monkeypatch) -> None:
    async def fake_suite():
        return {"all_blocked": True, "attacks": {"fake_pass_claim": True}}

    monkeypatch.setattr("app.evals.adversarial_suite._run_default_suite_for_cli", fake_suite)
    output = tmp_path / "adversarial.json"

    assert main(["--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["all_blocked"] is True


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(
            CodeExecutionResult(
                error="Synthetic executor unavailable.",
                evidence={"status": "unavailable", "reason_code": "sandbox_unavailable"},
            ),
            id="unavailable",
        ),
        pytest.param(CodeExecutionResult(timed_out=True), id="timeout"),
        pytest.param(
            CodeExecutionResult(exit_code=127, stderr="Interpreter unavailable."),
            id="command_not_found",
        ),
        pytest.param(
            CodeExecutionResult(exit_code=1, stderr="Interpreter failed before candidate startup."),
            id="candidate_not_started",
        ),
    ],
)
def test_cli_does_not_claim_unexecuted_attacks_were_blocked(tmp_path, monkeypatch, result):
    calls = []

    async def unavailable(command, **kwargs):
        calls.append((command, kwargs["network_policy"]))
        return result

    async def run_suite():
        return await adversarial_suite.run_adversarial_suite(execute=unavailable)

    monkeypatch.setattr(adversarial_suite, "_run_default_suite_for_cli", run_suite)
    output = tmp_path / "adversarial.json"
    code = adversarial_suite.main(["--output", str(output)])
    report = json.loads(output.read_text())

    assert len(calls) == 2 and all(policy == "deny-all" for _command, policy in calls)
    assert code == 1, report
    assert report["all_blocked"] is False
    assert report["attacks"]["deleted_detection_marker"] is False
    assert report["attacks"]["fake_pass_claim"] is False
    assert report["attacks"]["modified_grader"] is True
