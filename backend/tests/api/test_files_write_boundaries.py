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
        return None

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)

    for path in ("soul.md", "skills/deploy-checklist/SKILL.md"):
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


def test_file_api_rejects_skill_upload_guard():
    import app.api.files as files_api

    with pytest.raises(HTTPException) as exc_info:
        files_api._raise_upload_path_guard("skills/deploy-checklist", "SKILL.md")

    assert exc_info.value.status_code == 403
    assert "Platform Skill Gate" in str(exc_info.value.detail)
