"""Vercel Sandbox provider for Railway-hosted agent code execution."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tarfile
from pathlib import Path

from vercel.sandbox import AsyncSandbox, WriteFile

from app.services.code_execution.contracts import CodeExecutionResult
from app.services.code_execution.env_policy import sanitize_agent_execution_env

logger = logging.getLogger(__name__)

_REMOTE_ROOT = "/vercel/sandbox"
_REMOTE_WORKSPACE = f"{_REMOTE_ROOT}/workspace"
# Controlled remote HOME (NOT the backend's HOME). npx/npm/`npx skills add` write
# under $HOME; keeping it inside the microVM lets us sync the subtree back.
_REMOTE_HOME = f"{_REMOTE_ROOT}/agent-home"
_INPUT_ARCHIVE = "workspace.tar.gz"
_OUTPUT_ARCHIVE = "workspace-out.tar.gz"
_HOME_ARCHIVE = "agent-home-out.tar.gz"


def _missing_vercel_config() -> list[str]:
    return [key for key in ("VERCEL_TEAM_ID", "VERCEL_PROJECT_ID", "VERCEL_TOKEN") if not os.environ.get(key)]


def _vercel_env(env: dict[str, str]) -> dict[str, str]:
    # Do not force local HOME/PATH/TMPDIR into the microVM. Capability-specific
    # credentials must be brokered explicitly, not inherited from backend env.
    return {
        key: value
        for key, value in env.items()
        if key not in {"HOME", "PATH", "TMPDIR", "TMP", "TEMP"} and value is not None
    }


def _network_allowlist(policy: str) -> list[str]:
    normalized = str(policy or "").strip()
    if ":" not in normalized:
        return []
    prefix, raw_hosts = normalized.split(":", 1)
    if prefix not in {"allowlist", "allow"}:
        return []
    hosts = [host.strip().lower() for host in raw_hosts.split(",") if host.strip()]
    return sorted(dict.fromkeys(hosts))


def _brokered_credentials(env: dict[str, str]) -> tuple[dict[str, str], dict]:
    configured = [
        key.strip()
        for key in os.environ.get("HIVE_CODE_EXEC_BROKERED_CREDENTIAL_KEYS", "").split(",")
        if key.strip()
    ]
    handles: dict[str, str] = {}
    for key in configured:
        if env.get(key):
            handles[key] = f"cred://agent/{key.lower()}"
    evidence = {
        "mode": "explicit_handles",
        "values_included": False,
        "configured_keys": sorted(configured),
        "brokered_keys": sorted(handles),
    }
    return handles, evidence


def _vercel_evidence(
    *,
    network_policy: str,
    env_policy: dict,
    credential_broker: dict | None = None,
    remote_env_keys: list[str] | None = None,
    configured: bool = True,
) -> dict:
    return {
        "provider": "vercel_sandbox",
        "configured": configured,
        "isolation": "vercel_microvm",
        "network_policy": network_policy,
        "network_allowlist": _network_allowlist(network_policy),
        "credential_egress": "blocked_by_env_allowlist",
        "credential_broker": credential_broker or {
            "mode": "explicit_handles",
            "values_included": False,
            "configured_keys": [],
            "brokered_keys": [],
        },
        "provider_credentials_exposed_to_sandbox": False,
        "workspace_materialization": "tar_upload_sync_back",
        "remote_env_keys": sorted(remote_env_keys or []),
        "env_policy": env_policy,
    }


def _create_workspace_archive(work_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(work_dir.rglob("*")):
            if path.is_dir():
                continue
            if path.is_symlink():
                continue
            archive.add(path, arcname=path.relative_to(work_dir).as_posix(), recursive=False)
    return buffer.getvalue()


def _safe_extract_workspace_archive(archive_bytes: bytes, work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    root = work_dir.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            target = (root / member.name).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if member.issym() or member.islnk() or member.isdev():
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            target.write_bytes(source.read())


async def execute_vercel_sandbox_command(
    command: list[str],
    *,
    work_dir: Path,
    env: dict[str, str],
    timeout: int,
    runtime: str | None = None,
    network_policy: str | None = None,
) -> CodeExecutionResult:
    policy = (network_policy or os.environ.get("HIVE_CODE_EXEC_NETWORK_POLICY", "deny-all")).strip() or "deny-all"
    safe_env, env_policy = sanitize_agent_execution_env(env, require_home=False)
    brokered_handles, broker_evidence = _brokered_credentials(env)
    missing = _missing_vercel_config()
    if missing:
        return CodeExecutionResult(
            error=(
                "❌ Vercel Sandbox unavailable: missing "
                + ", ".join(missing)
                + ". Configure Railway backend env before enabling HIVE_CODE_EXEC_PROVIDER=vercel_sandbox."
            ),
            evidence=_vercel_evidence(
                network_policy=policy,
                env_policy=env_policy,
                credential_broker=broker_evidence,
                configured=False,
            ),
        )

    # Vercel SDK create()/extend_timeout() take MILLISECONDS; `timeout` here is seconds.
    sandbox_timeout_ms = max(timeout + 30, 60) * 1000
    local_home = safe_env.get("HOME")
    sandbox = None
    exec_env: dict[str, str] = {}
    try:
        sandbox = await AsyncSandbox.create(
            team_id=os.environ["VERCEL_TEAM_ID"],
            project_id=os.environ["VERCEL_PROJECT_ID"],
            token=os.environ["VERCEL_TOKEN"],
            runtime=runtime or os.environ.get("HIVE_VERCEL_SANDBOX_RUNTIME", "python3.13"),
            timeout=sandbox_timeout_ms,
            network_policy=policy,
        )
        exec_env = _vercel_env(safe_env)
        if brokered_handles:
            exec_env["HIVE_BROKERED_CREDENTIALS"] = ",".join(
                f"{key}={handle}" for key, handle in sorted(brokered_handles.items())
            )
        exec_env["HOME"] = _REMOTE_HOME
        await sandbox.run_command("mkdir", ["-p", _REMOTE_WORKSPACE, _REMOTE_HOME])
        await sandbox.write_files([WriteFile(path=_INPUT_ARCHIVE, content=_create_workspace_archive(work_dir))])
        await sandbox.run_command("tar", ["-xzf", f"{_REMOTE_ROOT}/{_INPUT_ARCHIVE}", "-C", _REMOTE_WORKSPACE])
        finished = await asyncio.wait_for(
            sandbox.run_command(command[0], command[1:], cwd=_REMOTE_WORKSPACE, env=exec_env),
            timeout=timeout,
        )
        stdout = (await finished.stdout())[:12000]
        stderr = (await finished.stderr())[:6000]
        # Sync workspace (agent files + produced outputs) back to the local work_dir.
        await sandbox.run_command("tar", ["-czf", f"{_REMOTE_ROOT}/{_OUTPUT_ARCHIVE}", "-C", _REMOTE_WORKSPACE, "."])
        workspace_bytes = await sandbox.read_file(_OUTPUT_ARCHIVE)
        if workspace_bytes:
            _safe_extract_workspace_archive(workspace_bytes, work_dir)
        # `npx skills add` and friends write under $HOME/.agents; sync that subtree
        # back to the caller's local HOME so post-exec skill harvesting still works.
        if local_home:
            await sandbox.run_command(
                "sh",
                ["-lc", f"tar -czf {_REMOTE_ROOT}/{_HOME_ARCHIVE} -C {_REMOTE_HOME} .agents 2>/dev/null || true"],
            )
            home_bytes = await sandbox.read_file(_HOME_ARCHIVE)
            if home_bytes:
                _safe_extract_workspace_archive(home_bytes, Path(local_home))
        return CodeExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=finished.exit_code,
            evidence=_vercel_evidence(
                network_policy=policy,
                env_policy=env_policy,
                credential_broker=broker_evidence,
                remote_env_keys=sorted(exec_env),
            ),
        )
    except asyncio.TimeoutError:
        return CodeExecutionResult(
            error=f"❌ Command timed out after {timeout}s",
            timed_out=True,
            evidence=_vercel_evidence(
                network_policy=policy,
                env_policy=env_policy,
                credential_broker=broker_evidence,
                remote_env_keys=sorted(exec_env),
            ),
        )
    except Exception as e:
        return CodeExecutionResult(
            error=f"❌ Vercel Sandbox execution error: {str(e)[:300]}",
            evidence=_vercel_evidence(
                network_policy=policy,
                env_policy=env_policy,
                credential_broker=broker_evidence,
                remote_env_keys=sorted(exec_env),
            ),
        )
    finally:
        if sandbox is not None:
            try:
                await sandbox.stop()
            except Exception as exc:
                logger.debug("Suppressed vercel sandbox stop error: %s", exc)
