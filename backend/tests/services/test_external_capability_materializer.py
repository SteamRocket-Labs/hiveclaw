from __future__ import annotations

import base64
import io
from pathlib import Path
import subprocess
import tarfile

import pytest

from app.services.external_capabilities.materializer import (
    materialize_file_bundle,
    materialize_git_source,
    materialize_local_source,
    materialize_npm_source,
    materialize_remote_source,
)


def _make_local_git_repo(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.dev", "-c", "user.name=T", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )


def _make_npm_tarball(members: dict[str, str], *, prefix: str = "package/") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in members.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"{prefix}{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_materialize_file_bundle_writes_quarantine_report_without_host_authority(tmp_path):
    package = materialize_file_bundle(
        source_format="external_skill_url",
        source_uri="https://github.com/acme/skills/tree/main/research",
        package_name="research",
        files=[
            {"path": "SKILL.md", "content": "---\nname: Research\n---\n\nUse sources carefully."},
            {"path": "references/guide.md", "content": "# Guide\n"},
        ],
        quarantine_root=tmp_path,
        resolved_ref="commit:abc123",
    )

    assert package.status == "quarantined"
    assert package.artifact_sha256
    assert package.resolved_ref == "commit:abc123"
    assert package.files[0]["path"] == "SKILL.md"
    assert package.report["sandbox"]["network"] == "deny"
    assert package.report["sandbox"]["inherited_host_secrets"] is False
    assert package.report["sandbox"]["host_home_mounted"] is False
    assert package.report["install_time_commands_executed"] == []
    assert package.report["quarantine"]["key"] == package.artifact_sha256[:24]
    assert (tmp_path / package.artifact_sha256[:24] / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (tmp_path / package.artifact_sha256[:24] / "materialization_report.json").exists()


def test_materialize_file_bundle_blocks_path_escape_and_install_commands(tmp_path):
    package = materialize_file_bundle(
        source_format="cc_plugin",
        source_uri="github:acme/plugin",
        package_name="plugin",
        files=[
            {"path": "../escape.md", "content": "bad"},
            {"path": "skills/a/SKILL.md", "content": "---\nname: A\n---\n"},
        ],
        install_commands=["npx skills add acme/plugin"],
        quarantine_root=tmp_path,
    )

    assert package.status == "blocked"
    assert package.files == [{"path": "skills/a/SKILL.md", "content": "---\nname: A\n---\n"}]
    assert {note["code"] for note in package.blocking_notes} == {
        "materialized_path_escape",
        "install_time_commands_require_isolated_worker",
    }
    assert not (tmp_path / package.artifact_sha256[:24] / "escape.md").exists()


@pytest.mark.asyncio
async def test_materialize_remote_github_tree_fetches_into_quarantine(tmp_path):
    async def fake_fetch_json(url, headers):
        assert headers == {"Authorization": "Bearer gh-test"}
        if url == "https://api.github.com/repos/acme/skills/contents/research?ref=main":
            return [
                {
                    "name": "SKILL.md",
                    "path": "research/SKILL.md",
                    "type": "file",
                    "url": "https://api.github.com/file/skill",
                    "size": 36,
                },
                {
                    "name": "references",
                    "path": "research/references",
                    "type": "dir",
                },
            ]
        if url == "https://api.github.com/repos/acme/skills/contents/research/references?ref=main":
            return [
                {
                    "name": "guide.md",
                    "path": "research/references/guide.md",
                    "type": "file",
                    "url": "https://api.github.com/file/guide",
                    "size": 8,
                }
            ]
        if url == "https://api.github.com/file/skill":
            return {"content": base64.b64encode(b"---\nname: Research\n---\n\nUse sources.\n").decode()}
        if url == "https://api.github.com/file/guide":
            return {"content": base64.b64encode(b"# Guide\n").decode()}
        raise AssertionError(f"unexpected url: {url}")

    package = await materialize_remote_source(
        source_uri="https://github.com/acme/skills/tree/main/research",
        source_format="external_skill_url",
        package_name="research",
        token="gh-test",
        quarantine_root=tmp_path,
        fetch_json=fake_fetch_json,
    )

    assert package.status == "quarantined"
    assert [item["path"] for item in package.files] == ["SKILL.md", "references/guide.md"]
    assert package.report["remote_fetch"]["source_kind"] == "github_tree"
    assert package.report["remote_fetch"]["host_allowlist_enforced"] is True
    assert package.report["sandbox"]["inherited_host_secrets"] is False
    assert package.report["install_time_commands_executed"] == []
    assert (tmp_path / package.artifact_sha256[:24] / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_materialize_remote_source_blocks_install_command_without_execution():
    async def fail_fetch_json(*_args, **_kwargs):
        raise AssertionError("install command materialization must not fetch or execute")

    package = await materialize_remote_source(
        source_uri="npx skills add acme/research",
        source_format="skills_sh_command",
        package_name="research",
        fetch_json=fail_fetch_json,
    )

    assert package.status == "blocked"
    assert package.files == []
    assert package.report["install_time_commands_executed"] == []
    assert package.blocking_notes == [
        {
            "code": "install_time_commands_require_isolated_worker",
            "command": "npx skills add acme/research",
        }
    ]


@pytest.mark.asyncio
async def test_materialize_remote_source_blocks_non_allowlisted_host():
    async def fail_fetch_json(*_args, **_kwargs):
        raise AssertionError("non-allowlisted hosts must not be fetched")

    package = await materialize_remote_source(
        source_uri="https://downloads.example.invalid/skill.zip",
        source_format="external_skill_url",
        package_name="skill",
        fetch_json=fail_fetch_json,
    )

    assert package.status == "blocked"
    assert package.files == []
    assert package.blocking_notes[0]["code"] == "remote_host_not_allowed"


# --- C2 local sources (file / directory), FreeCode marketplaceManager.ts:1623/1636 ---


def test_materialize_local_file_source_reads_single_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: local\n---\nLocal skill.", encoding="utf-8")
    quarantine = tmp_path / "q"

    package = materialize_local_source(
        source_path=str(src / "SKILL.md"),
        source_format="cc_plugin",
        package_name="local",
        source_kind="file",
        quarantine_root=quarantine,
        allowed_roots=[src],
    )

    assert package.status == "quarantined"
    assert package.files == [{"path": "SKILL.md", "content": "---\nname: local\n---\nLocal skill."}]
    assert package.report["source_kind"] == "file"
    assert (quarantine / package.artifact_sha256[:24] / "SKILL.md").exists()


def test_materialize_local_directory_source_preserves_tree(tmp_path):
    src = tmp_path / "plugin"
    (src / "skills" / "audit").mkdir(parents=True)
    (src / "skills" / "audit" / "SKILL.md").write_text("# Audit", encoding="utf-8")
    (src / ".claude-plugin").mkdir()
    (src / ".claude-plugin" / "plugin.json").write_text('{"name": "local-pack"}', encoding="utf-8")

    package = materialize_local_source(
        source_path=str(src),
        source_format="cc_plugin",
        package_name="local-pack",
        source_kind="directory",
        allowed_roots=[tmp_path],
    )

    assert package.status == "quarantined"
    paths = {item["path"] for item in package.files}
    assert paths == {"skills/audit/SKILL.md", ".claude-plugin/plugin.json"}
    assert package.report["source_kind"] == "directory"


def test_materialize_local_directory_rejects_symlinks(tmp_path):
    src = tmp_path / "plugin"
    src.mkdir()
    (src / "real.md").write_text("real", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    (src / "link.md").symlink_to(secret)

    package = materialize_local_source(
        source_path=str(src),
        source_format="cc_plugin",
        package_name="local-pack",
        source_kind="directory",
        allowed_roots=[tmp_path],
    )

    paths = {item["path"] for item in package.files}
    assert paths == {"real.md"}  # symlink is skipped, secret content never read
    assert any(note["code"] == "materialized_symlink_rejected" for note in package.blocking_notes)


def test_materialize_local_source_blocks_path_outside_allowed_roots(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("x", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    package = materialize_local_source(
        source_path=str(outside / "SKILL.md"),
        source_format="cc_plugin",
        package_name="local",
        source_kind="file",
        allowed_roots=[allowed],
    )

    assert package.status == "blocked"
    assert package.files == []
    assert package.blocking_notes[0]["code"] == "local_source_outside_allowed_roots"


def test_materialize_local_source_blocks_missing_path(tmp_path):
    package = materialize_local_source(
        source_path=str(tmp_path / "does-not-exist"),
        source_format="cc_plugin",
        package_name="local",
        allowed_roots=[tmp_path],
    )

    assert package.status == "blocked"
    assert package.blocking_notes[0]["code"] == "local_source_not_found"


# --- C2 generic git (hardened shallow clone), FreeCode marketplaceManager.ts:836-897 ---


@pytest.mark.asyncio
async def test_materialize_git_source_shallow_clones_local_repo_and_strips_git(tmp_path):
    repo = tmp_path / "upstream"
    _make_local_git_repo(
        repo,
        {
            ".claude-plugin/plugin.json": '{"name": "git-pack"}',
            "SKILL.md": "# Git skill",
        },
    )

    package = await materialize_git_source(
        git_url=f"file://{repo}",
        source_format="cc_plugin",
        package_name="git-pack",
        quarantine_root=tmp_path / "q",
        allowed_roots=[tmp_path],
    )

    assert package.status == "quarantined"
    paths = {item["path"] for item in package.files}
    assert paths == {".claude-plugin/plugin.json", "SKILL.md"}
    # .git internals are stripped from the materialized bundle.
    assert not any(path.startswith(".git/") or path == ".git" for path in paths)
    assert package.report["source_kind"] == "git"


@pytest.mark.asyncio
async def test_materialize_git_source_rejects_symlink_members(tmp_path):
    repo = tmp_path / "upstream"
    _make_local_git_repo(repo, {"real.md": "real"})
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    (repo / "link.md").symlink_to(secret)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.dev", "-c", "user.name=T", "commit", "-q", "-m", "link"],
        cwd=repo,
        check=True,
    )

    package = await materialize_git_source(
        git_url=f"file://{repo}",
        source_format="cc_plugin",
        package_name="git-pack",
        allowed_roots=[tmp_path],
    )

    paths = {item["path"] for item in package.files}
    assert "real.md" in paths
    assert "link.md" not in paths  # symlink not followed, secret never read
    assert all("top-secret" not in item["content"] for item in package.files)


@pytest.mark.asyncio
async def test_materialize_git_source_blocks_disallowed_scheme(tmp_path):
    package = await materialize_git_source(
        git_url="ext::sh -c 'touch /tmp/pwned'",
        source_format="cc_plugin",
        package_name="evil",
        allowed_roots=[tmp_path],
    )

    assert package.status == "blocked"
    assert package.files == []
    assert package.blocking_notes[0]["code"] == "git_scheme_not_allowed"


# --- C2 npm (read-only tarball, Hive ahead of CC which is a TODO at :1618) ---


@pytest.mark.asyncio
async def test_materialize_npm_source_downloads_and_unpacks_tarball(tmp_path):
    tarball = _make_npm_tarball(
        {
            "package.json": '{"name": "@acme/pack", "version": "1.0.0"}',
            "SKILL.md": "# Npm skill",
        }
    )

    async def fake_fetch_json(url, headers):
        assert url == "https://registry.npmjs.org/@acme/pack"
        return {
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {"dist": {"tarball": "https://registry.npmjs.org/@acme/pack/-/pack-1.0.0.tgz"}}},
        }

    async def fake_fetch_bytes(url, headers):
        assert url == "https://registry.npmjs.org/@acme/pack/-/pack-1.0.0.tgz"
        return tarball

    package = await materialize_npm_source(
        package="@acme/pack",
        source_format="cc_plugin",
        package_name="acme-pack",
        quarantine_root=tmp_path / "q",
        fetch_json=fake_fetch_json,
        fetch_bytes=fake_fetch_bytes,
    )

    assert package.status == "quarantined"
    paths = {item["path"] for item in package.files}
    # The leading "package/" tarball prefix is stripped.
    assert paths == {"package.json", "SKILL.md"}
    assert package.report["source_kind"] == "npm"
    assert package.resolved_ref == "npm:@acme/pack@1.0.0"


@pytest.mark.asyncio
async def test_materialize_npm_source_rejects_path_traversal_member(tmp_path):
    # Craft a tarball with a member escaping the package root.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in ("package/ok.md", "package/../../escape.md"):
            data = b"x"
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    tarball = buffer.getvalue()

    async def fake_fetch_json(url, headers):
        return {
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {"dist": {"tarball": "https://registry.npmjs.org/p/-/p-1.0.0.tgz"}}},
        }

    async def fake_fetch_bytes(url, headers):
        return tarball

    package = await materialize_npm_source(
        package="p",
        source_format="cc_plugin",
        package_name="p",
        fetch_json=fake_fetch_json,
        fetch_bytes=fake_fetch_bytes,
    )

    paths = {item["path"] for item in package.files}
    assert paths == {"ok.md"}
    assert not (tmp_path.parent / "escape.md").exists()


@pytest.mark.asyncio
async def test_materialize_npm_source_rejects_symlink_member(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"real"
        info = tarfile.TarInfo(name="package/real.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(name="package/link.md")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    tarball = buffer.getvalue()

    async def fake_fetch_json(url, headers):
        return {
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {"dist": {"tarball": "https://registry.npmjs.org/p/-/p-1.0.0.tgz"}}},
        }

    async def fake_fetch_bytes(url, headers):
        return tarball

    package = await materialize_npm_source(
        package="p",
        source_format="cc_plugin",
        package_name="p",
        fetch_json=fake_fetch_json,
        fetch_bytes=fake_fetch_bytes,
    )

    paths = {item["path"] for item in package.files}
    assert paths == {"real.md"}
    assert any(note["code"] == "materialized_symlink_rejected" for note in package.blocking_notes)


@pytest.mark.asyncio
async def test_materialize_npm_source_blocks_disallowed_registry():
    async def fail_fetch_json(*_args, **_kwargs):
        raise AssertionError("disallowed registry must not be fetched")

    package = await materialize_npm_source(
        package="p",
        source_format="cc_plugin",
        package_name="p",
        registry="https://evil.example.invalid",
        fetch_json=fail_fetch_json,
        fetch_bytes=fail_fetch_json,
    )

    assert package.status == "blocked"
    assert package.blocking_notes[0]["code"] == "npm_registry_not_allowed"
