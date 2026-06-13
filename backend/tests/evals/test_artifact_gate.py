"""E6: Voyager-style pre-promotion artifact execution gate (red first).

A candidate's code must EXECUTE successfully in the isolated microVM before
promotion. The pass/fail judgment lives OUTSIDE the microVM — we trust the
returned exit_code, never the candidate's own stdout claim (DGM/Voyager).
"""

from __future__ import annotations

from pathlib import Path

from app.evals.artifact_gate import artifact_gate_passed, run_artifact_execution_gate
from app.services.code_execution.contracts import CodeExecutionResult


def _ok_execute(stdout: str = "VERIFIED-OK"):
    async def _execute(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        return CodeExecutionResult(stdout=stdout, exit_code=0)

    return _execute


async def test_good_artifact_passes() -> None:
    result = await run_artifact_execution_gate(
        candidate_files={"skill_check.py": "print('VERIFIED-OK')"},
        verification_command=["python3", "skill_check.py"],
        expected_stdout="VERIFIED-OK",
        execute=_ok_execute(),
    )
    assert result["passed"] is True
    assert artifact_gate_passed(result) is True


async def test_nonzero_exit_fails() -> None:
    async def _execute(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        return CodeExecutionResult(stdout="", stderr="boom", exit_code=1)

    result = await run_artifact_execution_gate(
        candidate_files={"s.py": "raise SystemExit(1)"},
        verification_command=["python3", "s.py"],
        execute=_execute,
    )
    assert result["passed"] is False


async def test_timeout_fails() -> None:
    async def _execute(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        return CodeExecutionResult(stdout="", exit_code=0, timed_out=True)

    result = await run_artifact_execution_gate(
        candidate_files={"s.py": "while True: pass"},
        verification_command=["python3", "s.py"],
        execute=_execute,
    )
    assert result["passed"] is False


async def test_execution_error_fails() -> None:
    async def _execute(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        return CodeExecutionResult(error="sandbox unavailable", exit_code=0)

    result = await run_artifact_execution_gate(
        candidate_files={"s.py": "print('x')"},
        verification_command=["python3", "s.py"],
        execute=_execute,
    )
    assert result["passed"] is False


async def test_missing_expected_stdout_fails() -> None:
    result = await run_artifact_execution_gate(
        candidate_files={"s.py": "print('nope')"},
        verification_command=["python3", "s.py"],
        expected_stdout="VERIFIED-OK",
        execute=_ok_execute(stdout="nope"),
    )
    assert result["passed"] is False
    assert result["expected_satisfied"] is False


async def test_candidate_claim_does_not_override_exit_code() -> None:
    # The reward-hack / Voyager point: a candidate that PRINTS success but exits
    # non-zero is caught — we trust the microVM exit_code, not the stdout claim.
    async def _lying_execute(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        return CodeExecutionResult(stdout="all tests passed! 100%", exit_code=1)

    result = await run_artifact_execution_gate(
        candidate_files={"s.py": "..."},
        verification_command=["python3", "s.py"],
        execute=_lying_execute,
    )
    assert result["passed"] is False


async def test_candidate_files_written_to_microvm_workdir() -> None:
    seen: dict = {}

    async def _execute(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        path = Path(work_dir) / "skill_check.py"
        seen["exists"] = path.is_file()
        seen["content"] = path.read_text(encoding="utf-8") if path.is_file() else ""
        seen["network_policy"] = network_policy
        return CodeExecutionResult(stdout="VERIFIED-OK", exit_code=0)

    await run_artifact_execution_gate(
        candidate_files={"skill_check.py": "print('VERIFIED-OK')"},
        verification_command=["python3", "skill_check.py"],
        execute=_execute,
    )
    assert seen["exists"] is True
    assert "VERIFIED-OK" in seen["content"]
    assert seen["network_policy"] == "deny-all"  # isolated by default
