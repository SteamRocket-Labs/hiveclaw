"""Concrete office API rejection is a failure, never confirmed business data."""

import json
from uuid import uuid4

import httpx
import pytest

from app.services.agent_tool_domains.feishu_helpers import FeishuToolAuth
from app.services.feishu_platform import resolve_feishu_platform
from app.tools.result_envelope import ToolContentEnvelope


@pytest.mark.parametrize(("tool_name", "stage", "arguments"), [
    ("feishu_doc_read", "read", {"document_token": "resource"}),
    ("feishu_doc_create", "create", {"title": "Report"}),
    ("feishu_doc_append", "metadata", {"document_token": "resource", "content": "Report"}),
    ("feishu_doc_append", "append", {"document_token": "resource", "content": "Report"}),
    ("feishu_approval_create", "approval", {"approval_code": "definition", "user_id": "requester", "form": []}),
    ("feishu_approval_query", "query", {"approval_code": "definition"}),
    ("feishu_approval_definition", "definition", {"approval_code": "definition"}),
    ("feishu_approval_get", "instance", {"instance_id": "instance"}),
])
@pytest.mark.parametrize("http_status", [200, 403])
async def test_office_rejection_preserves_protocol_facts(monkeypatch, tool_name, stage, arguments, http_status):
    from app.services.agent_tool_domains import feishu_approval, feishu_docs, feishu_helpers

    auth = FeishuToolAuth("synthetic-app", "synthetic-secret", resolve_feishu_platform())
    async def current_auth(_agent):
        return auth
    monkeypatch.setattr(feishu_helpers, "_get_feishu_tool_auth", current_auth)
    monkeypatch.setattr(feishu_approval, "_get_feishu_tool_auth", current_auth)
    mutations = []
    def provider(request):
        assert request.url.host == "open.feishu.cn"
        if "/auth/v3/" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-token"})
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        if request.url.path.endswith("/spaces/get_node"):
            return httpx.Response(200, json={"code": 131004})
        if stage == "append" and request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"document": {"document_id": "resource"}}})
        if request.method == "POST" and stage in {"create", "append", "approval"}:
            mutations.append(request)
        return httpx.Response(http_status, json={"code": 90300, "msg": "PROVIDER_DIAGNOSTIC"})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
    module = feishu_docs if tool_name.startswith("feishu_doc_") else feishu_approval
    function = getattr(module, "_" + tool_name + ("_via_openapi" if tool_name == "feishu_doc_read" else ""))
    result = await function(uuid4(), arguments)
    assert isinstance(result, ToolContentEnvelope)
    facts = result.metadata["feishu_failure"]
    assert facts["tool_name"] == tool_name and facts["http_status"] == http_status
    assert facts["provider_code"] == 90300
    assert facts["error_class"] == ("auth_or_permission" if http_status == 403 else "provider_error")
    assert facts["next_action"] == ("review_permissions" if http_status == 403 else "review_provider_error")
    assert "PROVIDER_DIAGNOSTIC" in str(result) and "PROVIDER_DIAGNOSTIC" not in json.dumps(facts)
    assert len(mutations) == (1 if stage in {"create", "append", "approval"} else 0)


@pytest.mark.parametrize(("status", "body"), [
    (401, {"code": 90301, "tenant_access_token": "rejected-token"}),
    (200, {"code": 90301, "tenant_access_token": "rejected-token"}),
    (200, {"code": 0, "tenant_access_token": {"invalid": "token"}}),
    (200, []),
])
async def test_token_probe_cannot_turn_rejected_or_malformed_response_into_success(monkeypatch, status, body):
    from app.services.agent_tool_domains.feishu_helpers import _request_feishu_tenant_access_token

    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(status, json=body)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
    result = await _request_feishu_tenant_access_token("synthetic-app", "synthetic-secret")
    assert result["configured"] is True and result["ok"] is False and "token" not in result
    assert len(calls) == 1


def test_original_work_failure_projection_keeps_only_bound_machine_facts():
    import hashlib
    from app.services.approval_execution_runtime import approval_notification_summary
    from app.services.feishu_platform import feishu_tool_error

    result = feishu_tool_error("feishu_doc_create", message="PRIVATE_PROVIDER_BODY", http_status=403, code=90302)
    facts = result.metadata["feishu_failure"]
    receipt = {"status": "failed", "tool_name": "feishu_doc_create", "error": {**facts}, "runtime_evidence": {
        "feishu_failure": facts, "tool_execution_frame": {"tool_name": "feishu_doc_create", "status": "failed",
            "output_hash": hashlib.sha256(json.dumps(str(result), ensure_ascii=False).encode()).hexdigest()}}}
    summary = approval_notification_summary("failed", str(result), receipt)
    assert "90302" in summary and "review_permissions" in summary and "PRIVATE_PROVIDER_BODY" not in summary
    assert "90302" not in approval_notification_summary("failed", "different result", receipt)
    receipt["error"] = {}
    assert "90302" not in approval_notification_summary("failed", str(result), receipt)
