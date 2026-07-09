from __future__ import annotations

import base64

import pytest

from app.services.external_capabilities.materializer import materialize_file_bundle, materialize_remote_source


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
