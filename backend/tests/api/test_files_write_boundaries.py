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


@pytest.mark.parametrize(
    "path",
    [
        "workspace/recovery_manifest.json",
        "workspace/recovery_manifest.json/payload.json",
        "workspace/session_memory.md",
        "workspace/compaction_summary.md",
    ],
)
def test_file_api_guard_rejects_platform_private_state(path):
    import app.api.files as files_api

    with pytest.raises(HTTPException) as exc_info:
        files_api._raise_managed_path_write_guard(path)

    assert exc_info.value.status_code == 403


def test_file_upload_guard_rejects_legacy_recovery_manifest():
    import app.api.files as files_api

    with pytest.raises(HTTPException) as exc_info:
        files_api._raise_upload_path_guard("workspace/", "recovery_manifest.json")

    assert exc_info.value.status_code == 403


def test_file_api_safe_path_rejects_symlink_to_private_state(tmp_path, monkeypatch):
    import app.api.files as files_api

    agent_id = uuid4()
    monkeypatch.setattr(files_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    private = tmp_path / str(agent_id) / "workspace" / "recovery_manifest.json"
    private.parent.mkdir(parents=True)
    private.write_text("trusted", encoding="utf-8")
    (private.parent / "alias.json").symlink_to(private.name)

    with pytest.raises(HTTPException) as exc_info:
        files_api._safe_path(agent_id, "workspace/alias.json")

    assert exc_info.value.status_code == 403
    assert private.read_text(encoding="utf-8") == "trusted"
