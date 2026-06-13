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
    return {key: value for key, value in env.items() if key not in {"HOME", "PATH", "TMPDIR"} and value is not None}


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
            if not str(target).startswith(str(root) + os.sep) and target != root:
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
    missing = _missing_vercel_config()
    if missing:
        return CodeExecutionResult(
            error=(
                "❌ Vercel Sandbox unavailable: missing "
                + ", ".join(missing)
                + ". Configure Railway backend env before enabling HIVE_CODE_EXEC_PROVIDER=vercel_sandbox."
            )
        )

    policy = (network_policy or os.environ.get("HIVE_CODE_EXEC_NETWORK_POLICY", "deny-all")).strip() or "deny-all"
    # Vercel SDK create()/extend_timeout() take MILLISECONDS; `timeout` here is seconds.
    sandbox_timeout_ms = max(timeout + 30, 60) * 1000
    local_home = env.get("HOME")
    sandbox = None
    try:
        sandbox = await AsyncSandbox.create(
            team_id=os.environ["VERCEL_TEAM_ID"],
            project_id=os.environ["VERCEL_PROJECT_ID"],
            token=os.environ["VERCEL_TOKEN"],
            runtime=runtime or os.environ.get("HIVE_VERCEL_SANDBOX_RUNTIME", "python3.13"),
            timeout=sandbox_timeout_ms,
            network_policy=policy,
        )
        exec_env = _vercel_env(env)
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
        return CodeExecutionResult(stdout=stdout, stderr=stderr, exit_code=finished.exit_code)
    except asyncio.TimeoutError:
        return CodeExecutionResult(error=f"❌ Command timed out after {timeout}s", timed_out=True)
    except Exception as e:
        return CodeExecutionResult(error=f"❌ Vercel Sandbox execution error: {str(e)[:300]}")
    finally:
        if sandbox is not None:
            try:
                await sandbox.stop()
            except Exception as exc:
                logger.debug("Suppressed vercel sandbox stop error: %s", exc)
