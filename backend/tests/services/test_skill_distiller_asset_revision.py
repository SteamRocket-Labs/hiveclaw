from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_skill_asset_revision_failure_restores_previous_native_file(monkeypatch, tmp_path: Path) -> None:
    from app.services.skill_distiller import _commit_skill_with_asset_revision

    workspace = tmp_path / "agent"
    target = workspace / "skills" / "review" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    async def fail_registration(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.services.ai_assets.register_evolved_workspace_skill_asset", fail_registration)
    result = await _commit_skill_with_asset_revision(
        workspace=workspace,
        target_relative_path="skills/review/SKILL.md",
        rendered_markdown="---\nname: Review\ndescription: Review.\n---\n\n# Review\n",
        skill_name="Review",
        overwrite=True,
        status="provisional",
        candidate_id="candidate-1",
        skill_origin="t3_auto_created",
        agent_id=uuid4(),
        tenant_id=uuid4(),
    )

    assert result.startswith("❌ skill asset revision failed")
    assert target.read_text(encoding="utf-8") == "old"


@pytest.mark.asyncio
async def test_skill_asset_revision_failure_removes_new_native_file(monkeypatch, tmp_path: Path) -> None:
    from app.services.skill_distiller import _commit_skill_with_asset_revision

    workspace = tmp_path / "agent"
    workspace.mkdir()

    async def fail_registration(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.services.ai_assets.register_evolved_workspace_skill_asset", fail_registration)
    result = await _commit_skill_with_asset_revision(
        workspace=workspace,
        target_relative_path="skills/review/SKILL.md",
        rendered_markdown="---\nname: Review\ndescription: Review.\n---\n\n# Review\n",
        skill_name="Review",
        overwrite=False,
        status="provisional",
        candidate_id="candidate-1",
        skill_origin="t3_auto_created",
        agent_id=uuid4(),
        tenant_id=uuid4(),
    )

    assert result.startswith("❌ skill asset revision failed")
    assert not (workspace / "skills" / "review").exists()
