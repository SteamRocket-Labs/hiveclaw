"""Protected source roots cannot be waived by local development fallback."""

from pathlib import Path

import pytest

from app.services import subprocess_sandbox as sandbox


@pytest.mark.parametrize("mode", ["off", "auto"])
def test_private_source_scope_never_runs_without_its_os_boundary(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", mode)
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "1")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    result = sandbox.build_sandboxed_agent_command(["echo", "private"], work_dir=tmp_path,
        env={"HOME": str(tmp_path / "home")},
        spec=sandbox.SandboxBuildSpec(protected_roots=(str(tmp_path / "sources"),)))
    assert result.command is None and "Protected session sources" in result.error


def test_scoped_darwin_profile_exposes_ancestor_metadata_without_their_contents(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    work, home = tmp_path / "job/workspace", tmp_path / "job/home"
    spec = sandbox.SandboxBuildSpec(protected_roots=(str(tmp_path),))
    argv, cleanup = sandbox._darwin_sandbox_command(["echo", "ok"], work_dir=work, home=home, spec=spec)
    try:
        profile = Path(argv[2]).read_text()
        assert f'(allow file-read-metadata (literal "{tmp_path}"))' in profile
        assert f'(allow file-read* (subpath "{tmp_path}"))' not in profile
        assert f'(deny file-read* file-write* (subpath "{tmp_path}"))' in profile
    finally:
        for path in cleanup:
            path.unlink()


def test_scoped_bwrap_hides_sources_before_rebinding_authorized_job(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")
    work, home = tmp_path / "job/workspace", tmp_path / "job/home"
    spec = sandbox.SandboxBuildSpec(protected_roots=(str(tmp_path),))
    argv, _cleanup = sandbox._linux_bwrap_command(["echo", "ok"], work_dir=work, home=home, spec=spec)
    mask = next(index for index in range(len(argv) - 1) if argv[index:index + 2] == ["--tmpfs", str(tmp_path)])
    binding = max(index for index in range(len(argv) - 2) if argv[index:index + 3] == ["--bind", str(work), str(work)])
    assert mask < binding
