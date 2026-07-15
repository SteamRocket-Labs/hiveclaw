from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_install_active_skill_package_writes_guarded_files(tmp_path):
    from app.services.skill_installation import install_active_skill_package

    result = install_active_skill_package(
        workspace=tmp_path,
        folder_name="deploy-checklist",
        files=[
            {"path": "SKILL.md", "content": "---\nname: Deploy Checklist\n---\n# Deploy Checklist\n"},
            {"path": "references/checks.md", "content": "Run health check after deploy.\n"},
        ],
        source="test",
    )

    assert result["status"] == "installed"
    assert result["folder_name"] == "deploy-checklist"
    assert result["files_written"] == 2
    assert (tmp_path / "skills" / "deploy-checklist" / "SKILL.md").exists()
    assert (tmp_path / "skills" / "deploy-checklist" / "references" / "checks.md").exists()
    assert result["skill_guard"]["allowed"] is True


def test_install_active_skill_package_blocks_unsafe_files_without_partial_write(tmp_path):
    from app.services.skill_installation import install_active_skill_package

    with pytest.raises(ValueError, match="SkillGuard blocked"):
        install_active_skill_package(
            workspace=tmp_path,
            folder_name="unsafe",
            files=[
                {"path": "SKILL.md", "content": "---\nname: unsafe\n---\nOPENAI_API_KEY=abcdef123456"},
                {"path": "../escape.md", "content": "escape"},
            ],
            source="test",
        )

    assert not (tmp_path / "skills" / "unsafe").exists()
    assert not (tmp_path / "escape.md").exists()


def test_install_active_skill_package_exact_overwrite_is_zero_write(tmp_path: Path) -> None:
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.skill_installation import install_active_skill_package

    files = [
        {"path": "SKILL.md", "content": "---\nname: Deploy Checklist\n---\n# Deploy Checklist\n"},
        {"path": "references/checks.md", "content": "Run health check after deploy.\n"},
    ]
    install_active_skill_package(
        workspace=tmp_path,
        folder_name="deploy-checklist",
        files=files,
        source="startup_default_registry_skill:skill-1",
        overwrite=True,
    )

    skill_path = tmp_path / "skills" / "deploy-checklist" / "SKILL.md"
    review_path = tmp_path / "evolution" / "skill_review.md"
    stable_ns = 1_700_000_000_000_000_000
    os.utime(skill_path, ns=(stable_ns, stable_ns))
    os.utime(review_path, ns=(stable_ns, stable_ns))
    revision_before = read_agent_asset_revision(tmp_path)
    review_before = review_path.read_bytes()
    journals_before = sorted(
        (tmp_path / "runtime_artifacts" / "asset_transactions" / "transactions").glob("*/journal.json")
    )

    result = install_active_skill_package(
        workspace=tmp_path,
        folder_name="deploy-checklist",
        files=files,
        source="startup_default_registry_skill:skill-1",
        overwrite=True,
    )

    assert result["status"] == "unchanged"
    assert result["files_written"] == 0
    assert result["files"] == []
    assert read_agent_asset_revision(tmp_path) == revision_before
    assert review_path.read_bytes() == review_before
    assert skill_path.stat().st_mtime_ns == stable_ns
    assert review_path.stat().st_mtime_ns == stable_ns
    assert sorted(
        (tmp_path / "runtime_artifacts" / "asset_transactions" / "transactions").glob("*/journal.json")
    ) == journals_before


def test_default_skill_startup_batches_one_recovery_scan_per_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services import agent_asset_transaction as asset_tx
    from app.services.skill_installation import install_active_skill_package
    from app.services.skill_seeder import _push_default_skill_packages_to_agent

    skills = [
        SimpleNamespace(
            id="skill-1",
            name="One",
            folder_name="one",
            files=[SimpleNamespace(path="SKILL.md", content="---\nname: One\n---\n# One\n")],
        ),
        SimpleNamespace(
            id="skill-2",
            name="Two",
            folder_name="two",
            files=[SimpleNamespace(path="SKILL.md", content="---\nname: Two\n---\n# Two\n")],
        ),
    ]
    for skill in skills:
        install_active_skill_package(
            workspace=tmp_path,
            folder_name=skill.folder_name,
            files=[{"path": item.path, "content": item.content} for item in skill.files],
            source=f"startup_default_registry_skill:{skill.id}",
            overwrite=True,
        )

    real_recover = asset_tx._recover_incomplete_locked
    recovery_calls = 0

    def count_recovery_calls(agent_root: Path):
        nonlocal recovery_calls
        recovery_calls += 1
        return real_recover(agent_root)

    monkeypatch.setattr(asset_tx, "_recover_incomplete_locked", count_recovery_calls)
    journals_before = sorted(
        (tmp_path / "runtime_artifacts" / "asset_transactions" / "transactions").glob("*/journal.json")
    )

    result = _push_default_skill_packages_to_agent(agent_dir=tmp_path, default_skills=skills)

    assert result == {"pushed": 0, "updated": 0, "unchanged": 2}
    assert recovery_calls == 1
    assert sorted(
        (tmp_path / "runtime_artifacts" / "asset_transactions" / "transactions").glob("*/journal.json")
    ) == journals_before
