"""Local OS sandbox provider for development and trusted Linux hosts."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.runtime.ccplus_contracts import SandboxProfile
from app.services.code_execution.contracts import CodeExecutionResult
from app.services.code_execution.env_policy import sanitize_agent_execution_env
from app.services.subprocess_sandbox import SandboxBuildSpec, build_sandboxed_agent_command

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


def _local_network_policy(*, isolation: str, spec: SandboxBuildSpec) -> str:
    if isolation == "unsandboxed_dev_bypass":
        return "host_network"
    return "allow" if spec.network_access else "deny-all"


def _local_evidence(*, isolation: str, env_policy: dict, spec: SandboxBuildSpec) -> dict:
    return {
        "provider": "local_os_sandbox",
        "isolation": isolation,
        "sandbox_profile": spec.profile.value,
        "network_policy": _local_network_policy(isolation=isolation, spec=spec),
        "credential_egress": "blocked_by_env_allowlist",
        "env_policy": env_policy,
    }


async def execute_local_sandboxed_command(
    command: list[str],
    *,
    work_dir: Path,
    env: dict[str, str],
    timeout: int,
    spec: SandboxBuildSpec | None = None,
) -> CodeExecutionResult:
    effective_spec = spec or SandboxBuildSpec(profile=SandboxProfile.FULL_ACCESS_LOCAL_ONLY)
    safe_env, env_policy = sanitize_agent_execution_env(env, require_home=True)
    if env_policy["missing_required_keys"]:
        return CodeExecutionResult(
            error="❌ Code execution environment invalid: HOME is required.",
            evidence=_local_evidence(isolation="unavailable", env_policy=env_policy, spec=effective_spec),
        )

    sandbox = build_sandboxed_agent_command(command, work_dir=work_dir, env=safe_env, spec=effective_spec)
    isolation = _local_isolation(command, sandbox.command)
    evidence = _local_evidence(isolation=isolation, env_policy=env_policy, spec=effective_spec)
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
