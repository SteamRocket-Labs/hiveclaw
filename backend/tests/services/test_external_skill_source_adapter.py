from __future__ import annotations

from app.services.external_capabilities.materializer import materialize_local_source
from app.services.external_capabilities.skill_source_adapter import build_external_skill_bundle


def test_local_directory_source_builds_skill_bundle_through_materializer(tmp_path):
    # C2 wiring: a local plugin/skill directory materializes into the same
    # quarantine-only staging bundle that the Trust Gate review flow consumes.
    src = tmp_path / "helper-skill"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: helper\n---\nHelp the user.", encoding="utf-8")
    (src / "reference.md").write_text("# Reference", encoding="utf-8")

    materialized = materialize_local_source(
        source_path=str(src),
        source_format="cc_plugin",
        package_name="helper",
        source_kind="directory",
        allowed_roots=[tmp_path],
    )
    bundle, guard_report, materialization_report = build_external_skill_bundle(
        source_uri=str(src),
        folder_name="helper",
        files=materialized.files,
        source_format="cc_plugin",
        materialized=materialized,
    )

    assert bundle.source_format == "cc_plugin"
    assert bundle.plugin_name == "helper"
    assert len(bundle.components) == 1
    component = bundle.components[0]
    assert component.component_type == "skill"
    stored = {file["path"] for file in component.metadata["files"]}
    assert stored == {"SKILL.md", "reference.md"}
    # No SKILL.md-missing / guard-blocked admission notes for a valid skill dir.
    assert not any(note.get("code") == "missing_skill_md" for note in bundle.admission_notes)
    assert materialization_report["source_kind"] == "directory"


def test_local_directory_without_skill_md_flags_admission_note(tmp_path):
    src = tmp_path / "no-skill"
    src.mkdir()
    (src / "README.md").write_text("# Not a skill", encoding="utf-8")

    materialized = materialize_local_source(
        source_path=str(src),
        source_format="cc_plugin",
        package_name="no-skill",
        source_kind="directory",
        allowed_roots=[tmp_path],
    )
    bundle, _guard_report, _materialization_report = build_external_skill_bundle(
        source_uri=str(src),
        folder_name="no-skill",
        files=materialized.files,
        source_format="cc_plugin",
        materialized=materialized,
    )

    assert any(note.get("code") == "missing_skill_md" for note in bundle.admission_notes)
