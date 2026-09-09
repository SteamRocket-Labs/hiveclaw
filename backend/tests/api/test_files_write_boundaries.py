from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_file_api_rejects_direct_soul_and_skill_writes(tmp_path, monkeypatch):
    import app.api.files as files_api

    agent_id = uuid4()
    (tmp_path / str(agent_id)).mkdir(parents=True)
    monkeypatch.setattr(files_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "manage"

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)

    for path in (
        "soul.md",
        "skills/deploy-checklist/SKILL.md",
        "subagents/reviewer.md",
        "enterprise_info/company_profile.md",
    ):
        with pytest.raises(HTTPException) as exc_info:
            await files_api.write_file(
                agent_id=agent_id,
                path=path,
                data=files_api.FileWrite(content="raw bypass"),
                current_user=SimpleNamespace(id=uuid4()),
                db=SimpleNamespace(),
            )

        assert exc_info.value.status_code == 403

    assert not (tmp_path / str(agent_id) / "soul.md").exists()
    assert not (tmp_path / str(agent_id) / "skills" / "deploy-checklist" / "SKILL.md").exists()
    assert not (tmp_path / str(agent_id) / "subagents" / "reviewer.md").exists()
    assert not (tmp_path / str(agent_id) / "enterprise_info" / "company_profile.md").exists()


def test_file_api_rejects_skill_upload_guard():
    import app.api.files as files_api

    with pytest.raises(HTTPException) as exc_info:
        files_api._raise_upload_path_guard("skills/deploy-checklist", "SKILL.md")

    assert exc_info.value.status_code == 403
    assert "Platform Skill Gate" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_skill_uninstall_requires_manage_access_before_filesystem_effect(tmp_path, monkeypatch):
    import app.api.files as files_api
    from app.services.skill_installation import install_active_skill_package

    agent_id, user_id = uuid4(), uuid4()
    workspace = tmp_path / str(agent_id)
    install_active_skill_package(
        workspace=workspace,
        folder_name="owned",
        source="test",
        files=[
            {"path": "SKILL.md", "content": "---\nname: Owned\n---\n# Owned\n"},
        ],
    )
    monkeypatch.setattr(files_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    async def deny(*_args):
        raise HTTPException(status_code=403, detail="Manage access required")

    monkeypatch.setattr(files_api, "require_agent_manage_access", deny)
    args = dict(
        agent_id=agent_id,
        body=files_api.UninstallSkillBody(folder_name="owned"),
        current_user=SimpleNamespace(id=user_id),
        db=SimpleNamespace(),
    )
    with pytest.raises(HTTPException) as error:
        await files_api.uninstall_skill_from_agent(**args)
    assert error.value.status_code == 403 and (workspace / "skills/owned/SKILL.md").exists()

    async def allow(db, user, agent):
        assert agent == agent_id and user.id == user_id

    monkeypatch.setattr(files_api, "require_agent_manage_access", allow)
    assert (await files_api.uninstall_skill_from_agent(**args))["status"] == "uninstalled"
    assert not (workspace / "skills/owned/SKILL.md").exists()
