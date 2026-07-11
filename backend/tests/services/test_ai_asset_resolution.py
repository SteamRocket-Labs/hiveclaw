from __future__ import annotations

from pathlib import Path
from uuid import uuid4


def test_skill_asset_resolution_uses_actual_loaded_folder_not_model_selector(tmp_path: Path) -> None:
    from app.services.ai_asset_resolution import resolve_skill_native_consumption

    workspace = tmp_path / "agent"
    skill_path = workspace / "skills" / "report-folder" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: Report Display Name\ndescription: Produce reports.\n---\n# Report\n",
        encoding="utf-8",
    )
    agent_id = uuid4()

    consumption = resolve_skill_native_consumption(
        workspace=workspace,
        agent_id=agent_id,
        selector="Report Display Name",
    )

    assert consumption is not None
    assert consumption.native_key == f"skill:agent:{agent_id}:report-folder"
    assert consumption.relative_path == "skills/report-folder/SKILL.md"
    assert consumption.folder_name == "report-folder"
    assert "Report Display Name" not in consumption.native_key


def test_skill_asset_resolution_preserves_session_overlay_identity(tmp_path: Path) -> None:
    from app.services.ai_asset_resolution import resolve_skill_native_consumption

    session_id = "session-1"
    skill_path = tmp_path / "session_extensions" / session_id / "skills" / "trial" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: Session Trial\ndescription: Trial only.\n---\n# Trial\n",
        encoding="utf-8",
    )

    consumption = resolve_skill_native_consumption(
        workspace=tmp_path,
        agent_id=uuid4(),
        selector="Session Trial",
        session_id=session_id,
    )

    assert consumption is not None
    assert consumption.session_overlay is True
    assert consumption.relative_path == f"session_extensions/{session_id}/skills/trial/SKILL.md"
    assert consumption.native_key is None


def test_flat_skill_asset_resolution_has_stable_non_folder_identity(tmp_path: Path) -> None:
    from app.services.ai_asset_resolution import resolve_skill_native_consumption

    skill_path = tmp_path / "skills" / "legacy-report.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: Legacy Report\ndescription: Flat layout.\n---\n# Report\n",
        encoding="utf-8",
    )
    agent_id = uuid4()

    consumption = resolve_skill_native_consumption(
        workspace=tmp_path,
        agent_id=agent_id,
        selector="Legacy Report",
    )

    assert consumption is not None
    assert consumption.native_key == f"skill:agent:{agent_id}:flat:legacy-report"
    assert consumption.file_name == "legacy-report.md"


def test_external_skill_materialization_without_legacy_status_is_a_real_component_use() -> None:
    from app.services.ai_asset_resolution import _component_consumed_match

    assert _component_consumed_match(
        {"component_type": "skill", "name": "report", "files_written": ["SKILL.md"]},
        {"skill", "slash_command"},
        {"report"},
    )
    assert not _component_consumed_match(
        {"component_type": "hook", "name": "report", "status": "pending_hook_approval"},
        {"skill"},
        {"report"},
    )


def test_asset_ref_set_comparison_detects_approval_revision_drift() -> None:
    from app.runtime.ccplus_contracts import ResolvedAssetRefV1
    from app.services.ai_asset_resolution import resolved_asset_refs_match

    common = {
        "asset_id": str(uuid4()),
        "asset_type": "skill",
        "native_key": "skill:agent:a:report",
        "revision_version": 2,
        "content_hash": "hash-v2",
        "source_ref": "agent:a/skills/report",
    }
    approved = ResolvedAssetRefV1(revision_id=str(uuid4()), **common)
    same = ResolvedAssetRefV1(**{**common, "revision_id": approved.revision_id})
    drifted = ResolvedAssetRefV1(
        **{
            **common,
            "revision_id": str(uuid4()),
            "revision_version": 3,
            "content_hash": "hash-v3",
        }
    )

    assert resolved_asset_refs_match((approved,), (same,)) is True
    assert resolved_asset_refs_match((approved,), (drifted,)) is False
