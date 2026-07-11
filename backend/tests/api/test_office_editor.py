from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi import HTTPException
from jose import jwt


def _settings(tmp_path, *, secret: str = "onlyoffice-secret", token_seconds: int = 300):
    return SimpleNamespace(
        AGENT_DATA_DIR=str(tmp_path),
        BASE_URL="https://hive.example.com",
        PUBLIC_BASE_URL="https://hive.example.com",
        ONLYOFFICE_DOCS_URL="https://docs.example.com",
        ONLYOFFICE_INTERNAL_DOCS_URL="https://docs.internal",
        ONLYOFFICE_JWT_SECRET=secret,
        ONLYOFFICE_DOWNLOAD_TOKEN_EXPIRE_SECONDS=token_seconds,
        JWT_SECRET_KEY="jwt-secret",
        JWT_ALGORITHM="HS256",
    )


@pytest.fixture(autouse=True)
def _office_resource_authority_stubs(tmp_path, monkeypatch):
    import app.api.office as office_api

    tenant_id = uuid4()

    async def fake_access(_db, _user, agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    async def fake_authorize(_db, user, **_kwargs):
        return SimpleNamespace(
            owner_user_id=user.id,
            root_session_id=None,
            authority_source="resource_owner",
            operator_view=False,
            manifest=SimpleNamespace(),
        )

    async def fake_register(*_args, **_kwargs):
        return None

    @asynccontextmanager
    async def fake_token_authority(*, agent_id, path, payload, expected_action):
        assert payload["authority_action"] == expected_action
        user = SimpleNamespace(id=uuid4())
        decision = SimpleNamespace(
            owner_user_id=user.id,
            root_session_id=None,
            authority_source="resource_owner",
            operator_view=False,
        )
        yield {
            "db": SimpleNamespace(),
            "user": user,
            "agent": SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            "decision": decision,
            "target": tmp_path / str(agent_id) / path,
            "path": path,
        }

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)
    monkeypatch.setattr(office_api, "authorize_workspace_path", fake_authorize)
    monkeypatch.setattr(office_api, "register_workspace_path", fake_register)
    monkeypatch.setattr(office_api, "_authorize_office_token_payload", fake_token_authority)


@pytest.mark.asyncio
async def test_office_editor_config_contains_onlyoffice_jwt(tmp_path, monkeypatch):
    import app.api.office as office_api

    monkeypatch.setattr(office_api, "settings", _settings(tmp_path))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)

    agent_id = uuid4()
    user_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "demo.docx").write_bytes(b"docx")

    response = await office_api.get_editor_config(
        agent_id=agent_id,
        path="workspace/demo.docx",
        mode="edit",
        current_user=SimpleNamespace(id=user_id, name="Rocky"),
        db=SimpleNamespace(),
    )

    assert response["enabled"] is True
    assert response["documentServerUrl"] == "https://docs.example.com"
    config = response["config"]
    assert config["document"]["fileType"] == "docx"
    assert config["document"]["url"].startswith("https://hive.example.com/api/agents/")
    assert config["editorConfig"]["callbackUrl"].startswith("https://hive.example.com/api/agents/")
    assert config["editorConfig"]["customization"]["forcesave"] is True
    decoded = jwt.decode(config["token"], "onlyoffice-secret", algorithms=["HS256"])
    assert decoded["document"]["key"] == config["document"]["key"]
    download_token = parse_qs(urlparse(config["document"]["url"]).query)["token"][0]
    callback_token = parse_qs(urlparse(config["editorConfig"]["callbackUrl"]).query)["token"][0]
    download_claims = jwt.decode(download_token, "onlyoffice-secret", algorithms=["HS256"])
    callback_claims = jwt.decode(callback_token, "onlyoffice-secret", algorithms=["HS256"])
    assert callback_claims["purpose"] == "callback"
    assert callback_claims["exp"] > download_claims["exp"]


@pytest.mark.asyncio
async def test_office_editor_config_uses_tenant_scoped_non_email_identity(tmp_path, monkeypatch):
    import app.api.office as office_api

    monkeypatch.setattr(office_api, "settings", _settings(tmp_path))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "demo.docx").write_bytes(b"docx")

    response = await office_api.get_editor_config(
        agent_id=agent_id,
        path="workspace/demo.docx",
        mode="edit",
        current_user=SimpleNamespace(
            id=user_id,
            tenant_id=tenant_id,
            display_name="",
            username="tenant-admin",
            email="lurocky14@gmail.com",
        ),
        db=SimpleNamespace(),
    )

    editor_user = response["config"]["editorConfig"]["user"]
    assert editor_user == {"id": f"{tenant_id}:{user_id}", "name": "tenant-admin"}
    assert "lurocky14@gmail.com" not in str(response["config"])
    decoded = jwt.decode(response["config"]["token"], "onlyoffice-secret", algorithms=["HS256"])
    assert decoded["editorConfig"]["user"] == editor_user

    active_session = office_api.OfficeDocumentService(tmp_path / str(agent_id)).get_active_editor_session(
        "workspace/demo.docx"
    )
    assert active_session["user_id"] == editor_user["id"]


