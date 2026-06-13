"""Local OS sandbox provider for development and trusted Linux hosts."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.services.code_execution.contracts import CodeExecutionResult
from app.services.subprocess_sandbox import build_sandboxed_agent_command

logger = logging.getLogger(__name__)


async def execute_local_sandboxed_command(
    command: list[str],
    *,
    work_dir: Path,
    env: dict[str, str],
    timeout: int,
) -> CodeExecutionResult:
    sandbox = build_sandboxed_agent_command(command, work_dir=work_dir, env=env)
    if sandbox.error:
        return CodeExecutionResult(error=sandbox.error)

    try:
        proc = await asyncio.create_subprocess_exec(
            *(sandbox.command or []),
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return CodeExecutionResult(error=f"❌ Command timed out after {timeout}s", timed_out=True)
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
    )
