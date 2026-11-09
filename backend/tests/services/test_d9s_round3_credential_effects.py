"""Small credential-egress regressions from the final D9s review."""

import json
import re
from urllib.parse import quote

import httpx
import pytest

from tests.services.test_d9s_round2_credential_effects import _admin_test


@pytest.mark.parametrize("media_type", [
    "application/problem+json", "application/vnd.api+json", "Application/Problem+Json; charset=utf-8",
])
async def test_json_suffix_media_types_redact_decoded_passwords(monkeypatch, media_type):
    password = 'D9sRound3Password"With\\Escapes012345'

    async def provider(*args, **kwargs):
        return httpx.Response(422, content=json.dumps({"detail": password, "business_value": "preserve this"}),
            headers={"content-type": media_type})

    response = await _admin_test(monkeypatch,
        {"base_url": "https://api.example.test", "auth": {"scheme": "basic"}, "action": {"path": "/read"}},
        {"basic_username": "round3@example.test", "basic_password": password}, provider)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert password not in result and json.dumps(password)[1:-1] not in result
    assert "[REDACTED_SECRET]" in result
    assert "preserve this" in result


async def test_lower_case_percent_hex_echo_is_redacted(monkeypatch):
    secret = "D9sRound3UpperCaseToken/+?#012345"
    encoded = re.sub(r"%[0-9A-F]{2}", lambda match: match[0].lower(), quote(secret, safe=""))

    async def provider(*args, **kwargs):
        return httpx.Response(422, text=f"https://api.example.test/?key={encoded}")

    response = await _admin_test(monkeypatch,
        {"base_url": "https://api.example.test", "auth": {"scheme": "api_key", "in": "query"}, "action": {"path": "/read"}},
        {"api_key": secret}, provider)
    assert response.status_code == 200, response.text
    assert encoded not in response.json()["result"]
    assert "[REDACTED_SECRET]" in response.json()["result"]


async def test_http_mcp_failure_explains_https_configuration(monkeypatch):
    from app.services.mcp_client import MCPClient

    async def unavailable(*args, **kwargs):
        raise Exception("HTTP 401")

    for scheme in ("http", "https"):
        client = MCPClient(f"{scheme}://shared.example/sse")
        monkeypatch.setattr(client, "_streamable_request", unavailable)
        monkeypatch.setattr(client, "_sse_request", unavailable)
        with pytest.raises(Exception) as failure:
            await client.list_tools()
        original = "Both transports failed. Streamable HTTP: HTTP 401; SSE: HTTP 401"
        if scheme == "http":
            assert str(failure.value).startswith(original)
            assert "https://" in str(failure.value)
        else:
            assert str(failure.value) == original


def test_invalid_idna_address_does_not_claim_a_port_error():
    from app.services.custom_api_connectors import CustomApiConnectorError, prepare_custom_api_request

    with pytest.raises(CustomApiConnectorError) as failure:
        prepare_custom_api_request(tool_name="custom_api__round3__read", arguments={}, secret_config={},
            tool_config={"base_url": "https://☃.example", "action": {"path": "/read"}})
    assert "连接地址无效" in str(failure.value)
    assert "端口" not in str(failure.value)
