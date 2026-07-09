"""Provider selection for agent code execution."""

from __future__ import annotations

import os
from pathlib import Path

from app.services.code_execution.contracts import CodeExecutionResult
from app.services.code_execution.local_provider import execute_local_sandboxed_command
from app.services.code_execution.vercel_provider import execute_vercel_sandbox_command
from app.services.subprocess_sandbox import SandboxBuildSpec


def configured_code_execution_provider() -> str:
    return os.environ.get("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox").strip().lower() or "local_os_sandbox"


async def execute_agent_command(
    command: list[str],
    *,
    work_dir: Path,
    env: dict[str, str],
    timeout: int,
    runtime: str | None = None,
    network_policy: str | None = None,
    sandbox_spec: SandboxBuildSpec | None = None,
) -> CodeExecutionResult:
    provider = configured_code_execution_provider()
    if provider in {"local", "local_os_sandbox", "os_sandbox", "bwrap", "sandbox-exec"}:
        return await execute_local_sandboxed_command(
            command, work_dir=work_dir, env=env, timeout=timeout, spec=sandbox_spec
        )
    if provider in {"vercel", "vercel_sandbox"}:
        return await execute_vercel_sandbox_command(
            command,
            work_dir=work_dir,
            env=env,
            timeout=timeout,
            runtime=runtime,
            network_policy=network_policy,
        )
    return CodeExecutionResult(
        error=(
            "❌ Code execution provider unavailable "
            f"(provider={provider}). Configure HIVE_CODE_EXEC_PROVIDER=local_os_sandbox or vercel_sandbox."
        )
    )
