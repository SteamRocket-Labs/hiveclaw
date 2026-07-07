from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_agent_import_from_url_short_circuits_when_skill_already_exists(tmp_path, monkeypatch):
    import app.api.files as files_api
    import app.api.skills as skills_api

    agent_id = uuid4()
    agent_dir = tmp_path / str(agent_id) / "skills" / "demo-skill"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "SKILL.md").write_text("# Demo", encoding="utf-8")

    monkeypatch.setattr(files_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    async def fake_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)
    monkeypatch.setattr(skills_api, "_parse_github_url", lambda _url: {
        "owner": "demo",
        "repo": "skills",
        "branch": "main",
        "path": "demo-skill",
    })

    async def fail_fetch(*_args, **_kwargs):
        raise AssertionError("_fetch_github_directory should not be called for already installed skills")

    monkeypatch.setattr(skills_api, "_fetch_github_directory", fail_fetch)

    result = await files_api.agent_import_from_url(
        agent_id=agent_id,
        body=files_api.UrlImportBody(url="https://github.com/demo/skills/tree/main/demo-skill"),
        current_user=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        db=SimpleNamespace(),
    )

    assert result["status"] == "already_installed"
    assert result["folder_name"] == "demo-skill"
    assert result["files_written"] == 0


@pytest.mark.asyncio
async def test_agent_import_from_url_stages_review_without_active_install(tmp_path, monkeypatch):
    import app.api.files as files_api
    import app.api.skills as skills_api

    agent_id = uuid4()
    expected_tenant_id = uuid4()
    monkeypatch.setattr(files_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    async def fake_access(*_args, **_kwargs):
        return None

    async def fake_fetch(*_args, **_kwargs):
        return [{"path": "SKILL.md", "content": "# Demo Skill"}]

    async def fake_token(_tenant_id):
        return "token"

    async def fake_stage(db, *, tenant_id, created_by_user_id, source_uri, folder_name, files, source_format):
        assert tenant_id == expected_tenant_id
        assert folder_name == "demo-skill"
        assert files == [{"path": "SKILL.md", "content": "# Demo Skill"}]
        assert source_format == "external_skill_url"
        return {
            "status": "review_required",
            "folder_name": folder_name,
            "files_written": 0,
            "files": [],
            "review_id": "review-1",
            "skill_guard": {"allowed": True},
            "source_uri": source_uri,
        }

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)
    monkeypatch.setattr(skills_api, "_parse_github_url", lambda _url: {
        "owner": "demo",
        "repo": "skills",
        "branch": "main",
        "path": "demo-skill",
    })
    monkeypatch.setattr(skills_api, "_fetch_github_directory", fake_fetch)
    monkeypatch.setattr(skills_api, "_get_github_token", fake_token)
    monkeypatch.setattr(files_api, "stage_external_skill_package_review", fake_stage)

    result = await files_api.agent_import_from_url(
        agent_id=agent_id,
        body=files_api.UrlImportBody(url="https://github.com/demo/skills/tree/main/demo-skill"),
        current_user=SimpleNamespace(id=uuid4(), tenant_id=expected_tenant_id),
        db=SimpleNamespace(),
    )

    assert result["status"] == "review_required"
    assert result["review_id"] == "review-1"
    assert not (tmp_path / str(agent_id) / "skills" / "demo-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_agent_import_from_clawhub_short_circuits_when_skill_already_exists(tmp_path, monkeypatch):
    import app.api.files as files_api

    agent_id = uuid4()
    agent_dir = tmp_path / str(agent_id) / "skills" / "market-research-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "SKILL.md").write_text("# Market Research", encoding="utf-8")

    monkeypatch.setattr(files_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    async def fake_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)

    class _UnexpectedAsyncClient:
        async def __aenter__(self):
            raise AssertionError("ClawHub should not be queried for an already installed workspace skill")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: _UnexpectedAsyncClient())

    result = await files_api.agent_import_from_clawhub(
        agent_id=agent_id,
        body=files_api.ClawhubImportBody(slug="market-research-agent"),
        current_user=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        db=SimpleNamespace(),
    )

    assert result["status"] == "already_installed"
    assert result["folder_name"] == "market-research-agent"
    assert result["files_written"] == 0
