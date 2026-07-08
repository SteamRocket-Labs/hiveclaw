from __future__ import annotations

from app.services.external_capabilities.materializer import materialize_file_bundle


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
