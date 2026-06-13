"""OS sandbox command construction for agent-controlled subprocesses."""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AgentSandboxCommand:
    command: list[str] | None
    cleanup_paths: list[Path] = field(default_factory=list)
    error: str | None = None


def _allow_unsandboxed_code_exec() -> bool:
    return os.environ.get("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "").strip().lower() in {"1", "true", "yes", "on"}


def _sandbox_unavailable_message(mode: str) -> str:
    return (
        "❌ Execution sandbox unavailable "
        f"(mode={mode}). Configure Linux bubblewrap (`bwrap`) or macOS `sandbox-exec`, "
        "or set HIVE_ALLOW_UNSANDBOXED_CODE_EXEC=1 for explicit local-development bypass."
    )


def _escape_sandbox_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _darwin_sandbox_command(command: list[str], *, work_dir: Path, home: Path) -> tuple[list[str], list[Path]]:
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return [], []

    fd, profile_path_str = tempfile.mkstemp(prefix="hive-sandbox-", suffix=".sb")
    profile_path = Path(profile_path_str)
    os.close(fd)
    work = _escape_sandbox_string(str(work_dir))
    home_s = _escape_sandbox_string(str(home))
    profile = f"""(version 1)
(allow default)
(deny network*)
(deny file-read* (subpath "/Users"))
(deny file-write* (subpath "/Users"))
(allow file-read* (subpath "{work}") (subpath "{home_s}"))
(allow file-write* (subpath "{work}") (subpath "{home_s}"))
"""
    profile_path.write_text(profile, encoding="utf-8")
    return [sandbox_exec, "-f", str(profile_path), *command], [profile_path]


def _linux_bwrap_command(command: list[str], *, work_dir: Path, home: Path) -> tuple[list[str], list[Path]]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return [], []

    args = [
        bwrap,
        "--die-with-parent",
        "--unshare-all",
        "--new-session",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(work_dir),
        str(work_dir),
        "--bind",
        str(home),
        str(home),
        "--setenv",
        "HOME",
        str(home),
        "--chdir",
        str(work_dir),
    ]
    for path in ("/bin", "/usr", "/lib", "/lib64", "/opt/homebrew"):
        if Path(path).exists():
            args.extend(["--ro-bind", path, path])
    return [*args, *command], []


def build_sandboxed_agent_command(command: list[str], *, work_dir: Path, env: dict[str, str]) -> AgentSandboxCommand:
    mode = os.environ.get("HIVE_CODE_SANDBOX_MODE", "auto").strip().lower() or "auto"
    if mode in {"none", "off", "disabled"}:
        if _allow_unsandboxed_code_exec():
            return AgentSandboxCommand(command=command)
        return AgentSandboxCommand(command=None, error=_sandbox_unavailable_message(mode))

    home = Path(env["HOME"]).resolve()
    if mode in {"auto", "darwin", "sandbox-exec"} and platform.system() == "Darwin":
        sandboxed, cleanup = _darwin_sandbox_command(command, work_dir=work_dir, home=home)
        if sandboxed:
            return AgentSandboxCommand(command=sandboxed, cleanup_paths=cleanup)
        if mode != "auto":
            return AgentSandboxCommand(command=None, error=_sandbox_unavailable_message(mode))

    if mode in {"auto", "bwrap", "bubblewrap", "linux"}:
        sandboxed, cleanup = _linux_bwrap_command(command, work_dir=work_dir, home=home)
        if sandboxed:
            return AgentSandboxCommand(command=sandboxed, cleanup_paths=cleanup)
        if mode != "auto":
            return AgentSandboxCommand(command=None, error=_sandbox_unavailable_message(mode))

    if _allow_unsandboxed_code_exec():
        return AgentSandboxCommand(command=command)
    return AgentSandboxCommand(command=None, error=_sandbox_unavailable_message(mode))
