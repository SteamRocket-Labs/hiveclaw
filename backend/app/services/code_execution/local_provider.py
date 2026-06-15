"""Local OS sandbox provider for development and trusted Linux hosts."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.services.code_execution.contracts import CodeExecutionResult
from app.services.code_execution.env_policy import sanitize_agent_execution_env
from app.services.subprocess_sandbox import build_sandboxed_agent_command

logger = logging.getLogger(__name__)


def _local_isolation(command: list[str], sandboxed_command: list[str] | None) -> str:
    if not sandboxed_command:
        return "unavailable"
    if sandboxed_command == command:
        return "unsandboxed_dev_bypass"
    executable = Path(sandboxed_command[0]).name
    if executable == "bwrap":
        return "bubblewrap"
    if executable == "sandbox-exec":
        return "sandbox-exec"
    return executable or "local_os_sandbox"


def _local_evidence(*, isolation: str, env_policy: dict) -> dict:
    return {
        "provider": "local_os_sandbox",
        "isolation": isolation,
        "network_policy": "host_network" if isolation == "unsandboxed_dev_bypass" else "deny-all",
        "credential_egress": "blocked_by_env_allowlist",
        "env_policy": env_policy,
    }


async def execute_local_sandboxed_command(
    command: list[str],
    *,
    work_dir: Path,
    env: dict[str, str],
    timeout: int,
) -> CodeExecutionResult:
    safe_env, env_policy = sanitize_agent_execution_env(env, require_home=True)
    if env_policy["missing_required_keys"]:
        return CodeExecutionResult(
            error="❌ Code execution environment invalid: HOME is required.",
            evidence=_local_evidence(isolation="unavailable", env_policy=env_policy),
        )

    sandbox = build_sandboxed_agent_command(command, work_dir=work_dir, env=safe_env)
    isolation = _local_isolation(command, sandbox.command)
    evidence = _local_evidence(isolation=isolation, env_policy=env_policy)
    if sandbox.error:
        return CodeExecutionResult(error=sandbox.error, evidence=evidence)

    try:
        proc = await asyncio.create_subprocess_exec(
            *(sandbox.command or []),
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return CodeExecutionResult(
                error=f"❌ Command timed out after {timeout}s",
                timed_out=True,
                evidence=evidence,
            )
    finally:
        for cleanup_path in sandbox.cleanup_paths:
            try:
                cleanup_path.unlink(missing_ok=True)
            except Exception as e:
                logger.debug("Suppressed sandbox cleanup error: %s", e)

    return CodeExecutionResult(
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        exit_code=proc.returncode or 0,
        evidence=evidence,
    )
