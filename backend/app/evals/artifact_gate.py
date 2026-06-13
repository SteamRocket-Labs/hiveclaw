"""E6: Voyager-style pre-promotion artifact execution gate.

A self-evolution candidate (a skill / code artifact) must EXECUTE successfully in
an isolated microVM before it can be promoted. "Execution success in the sandbox"
is the hard, agent-improvement-proof signal that separates a real capability from
remembered text (Voyager: a skill enters the library only after it runs without
error and satisfies its declared assertion).

The candidate's code runs INSIDE the microVM (services/code_execution), but the
PASS/FAIL judgment lives OUTSIDE it: we consume only the returned
CodeExecutionResult (exit_code / timed_out / error / stdout) and trust the
exit_code, never the candidate's own stdout claim. A candidate that prints
"all tests passed" while exiting non-zero is caught (DGM Node 114 resistance).
The microVM defaults to deny-all networking so the artifact cannot phone home.
"""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.services.code_execution.contracts import CodeExecutionResult

ExecuteCommand = Callable[..., Awaitable[CodeExecutionResult]]


def _artifact_gate_failure(reason: str) -> dict[str, Any]:
    return {
        "passed": False,
        "exit_code": None,
        "timed_out": False,
        "error": reason,
        "expected_satisfied": False,
        "stdout_tail": "",
        "reason": reason,
    }


def _failure_reason(result: CodeExecutionResult, expected_satisfied: bool) -> str:
    if result.error:
        return f"execution error: {result.error}"
    if result.timed_out:
        return "artifact execution timed out"
    if result.exit_code != 0:
        return f"artifact exited non-zero (exit_code={result.exit_code})"
    if not expected_satisfied:
        return "artifact ran but did not satisfy the declared stdout assertion"
    return "artifact execution failed"


def _candidate_target(work_dir: Path, candidate_path: str) -> Path:
    path = Path(candidate_path)
    if path.is_absolute():
        raise ValueError(f"unsafe candidate path: {candidate_path!r} is absolute")
    resolved_work_dir = work_dir.resolve()
    target = (resolved_work_dir / path).resolve()
    try:
        target.relative_to(resolved_work_dir)
    except ValueError as exc:
        raise ValueError(f"unsafe candidate path: {candidate_path!r} escapes artifact workdir") from exc
    if target == resolved_work_dir:
        raise ValueError(f"unsafe candidate path: {candidate_path!r} does not name a file")
    return target


def _sandbox_env(work_dir: Path) -> dict[str, str]:
    tmp_dir = work_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(work_dir),
        "TMPDIR": str(tmp_dir),
        "TMP": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }


async def run_artifact_execution_gate(
    *,
    candidate_files: dict[str, str],
    verification_command: list[str],
    expected_stdout: str | None = None,
    timeout: int = 60,
    network_policy: str = "deny-all",
    runtime: str | None = None,
    execute: ExecuteCommand | None = None,
) -> dict[str, Any]:
    """Write the candidate into an isolated work dir, run its verification command
    in the microVM, and judge OUTSIDE the microVM from the returned result.

    ``execute`` is injected in tests; it defaults to the production
    ``execute_agent_command`` (microVM provider per HIVE_CODE_EXEC_PROVIDER).
    """

    run_command = execute
    if run_command is None:
        from app.services.code_execution.service import execute_agent_command as run_command

    with tempfile.TemporaryDirectory(prefix="hive-artifact-gate-") as tmp:
        work_dir = Path(tmp)
        try:
            targets = {relative_path: _candidate_target(work_dir, relative_path) for relative_path in candidate_files}
        except ValueError as exc:
            return _artifact_gate_failure(str(exc))
        for relative_path, content in candidate_files.items():
            target = targets[relative_path]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        result = await run_command(
            verification_command,
            work_dir=work_dir,
            env=_sandbox_env(work_dir),
            timeout=timeout,
            runtime=runtime,
            network_policy=network_policy,
        )

    stdout = result.stdout or ""
    expected_satisfied = expected_stdout is None or expected_stdout in stdout
    passed = result.exit_code == 0 and not result.timed_out and not result.error and expected_satisfied
    return {
        "passed": passed,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "error": result.error,
        "expected_satisfied": expected_satisfied,
        "stdout_tail": stdout[-2000:],
        "reason": "artifact executed and satisfied its declared assertion"
        if passed
        else _failure_reason(result, expected_satisfied),
    }


def artifact_gate_passed(gate_result: dict[str, Any]) -> bool:
    """True only when the artifact executed cleanly and satisfied its assertion."""

    return bool(gate_result.get("passed"))
