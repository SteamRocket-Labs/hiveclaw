"""Tests for ``_safe_feishu_json`` — the guard that shields tool callers
from malformed Feishu OpenAPI responses (the root cause of the Railway
``JSONDecodeError: Extra data: line 1 column 5 (char 4)`` incident).
"""

import httpx
import pytest

from app.services.agent_tool_domains.feishu_docs import _safe_feishu_json


def _response(body: str, *, status: int = 200, content_type: str = "application/json") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=body.encode("utf-8"),
        headers={"content-type": content_type},
    )


def test_returns_dict_when_body_is_valid_json_object():
    resp = _response('{"code": 0, "data": {"content": "hi"}}')
    data = _safe_feishu_json(resp, "doc_read")
    assert data == {"code": 0, "data": {"content": "hi"}}


def test_returns_error_envelope_on_extra_data_decode_error():
    # Mirrors the actual Railway failure: first 4 chars are a valid JSON value
    # (the number 1234), then trailing junk triggers "Extra data: line 1 column 5 (char 4)".
    resp = _response('1234{"code":0}', status=200)
    data = _safe_feishu_json(resp, "doc_read")
    assert data["code"] == 200
    assert "non-JSON response" in data["msg"]
    assert "1234" in data["msg"]


def test_returns_error_envelope_on_html_error_body_with_5xx():
    resp = _response(
        "<html><body>gateway timeout</body></html>",
        status=502,
        content_type="text/html",
    )
    data = _safe_feishu_json(resp, "doc_append_meta")
    assert data["code"] == 502
    assert "502" in data["msg"]
    assert "<html>" in data["msg"]


def test_returns_error_envelope_when_json_is_not_a_dict():
    # Valid JSON, but shape is wrong (e.g. array). Callers read `.get("code")`,
    # so non-dicts must also be coerced into the envelope shape.
    resp = _response('["a", "b"]')
    data = _safe_feishu_json(resp, "doc_create")
    assert data["code"] == 200
    assert "non-JSON response" in data["msg"]


def test_error_msg_preserves_complete_provider_body_for_kernel_recovery():
    tail = "DECISIVE_FEISHU_PROVIDER_ERROR_TAIL"
    body = ("x" * 5000) + tail
    resp = _response(body, status=500, content_type="text/plain")
    data = _safe_feishu_json(resp, "doc_delete")
    assert data["code"] == 500
    assert body in data["msg"]
    assert tail in data["msg"]


@pytest.mark.asyncio
async def test_wiki_file_route_uses_full_file_content_by_default(monkeypatch: pytest.MonkeyPatch):
    from app.services.agent_tool_domains import feishu_docs, feishu_drive

    async def fake_drive_file_read(agent_id, arguments):
        assert agent_id == "agent-1"
        assert arguments == {"file_token": "file-token", "file_name": "evidence.txt"}
        return "COMPLETE FILE CONTENT"

    monkeypatch.setattr(feishu_drive, "_feishu_drive_file_read", fake_drive_file_read)

    result = await feishu_docs._route_wiki_non_doc_node(
        "agent-1",
        {"obj_type": "file", "obj_token": "file-token", "title": "evidence.txt"},
    )

    assert "COMPLETE FILE CONTENT" in result
