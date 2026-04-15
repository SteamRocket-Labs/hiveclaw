"""Tests for ``_safe_feishu_json`` — the guard that shields tool callers
from malformed Feishu OpenAPI responses (the root cause of the Railway
``JSONDecodeError: Extra data: line 1 column 5 (char 4)`` incident).
"""

import httpx

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


def test_error_msg_truncates_long_bodies_to_200_chars():
    body = "x" * 5000
    resp = _response(body, status=500, content_type="text/plain")
    data = _safe_feishu_json(resp, "doc_delete")
    assert data["code"] == 500
    # msg contains a 200-char snippet (surrounded by framing text), never the full body.
    assert "x" * 200 in data["msg"]
    assert "x" * 201 not in data["msg"]
