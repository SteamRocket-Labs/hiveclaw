"""A confirmed office write survives URL failure; ambiguous writes are never retried."""

import json
from uuid import uuid4

import httpx
import pytest

from app.services.agent_tool_domains.feishu_helpers import FeishuAccessToken, FeishuToolAuth
from app.services.feishu_platform import resolve_feishu_platform
from app.services.feishu_read_receipts import capture_feishu_result
from app.tools.result_envelope import ToolContentEnvelope


@pytest.mark.parametrize("mode", ["success", "foreign_metadata", "url_unavailable", "missing_id", "lost_response", "server_error", "unreadable"])
async def test_document_result_uses_matching_metadata_and_preserves_uncertain_effect(monkeypatch, mode):
    from app.services.agent_tool_domains import feishu_docs

    platform = resolve_feishu_platform({"platform_region": "lark_global"})
    async def credentials(_agent):
        return "synthetic-app", FeishuAccessToken("synthetic-token", platform)
    monkeypatch.setattr(feishu_docs, "_get_feishu_token", credentials)
    posts = []
    def provider(request):
        assert request.url.host == "open.larksuite.com"
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        if request.url.path == "/open-apis/docx/v1/documents":
            posts.append(request)
            if mode == "lost_response":
                raise httpx.ReadTimeout("Response lost after provider accepted the write", request=request)
            if mode == "server_error":
                return httpx.Response(503, json={"code": 0, "data": {"document": {"document_id": "created"}}})
            if mode == "unreadable":
                return httpx.Response(200, text="unreadable-response")
            return httpx.Response(200, json={"code": 0, "data": {"document": {} if mode == "missing_id" else {"document_id": "created"}}})
        assert request.url.path.endswith("/metas/batch_query")
        if mode == "url_unavailable":
            return httpx.Response(403, json={"code": 99991672})
        return httpx.Response(200, json={"code": 0, "data": {"metas": [{
            "doc_token": "another-document" if mode == "foreign_metadata" else "created", "doc_type": "docx",
            "url": "https://company.larksuite.com/docx/created"}]}})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
    with capture_feishu_result(None, "feishu_doc_create", {"title": "Office report"}) as capture:
        result = await feishu_docs._feishu_doc_create(uuid4(), {"title": "Office report"})
    assert len(posts) == 1
    assert "bytedance.larkoffice.com" not in str(result)
    if mode in {"missing_id", "lost_response", "server_error", "unreadable"}:
        assert isinstance(result, ToolContentEnvelope)
        assert json.loads(str(result))["status"] == "unknown"
        assert json.loads(str(result))["retryable"] is False
        assert json.loads(str(result))["next_action"] == "reconcile"
        assert capture.api_actions == {}
    else:
        assert "Token：created" in str(result)
        assert bool(capture.api_actions)
        if mode == "success":
            assert "https://company.larksuite.com/docx/created" in str(result)
        else:
            assert "https://company.larksuite.com" not in str(result)
            assert "无需重复执行" in str(result)


async def test_approval_submission_with_lost_response_is_unknown_without_retry(monkeypatch):
    from app.services.agent_tool_domains import feishu_approval

    auth = FeishuToolAuth("synthetic-app", "synthetic-secret", resolve_feishu_platform({"platform_region": "lark_global"}))
    async def credentials(*_args):
        return auth
    monkeypatch.setattr(feishu_approval, "_get_approval_credentials", credentials)
    writes = []
    def provider(request):
        assert request.url.host == "open.larksuite.com"
        if "/auth/v3/" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-token"})
        if request.url.path.endswith("/approvals/code"):
            return httpx.Response(200, json={"code": 0, "data": {"form": "[]"}})
        assert request.url.path == "/open-apis/approval/v4/instances"
        writes.append(request)
        raise httpx.ReadTimeout("Response lost after submission", request=request)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
    result = await feishu_approval._feishu_approval_create(uuid4(), {"approval_code": "code", "user_id": "u_requester", "form": []})
    assert len(writes) == 1
    assert isinstance(result, ToolContentEnvelope) and json.loads(str(result))["status"] == "unknown"
