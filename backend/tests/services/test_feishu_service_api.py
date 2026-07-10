from __future__ import annotations

import pytest


class _FakeResponse:
    def __init__(self, *, status_code: int, payload: dict | None = None, json_error: Exception | None = None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)

    async def patch(self, url: str, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return self._responses.pop(0)

    async def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_get_tenant_access_token_prefers_tenant_token(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient(
        [
            _FakeResponse(
                status_code=200, payload={"tenant_access_token": "tenant-token", "app_access_token": "app-token"}
            ),
        ]
    )
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()
    token = await service.get_tenant_access_token("tenant-app", "tenant-secret")

    assert token == "tenant-token"
    assert fake_client.calls[0][0] == "POST"
    assert fake_client.calls[0][2]["json"] == {"app_id": "tenant-app", "app_secret": "tenant-secret"}


@pytest.mark.asyncio
async def test_send_message_raises_with_stage_on_invalid_json(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient(
        [
            _FakeResponse(status_code=200, payload={"tenant_access_token": "tenant-token"}),
            _FakeResponse(status_code=200, json_error=ValueError("not json")),
        ]
    )
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()

    with pytest.raises(RuntimeError, match="send_text"):
        await service.send_message(
            "tenant-app",
            "tenant-secret",
            "ou_xxx",
            "text",
            '{"text":"hi"}',
            receive_id_type="open_id",
            stage="send_text",
        )


@pytest.mark.asyncio
async def test_send_approval_card_uses_delivery_id_type_and_agent_name(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient(
        [
            _FakeResponse(status_code=200, payload={"tenant_access_token": "tenant-token"}),
            _FakeResponse(status_code=200, payload={"code": 0, "data": {"message_id": "om_1"}}),
        ]
    )
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()
    await service.send_approval_card(
        "tenant-app",
        "tenant-secret",
        "ou_creator",
        "open_id",
        "Alisa 2",
        "workspace.command.secret_exfiltration",
        "{}",
        "approval-id",
    )

    send_call = fake_client.calls[1]
    assert "receive_id_type=open_id" in send_call[1]
    assert send_call[2]["json"]["receive_id"] == "ou_creator"
    assert "Alisa 2" in send_call[2]["json"]["content"]


@pytest.mark.asyncio
async def test_create_approval_instance_uses_user_id_field_for_tenant_user_ids(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient(
        [
            _FakeResponse(status_code=200, payload={"tenant_access_token": "tenant-token"}),
            _FakeResponse(status_code=200, payload={"code": 0, "data": {"instance_code": "ins_user"}}),
        ]
    )
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()
    payload = await service.create_approval_instance(
        "tenant-app",
        "tenant-secret",
        "approval-code",
        "u_submitter",
        '{"foo":"bar"}',
    )

    assert payload == {"instance_code": "ins_user"}
    create_call = fake_client.calls[1]
    assert create_call[2]["json"] == {
        "approval_code": "approval-code",
        "user_id": "u_submitter",
        "form": '{"foo":"bar"}',
    }


@pytest.mark.asyncio
async def test_create_approval_instance_keeps_open_id_field_for_open_ids(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient(
        [
            _FakeResponse(status_code=200, payload={"tenant_access_token": "tenant-token"}),
            _FakeResponse(status_code=200, payload={"code": 0, "data": {"instance_code": "ins_open"}}),
        ]
    )
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()
    payload = await service.create_approval_instance(
        "tenant-app",
        "tenant-secret",
        "approval-code",
        "ou_submitter",
        '{"foo":"bar"}',
    )

    assert payload == {"instance_code": "ins_open"}
    create_call = fake_client.calls[1]
    assert create_call[2]["json"] == {
        "approval_code": "approval-code",
        "open_id": "ou_submitter",
        "form": '{"foo":"bar"}',
    }


@pytest.mark.asyncio
async def test_get_approval_definition_fetches_form_schema(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient(
        [
            _FakeResponse(status_code=200, payload={"tenant_access_token": "tenant-token"}),
            _FakeResponse(
                status_code=200,
                payload={
                    "code": 0,
                    "data": {
                        "approval_code": "approval-code",
                        "form": {
                            "form_content": (
                                '[{"id":"widget_project","name":"项目名称","type":"input","required":true}]'
                            )
                        },
                    },
                },
            ),
        ]
    )
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()
    payload = await service.get_approval_definition("tenant-app", "tenant-secret", "approval-code")

    assert payload["approval_code"] == "approval-code"
    get_call = fake_client.calls[1]
    assert get_call[0] == "GET"
    assert get_call[1] == "https://open.feishu.cn/open-apis/approval/v4/approvals/approval-code"
    assert get_call[2]["headers"] == {"Authorization": "Bearer tenant-token"}


@pytest.mark.asyncio
async def test_patch_message_raises_with_stage_on_business_error(monkeypatch):
    from app.services.feishu_service import FeishuService

    fake_client = _FakeAsyncClient(
        [
            _FakeResponse(status_code=200, payload={"tenant_access_token": "tenant-token"}),
            _FakeResponse(status_code=200, payload={"code": 999001, "msg": "rate limited"}),
        ]
    )
    monkeypatch.setattr("app.services.feishu_service.httpx.AsyncClient", lambda *args, **kwargs: fake_client)

    service = FeishuService()

    with pytest.raises(RuntimeError, match="stream_patch"):
        await service.patch_message(
            "tenant-app",
            "tenant-secret",
            "om_dc132",
            '{"content":"..."}',
            stage="stream_patch",
        )
