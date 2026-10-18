"""Provider selection for agent code execution."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from app.runtime.ccplus_contracts import SandboxProfile
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
    from app.services.execution_environments import current_environment_scope

    provider = configured_code_execution_provider()
    scope = current_environment_scope()
    async def operation():
        return await _execute_selected_command(command, provider=provider, work_dir=work_dir, env=env, timeout=timeout,
            runtime=runtime, network_policy=network_policy, sandbox_spec=sandbox_spec)
    if scope is not None:
        return await scope.execute(operation, provider=provider, timeout=timeout)
    return await operation()


async def _execute_selected_command(command, *, provider, work_dir, env, timeout, runtime, network_policy, sandbox_spec):
    policy = network_policy or os.environ.get("HIVE_CODE_EXEC_NETWORK_POLICY")
    if policy is not None:
        policy = policy.strip() or "deny-all"
        policy = {"deny": "deny-all", "allow": "allow-all"}.get(policy.lower(), policy)
    if provider in {"local", "local_os_sandbox", "os_sandbox", "bwrap", "sandbox-exec"}:
        if policy is not None and policy not in {"deny-all", "allow-all"}:
            return CodeExecutionResult(error="❌ The local sandbox cannot enforce the requested network policy. Use a provider that supports it.",
                evidence={"provider": "local_os_sandbox", "status": "unsupported", "reason_code": "network_policy_unsupported",
                    "retryable": False})
        spec = sandbox_spec
        if policy is not None:
            spec = replace(spec, network_access=spec.network_access and policy == "allow-all") if spec is not None else (
                SandboxBuildSpec(profile=SandboxProfile.FULL_ACCESS_LOCAL_ONLY, network_access=policy == "allow-all"))
        return await execute_local_sandboxed_command(
            command, work_dir=work_dir, env=env, timeout=timeout, spec=spec,
            **({"require_network_denial": True} if policy is not None and not spec.network_access else {}),
        )
    if provider in {"vercel", "vercel_sandbox"}:
        if sandbox_spec is not None and not sandbox_spec.network_access:
            policy = "deny-all"
        return await execute_vercel_sandbox_command(
            command,
            work_dir=work_dir,
            env=env,
            timeout=timeout,
            runtime=runtime,
            network_policy=policy,
        )
    return CodeExecutionResult(
        error=(
            "❌ Code execution provider unavailable "
            f"(provider={provider}). Configure HIVE_CODE_EXEC_PROVIDER=local_os_sandbox or vercel_sandbox."
        )
    )