@pytest.mark.asyncio
async def test_office_download_rejects_expired_token(tmp_path, monkeypatch):
    import app.api.office as office_api

    monkeypatch.setattr(office_api, "settings", _settings(tmp_path))

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "demo.docx").write_bytes(b"docx")
    token = office_api.make_document_token(
        agent_id=agent_id,
        path="workspace/demo.docx",
        purpose="download",
        user_id=uuid4(),
        expires_delta=timedelta(seconds=-1),
    )

    with pytest.raises(HTTPException) as exc:
        await office_api.download_document(
            agent_id=agent_id,
            path="workspace/demo.docx",
            token=token,
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_office_callback_status_2_saves_downloaded_file(tmp_path, monkeypatch):
    import app.api.office as office_api

    monkeypatch.setattr(office_api, "settings", _settings(tmp_path))

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "demo.docx"
    target.write_bytes(b"old")
    token = office_api.make_document_token(
        agent_id=agent_id,
        path="workspace/demo.docx",
        purpose="callback",
        user_id=uuid4(),
    )

    class _Response:
        content = b"new-from-onlyoffice"

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            assert url == "https://docs.internal/download/demo.docx"
            return _Response()

    monkeypatch.setattr(office_api.httpx, "AsyncClient", lambda *args, **kwargs: _Client())

    result = await office_api.onlyoffice_callback(
        agent_id=agent_id,
        path="workspace/demo.docx",
        token=token,
        payload=office_api.OnlyOfficeCallback(status=2, url="https://docs.example.com/download/demo.docx"),
    )

    assert result == {"error": 0}
    assert target.read_bytes() == b"new-from-onlyoffice"


@pytest.mark.asyncio
async def test_office_callback_status_4_closes_active_session(tmp_path, monkeypatch):
    import app.api.office as office_api
    from app.services.office_document_service import OfficeDocumentService

    monkeypatch.setattr(office_api, "settings", _settings(tmp_path))

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    (workspace / "workspace").mkdir(parents=True)
    (workspace / "workspace" / "demo.docx").write_bytes(b"docx")
    service = OfficeDocumentService(workspace)
    service.set_active_editor_session("workspace/demo.docx", session_id="session-1", user_id="user-1")
    token = office_api.make_document_token(
        agent_id=agent_id,
        path="workspace/demo.docx",
        purpose="callback",
        user_id=uuid4(),
    )

    result = await office_api.onlyoffice_callback(
        agent_id=agent_id,
        path="workspace/demo.docx",
        token=token,
        payload=office_api.OnlyOfficeCallback(status=4),
    )

    assert result == {"error": 0}
    assert service.get_active_editor_session("workspace/demo.docx") is None


@pytest.mark.asyncio
async def test_office_callback_error_status_records_audit_event(tmp_path, monkeypatch):
    import app.api.office as office_api

    monkeypatch.setattr(office_api, "settings", _settings(tmp_path))
    events = []

    async def fake_record_event(**payload):
        events.append(payload)

    monkeypatch.setattr(office_api, "record_office_callback_event", fake_record_event)

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "demo.docx").write_bytes(b"docx")
    token = office_api.make_document_token(
        agent_id=agent_id,
        path="workspace/demo.docx",
        purpose="callback",
        user_id=uuid4(),
    )

    result = await office_api.onlyoffice_callback(
        agent_id=agent_id,
        path="workspace/demo.docx",
        token=token,
        payload=office_api.OnlyOfficeCallback(status=3, error=7),
    )

    assert result == {"error": 0}
    assert events == [
        {
            "agent_id": agent_id,
            "path": "workspace/demo.docx",
            "status": 3,
            "error": 7,
        }
    ]


@pytest.mark.asyncio
async def test_office_force_save_posts_signed_command_to_document_server(tmp_path, monkeypatch):
    import app.api.office as office_api

    monkeypatch.setattr(office_api, "settings", _settings(tmp_path))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=uuid4()), "use"

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)

    agent_id = uuid4()
    user_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "demo.docx").write_bytes(b"docx")

    editor_config = await office_api.get_editor_config(
        agent_id=agent_id,
        path="workspace/demo.docx",
        mode="edit",
        current_user=SimpleNamespace(id=user_id, name="Rocky"),
        db=SimpleNamespace(),
    )
    document_key = editor_config["config"]["document"]["key"]
    posted = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"error": 0, "key": document_key}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            posted["url"] = url
            posted["json"] = json
            return _Response()

    monkeypatch.setattr(office_api.httpx, "AsyncClient", lambda *args, **kwargs: _Client())

    result = await office_api.force_save_document(
        agent_id=agent_id,
        body=office_api.OfficeForceSaveIn(path="workspace/demo.docx", userdata="manual-save"),
        current_user=SimpleNamespace(id=user_id, name="Rocky"),
        db=SimpleNamespace(),
    )

    assert result == {"status": "ok", "result": {"error": 0, "key": document_key}}
    assert posted["url"] == f"https://docs.internal/command?shardkey={document_key}"
    assert set(posted["json"]) == {"token"}
    decoded = jwt.decode(posted["json"]["token"], "onlyoffice-secret", algorithms=["HS256"])
    assert decoded == {"c": "forcesave", "key": document_key, "userdata": "manual-save"}
