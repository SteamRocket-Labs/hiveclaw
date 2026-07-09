"""Tests for the four-tier OS sandbox builder (D1).

Two layers:

* Parameter-construction tests run UNCONDITIONALLY (they monkeypatch
  ``shutil.which`` so the darwin/linux generators run on any host) and assert the
  generated ``sandbox-exec`` profile / ``bwrap`` argv reflects the requested
  SandboxProfile + network toggle.
* Behavior tests run the real sandbox and are skip-guarded by ``sandbox-exec`` /
  ``bwrap`` availability.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.runtime.ccplus_contracts import SandboxProfile
from app.services import subprocess_sandbox
from app.services.subprocess_sandbox import (
    SandboxBuildSpec,
    _darwin_sandbox_command,
    _linux_bwrap_command,
    _resolve_writable_roots,
    build_sandboxed_agent_command,
)

_HAS_SANDBOX_EXEC = shutil.which("sandbox-exec") is not None
_HAS_BWRAP = shutil.which("bwrap") is not None


# ── Pure writable-root resolution (unconditional) ──────────────────────


def test_resolve_writable_roots_per_profile():
    work = Path("/tmp/agent/work")
    home = Path("/tmp/agent/home")

    assert _resolve_writable_roots(SandboxBuildSpec(profile=SandboxProfile.READ_ONLY), work_dir=work, home=home) == ()
    assert _resolve_writable_roots(
        SandboxBuildSpec(profile=SandboxProfile.FULL_ACCESS_LOCAL_ONLY), work_dir=work, home=home
    ) == (work, home)
    assert _resolve_writable_roots(
        SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE), work_dir=work, home=home
    ) == (work,)
    # Explicit whitelist: absolute stays, relative resolves against work_dir.
    assert _resolve_writable_roots(
        SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE, writable_roots=("out", "/data")),
        work_dir=work,
        home=home,
    ) == (work / "out", Path("/data"))
    # EXTERNAL_SANDBOX degrades to workspace scoping locally.
    assert _resolve_writable_roots(
        SandboxBuildSpec(profile=SandboxProfile.EXTERNAL_SANDBOX), work_dir=work, home=home
    ) == (work,)


# ── Darwin sandbox-exec profile construction (unconditional) ───────────


def _darwin_profile(monkeypatch, spec: SandboxBuildSpec, work: Path, home: Path) -> str:
    monkeypatch.setattr(subprocess_sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    argv, cleanup = _darwin_sandbox_command(["echo", "x"], work_dir=work, home=home, spec=spec)
    assert argv and argv[0] == "/usr/bin/sandbox-exec"
    assert argv[1] == "-f"
    profile_path = Path(argv[2])
    try:
        text = profile_path.read_text(encoding="utf-8")
    finally:
        for path in cleanup:
            path.unlink(missing_ok=True)
    return text


def test_darwin_read_only_denies_all_writes(monkeypatch):
    work, home = Path("/tmp/w"), Path("/tmp/h")
    profile = _darwin_profile(monkeypatch, SandboxBuildSpec(profile=SandboxProfile.READ_ONLY), work, home)
    assert "(deny file-write*)" in profile
    assert "allow file-write*" not in profile  # nothing is writable


def test_darwin_workspace_write_allows_only_writable_roots(monkeypatch):
    work, home = Path("/tmp/w"), Path("/tmp/h")
    profile = _darwin_profile(monkeypatch, SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE), work, home)
    assert "(deny file-write*)" in profile
    assert '(allow file-write* (subpath "/tmp/w"))' in profile
    # Home is NOT writable under workspace_write.
    assert '(subpath "/tmp/h")' not in profile.split("(deny file-write*)", 1)[1]


def test_darwin_full_access_allows_work_and_home(monkeypatch):
    work, home = Path("/tmp/w"), Path("/tmp/h")
    profile = _darwin_profile(monkeypatch, SandboxBuildSpec(profile=SandboxProfile.FULL_ACCESS_LOCAL_ONLY), work, home)
    assert '(allow file-write* (subpath "/tmp/w") (subpath "/tmp/h"))' in profile


def test_darwin_network_toggle(monkeypatch):
    work, home = Path("/tmp/w"), Path("/tmp/h")
    denied = _darwin_profile(
        monkeypatch, SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE, network_access=False), work, home
    )
    assert "(deny network*)" in denied
    allowed = _darwin_profile(
        monkeypatch, SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE, network_access=True), work, home
    )
    assert "(allow network*)" in allowed


# ── Linux bwrap argv construction (unconditional) ──────────────────────


def _bwrap_argv(monkeypatch, spec: SandboxBuildSpec, work: Path, home: Path) -> list[str]:
    monkeypatch.setattr(subprocess_sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")
    argv, _ = _linux_bwrap_command(["echo", "x"], work_dir=work, home=home, spec=spec)
    assert argv and argv[0] == "/usr/bin/bwrap"
    return argv


def _pairs(argv: list[str], flag: str) -> set[tuple[str, str]]:
    return {(argv[i + 1], argv[i + 2]) for i, tok in enumerate(argv) if tok == flag and i + 2 < len(argv)}


def test_bwrap_read_only_binds_read_only(monkeypatch):
    work, home = Path("/tmp/w"), Path("/tmp/h")
    argv = _bwrap_argv(monkeypatch, SandboxBuildSpec(profile=SandboxProfile.READ_ONLY), work, home)
    ro = _pairs(argv, "--ro-bind")
    assert ("/tmp/w", "/tmp/w") in ro
    assert ("/tmp/h", "/tmp/h") in ro
    assert ("/tmp/w", "/tmp/w") not in _pairs(argv, "--bind")  # not writable


def test_bwrap_workspace_write_binds_work_rw_home_ro(monkeypatch):
    work, home = Path("/tmp/w"), Path("/tmp/h")
    argv = _bwrap_argv(monkeypatch, SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE), work, home)
    assert ("/tmp/w", "/tmp/w") in _pairs(argv, "--bind")
    assert ("/tmp/h", "/tmp/h") in _pairs(argv, "--ro-bind")


def test_bwrap_full_access_binds_work_and_home_rw(monkeypatch):
    work, home = Path("/tmp/w"), Path("/tmp/h")
    argv = _bwrap_argv(monkeypatch, SandboxBuildSpec(profile=SandboxProfile.FULL_ACCESS_LOCAL_ONLY), work, home)
    rw = _pairs(argv, "--bind")
    assert ("/tmp/w", "/tmp/w") in rw
    assert ("/tmp/h", "/tmp/h") in rw


def test_bwrap_network_toggle(monkeypatch):
    work, home = Path("/tmp/w"), Path("/tmp/h")
    denied = _bwrap_argv(monkeypatch, SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE), work, home)
    assert "--share-net" not in denied
    allowed = _bwrap_argv(
        monkeypatch, SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE, network_access=True), work, home
    )
    assert "--share-net" in allowed


# ── Backward-compat: no spec preserves the legacy permissive scope ─────


def test_build_without_spec_uses_legacy_full_access(monkeypatch):
    monkeypatch.setattr(subprocess_sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(subprocess_sandbox.platform, "system", lambda: "Darwin")
    result = build_sandboxed_agent_command(["echo", "x"], work_dir=Path("/tmp/w"), env={"HOME": "/tmp/h"})
    assert result.command is not None
    profile = Path(result.command[2]).read_text(encoding="utf-8")
    try:
        # Legacy default = full_access_local_only → work+home writable.
        assert '(allow file-write* (subpath "/tmp/w")' in profile
        assert "(deny network*)" in profile
    finally:
        for path in result.cleanup_paths:
            path.unlink(missing_ok=True)


# ── Behavior tests (skip-guarded by real sandbox availability) ─────────


@pytest.mark.asyncio
@pytest.mark.skipif(not (_HAS_SANDBOX_EXEC or _HAS_BWRAP), reason="no OS sandbox (sandbox-exec/bwrap) available")
async def test_read_only_profile_blocks_workspace_write(tmp_path):
    from app.services.code_execution.local_provider import execute_local_sandboxed_command

    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    target = work / "should_not_exist.txt"

    result = await execute_local_sandboxed_command(
        ["/bin/sh", "-c", f"echo hi > {target}"],
        work_dir=work,
        env={"HOME": str(home)},
        timeout=15,
        spec=SandboxBuildSpec(profile=SandboxProfile.READ_ONLY),
    )
    # Either the write is denied (non-zero exit) or the file is simply absent.
    assert result.exit_code != 0 or not target.exists()
    assert result.evidence.get("sandbox_profile") == "read_only"


@pytest.mark.asyncio
@pytest.mark.skipif(not (_HAS_SANDBOX_EXEC or _HAS_BWRAP), reason="no OS sandbox (sandbox-exec/bwrap) available")
async def test_workspace_write_profile_allows_workspace_write(tmp_path):
    from app.services.code_execution.local_provider import execute_local_sandboxed_command

    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    target = work / "created.txt"

    result = await execute_local_sandboxed_command(
        ["/bin/sh", "-c", f"echo hi > {target}"],
        work_dir=work,
        env={"HOME": str(home)},
        timeout=15,
        spec=SandboxBuildSpec(profile=SandboxProfile.WORKSPACE_WRITE),
    )
    assert result.exit_code == 0, f"stderr={result.stderr!r} error={result.error!r}"
    assert target.exists()
    assert result.evidence.get("sandbox_profile") == "workspace_write"
