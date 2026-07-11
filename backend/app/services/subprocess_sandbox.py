"""OS sandbox command construction for agent-controlled subprocesses.

The builder honors a :class:`SandboxBuildSpec` — the runtime projection of
``PermissionProfileV1``'s ``sandbox`` / ``network_access`` / ``writable_roots``
fields (``app.runtime.ccplus_contracts``). Four sandbox profiles map onto real
``sandbox-exec`` / ``bwrap`` parameters:

* ``READ_ONLY`` — no filesystem writes anywhere.
* ``WORKSPACE_WRITE`` — writes confined to the ``writable_roots`` whitelist
  (defaults to the work dir).
* ``FULL_ACCESS_LOCAL_ONLY`` — writes to the work dir plus the home dir (the
  historical permissive local behavior; still no network unless enabled).
* ``EXTERNAL_SANDBOX`` — the external (Vercel) sandbox tier; when it reaches the
  local builder it degrades to ``WORKSPACE_WRITE`` scoping.

``network_access`` is an orthogonal switch: false denies network (the default),
true re-shares it.

Railway production continues to run through ``HIVE_CODE_EXEC_PROVIDER=vercel_sandbox``;
this builder is only the local/trusted-host path and that provider selection is
unchanged.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.runtime.ccplus_contracts import SandboxProfile


@dataclass(frozen=True)
class SandboxBuildSpec:
    """Sandbox scoping request for the local OS sandbox builder.

    Defaults to the CCPlus contract default (``WORKSPACE_WRITE``, no network).
    ``build_sandboxed_agent_command`` treats a missing spec (``None``) as the
    legacy permissive path (``FULL_ACCESS_LOCAL_ONLY``) so callers that have not
    yet threaded a profile keep their historical work+home writable scope.
    """

    profile: SandboxProfile = SandboxProfile.WORKSPACE_WRITE
    network_access: bool = False
    writable_roots: tuple[str, ...] = ()


# Legacy default for callers that pass no spec — preserves the historical
# work+home writable, network-denied behavior so existing execution paths do not
# regress (pip/npm caches under $HOME keep working).
_LEGACY_SANDBOX_SPEC = SandboxBuildSpec(profile=SandboxProfile.FULL_ACCESS_LOCAL_ONLY)


@dataclass(frozen=True)
class AgentSandboxCommand:
    command: list[str] | None
    cleanup_paths: list[Path] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class SandboxCapabilityProbe:
    available: bool
    provider: str | None
    reason: str


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


def _resolve_writable_roots(spec: SandboxBuildSpec, *, work_dir: Path, home: Path) -> tuple[Path, ...]:
    """Resolve the absolute writable roots a profile grants.

    ``READ_ONLY`` grants none; ``FULL_ACCESS_LOCAL_ONLY`` grants work+home;
    ``WORKSPACE_WRITE`` / ``EXTERNAL_SANDBOX`` grant the ``writable_roots``
    whitelist (relative entries resolve against the work dir), defaulting to the
    work dir when the whitelist is empty.
    """
    if spec.profile == SandboxProfile.READ_ONLY:
        return ()
    if spec.profile == SandboxProfile.FULL_ACCESS_LOCAL_ONLY:
        return (work_dir, home)
    if not spec.writable_roots:
        return (work_dir,)
    resolved: list[Path] = []
    for root in spec.writable_roots:
        candidate = Path(root)
        resolved.append(candidate if candidate.is_absolute() else (work_dir / root))
    return tuple(resolved)


def _darwin_sandbox_command(
    command: list[str], *, work_dir: Path, home: Path, spec: SandboxBuildSpec
) -> tuple[list[str], list[Path]]:
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return [], []

    fd, profile_path_str = tempfile.mkstemp(prefix="hive-sandbox-", suffix=".sb")
    profile_path = Path(profile_path_str)
    os.close(fd)
    work = _escape_sandbox_string(str(work_dir))
    home_s = _escape_sandbox_string(str(home))
    writable = _resolve_writable_roots(spec, work_dir=work_dir, home=home)

    lines = ["(version 1)", "(allow default)"]
    lines.append("(allow network*)" if spec.network_access else "(deny network*)")
    if spec.profile == SandboxProfile.FULL_ACCESS_LOCAL_ONLY:
        # Historical permissive scope: writes everywhere except /Users, with
        # work+home explicitly allowed for both read and write.
        lines += [
            '(deny file-read* (subpath "/Users"))',
            '(deny file-write* (subpath "/Users"))',
            f'(allow file-read* (subpath "{work}") (subpath "{home_s}"))',
            f'(allow file-write* (subpath "{work}") (subpath "{home_s}"))',
        ]
    else:
        # read_only / workspace_write / external_sandbox: deny all writes by
        # default (last matching rule wins), then re-allow only the resolved
        # writable roots. read_only resolves to zero roots, so writes stay denied.
        lines.append("(deny file-write*)")
        for root in writable:
            lines.append(f'(allow file-write* (subpath "{_escape_sandbox_string(str(root))}"))')
    profile = "\n".join(lines) + "\n"
    profile_path.write_text(profile, encoding="utf-8")
    return [sandbox_exec, "-f", str(profile_path), *command], [profile_path]


def _linux_bwrap_command(
    command: list[str], *, work_dir: Path, home: Path, spec: SandboxBuildSpec
) -> tuple[list[str], list[Path]]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return [], []

    writable = _resolve_writable_roots(spec, work_dir=work_dir, home=home)
    writable_set = {str(path) for path in writable}

    args = [
        bwrap,
        "--die-with-parent",
        "--unshare-all",
    ]
    if spec.network_access:
        # --unshare-all unshares the network namespace; --share-net re-shares it.
        args.append("--share-net")
    args += [
        "--new-session",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
    ]

    # Writable roots are bound read-write; work_dir and home are otherwise bound
    # read-only so the sandbox can still read them (read_only binds both ro).
    for root in writable:
        args += ["--bind", str(root), str(root)]
    if str(work_dir) not in writable_set:
        args += ["--ro-bind", str(work_dir), str(work_dir)]
    if str(home) not in writable_set:
        args += ["--ro-bind", str(home), str(home)]

    args += ["--setenv", "HOME", str(home), "--chdir", str(work_dir)]
    for path in ("/bin", "/usr", "/lib", "/lib64", "/opt/homebrew"):
        if Path(path).exists():
            args.extend(["--ro-bind", path, path])
    return [*args, *command], []


def build_sandboxed_agent_command(
    command: list[str],
    *,
    work_dir: Path,
    env: dict[str, str],
    spec: SandboxBuildSpec | None = None,
) -> AgentSandboxCommand:
    resolved_spec = spec if spec is not None else _LEGACY_SANDBOX_SPEC
    mode = os.environ.get("HIVE_CODE_SANDBOX_MODE", "auto").strip().lower() or "auto"
    if mode in {"none", "off", "disabled"}:
        if _allow_unsandboxed_code_exec():
            return AgentSandboxCommand(command=command)
        return AgentSandboxCommand(command=None, error=_sandbox_unavailable_message(mode))

    home = Path(env["HOME"]).resolve()
    if mode in {"auto", "darwin", "sandbox-exec"} and platform.system() == "Darwin":
        sandboxed, cleanup = _darwin_sandbox_command(command, work_dir=work_dir, home=home, spec=resolved_spec)
        if sandboxed:
            return AgentSandboxCommand(command=sandboxed, cleanup_paths=cleanup)
        if mode != "auto":
            return AgentSandboxCommand(command=None, error=_sandbox_unavailable_message(mode))

    if mode in {"auto", "bwrap", "bubblewrap", "linux"}:
        sandboxed, cleanup = _linux_bwrap_command(command, work_dir=work_dir, home=home, spec=resolved_spec)
        if sandboxed:
            return AgentSandboxCommand(command=sandboxed, cleanup_paths=cleanup)
        if mode != "auto":
            return AgentSandboxCommand(command=None, error=_sandbox_unavailable_message(mode))

    if _allow_unsandboxed_code_exec():
        return AgentSandboxCommand(command=command)
    return AgentSandboxCommand(command=None, error=_sandbox_unavailable_message(mode))


@lru_cache(maxsize=1)
def probe_os_sandbox_capability() -> SandboxCapabilityProbe:
    """Prove that the installed OS sandbox can actually launch a process.

    Binary presence is insufficient on hosts that forbid nested Seatbelt/user
    namespaces. The probe never falls back to a raw command and has no network
    or write side effect outside its disposable work/home directories.
    """

    provider: str | None = None
    builder = None
    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        provider = "sandbox-exec"
        builder = _darwin_sandbox_command
    elif shutil.which("bwrap"):
        provider = "bubblewrap"
        builder = _linux_bwrap_command
    if builder is None:
        return SandboxCapabilityProbe(False, None, "no OS sandbox binary is installed")

    cleanup_paths: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix="hive-sandbox-capability-") as temporary:
            root = Path(temporary)
            work = root / "work"
            home = root / "home"
            work.mkdir()
            home.mkdir()
            command, cleanup_paths = builder(
                ["/bin/sh", "-c", "printf hive-sandbox-probe-ok"],
                work_dir=work,
                home=home,
                spec=SandboxBuildSpec(profile=SandboxProfile.READ_ONLY),
            )
            if not command:
                return SandboxCapabilityProbe(False, provider, f"{provider} command construction failed")
            completed = subprocess.run(
                command,
                cwd=work,
                env={**os.environ, "HOME": str(home)},
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0 and "hive-sandbox-probe-ok" in completed.stdout:
                return SandboxCapabilityProbe(True, provider, "sandbox launch probe passed")
            detail = (completed.stderr or completed.stdout or f"exit={completed.returncode}").strip()
            return SandboxCapabilityProbe(False, provider, f"sandbox launch probe failed: {detail[-500:]}")
    except (OSError, subprocess.SubprocessError) as exc:
        return SandboxCapabilityProbe(False, provider, f"sandbox launch probe failed: {exc}")
    finally:
        for path in cleanup_paths:
            path.unlink(missing_ok=True)
