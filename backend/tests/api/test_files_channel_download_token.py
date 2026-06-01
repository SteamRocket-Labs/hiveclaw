from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


def _settings(tmp_path):
    return SimpleNamespace(
        AGENT_DATA_DIR=str(tmp_path),
        BASE_URL="https://backend.example.com",
        PUBLIC_BASE_URL="https://backend.example.com",
        JWT_SECRET_KEY="jwt-secret",
        JWT_ALGORITHM="HS256",
    )


@pytest.mark.asyncio
async def test_channel_file_download_token_allows_external_file_download(tmp_path, monkeypatch) -> None:
    import app.api.files as files_api
    import app.services.file_download_tokens as token_service

    settings = _settings(tmp_path)
    monkeypatch.setattr(files_api, "settings", settings)
    monkeypatch.setattr(token_service, "get_settings", lambda: settings)

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "report.md"
    target.write_text("hello", encoding="utf-8")

    token = token_service.make_channel_file_download_token(
        agent_id=agent_id,
        path="workspace/report.md",
        expires_delta=timedelta(minutes=5),
    )

    response = await files_api.download_file(
        agent_id=agent_id,
        path="workspace/report.md",
        token=token,
        credentials=None,
        db=object(),
    )

    assert response.path == str(target)
    assert response.filename == "report.md"


@pytest.mark.asyncio
async def test_channel_file_download_token_rejects_path_mismatch(tmp_path, monkeypatch) -> None:
    import app.api.files as files_api
    import app.services.file_download_tokens as token_service

    settings = _settings(tmp_path)
    monkeypatch.setattr(files_api, "settings", settings)
    monkeypatch.setattr(token_service, "get_settings", lambda: settings)

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("hello", encoding="utf-8")
    (workspace / "other.md").write_text("other", encoding="utf-8")

    token = token_service.make_channel_file_download_token(
        agent_id=agent_id,
        path="workspace/report.md",
        expires_delta=timedelta(minutes=5),
    )

    with pytest.raises(HTTPException) as exc:
        await files_api.download_file(
            agent_id=agent_id,
            path="workspace/other.md",
            token=token,
            credentials=None,
            db=object(),
        )

    assert exc.value.status_code == 401


def test_build_channel_file_download_url_uses_signed_token_and_quoted_path(tmp_path, monkeypatch) -> None:
    import app.services.file_download_tokens as token_service

    settings = _settings(tmp_path)
    monkeypatch.setattr(token_service, "get_settings", lambda: settings)

    agent_id = uuid4()
    url = token_service.build_channel_file_download_url(
        agent_id=agent_id,
        path="workspace/Serenity_投资观点追踪.md",
        expires_delta=timedelta(minutes=5),
    )

    assert url.startswith(f"https://backend.example.com/api/agents/{agent_id}/files/download?")
    assert "path=workspace%2FSerenity_" in url
    assert "token=" in url
