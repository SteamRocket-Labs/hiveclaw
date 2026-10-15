from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest


def test_custom_api_tool_name_slugifies_connector_and_action() -> None:
    from app.services.custom_api_connectors import build_custom_api_tool_name

    assert build_custom_api_tool_name("AlphaGBM 美股", "Analyze Stock Sync") == (
        "custom_api__alphagbm__analyze_stock_sync"
    )


def test_prepare_custom_api_request_injects_secret_server_side_and_redacts_audit() -> None:
    from app.services.custom_api_connectors import prepare_custom_api_request

    request = prepare_custom_api_request(
        tool_name="custom_api__alphagbm__analyze",
        tool_config={
            "base_url": "https://api.example.com",
            "auth": {"scheme": "api_key", "in": "header", "name": "X-API-Key"},
            "action": {
                "method": "POST",
                "path": "/api/stock/{symbol}/analyze-sync",
                "headers": {"X-Client": "Hive"},
                "query": {"region": "{region}"},
                "body": {"symbol": "{symbol}", "period": "{period}"},
                "timeout_seconds": 12,
            },
        },
        secret_config={"api_key": "sk-live-secret"},
        arguments={"symbol": "AAOI", "period": "Q2", "region": "US"},
    )

    assert request.method == "POST"
    assert request.url == "https://api.example.com/api/stock/AAOI/analyze-sync"
    assert request.headers["X-API-Key"] == "sk-live-secret"
    assert request.headers["X-Client"] == "Hive"
    assert request.params == {"region": "US"}
    assert request.json_body == {"symbol": "AAOI", "period": "Q2"}
    assert request.timeout_seconds == 12
    assert "sk-live-secret" not in json.dumps(request.audit, ensure_ascii=False)


@pytest.mark.parametrize("argument", ["api_key", "approved_networks", "network_grant"])
def test_prepare_custom_api_request_rejects_credential_arguments(argument: str) -> None:
    from app.services.custom_api_connectors import CustomApiConnectorError, prepare_custom_api_request

    with pytest.raises(CustomApiConnectorError, match="credentials must be configured by an admin"):
        prepare_custom_api_request(
            tool_name="custom_api__bad__leak",
            tool_config={
                "base_url": "https://api.example.com",
                "auth": {"scheme": "api_key", "in": "header", "name": "X-API-Key"},
                "action": {"method": "GET", "path": "/v1/things"},
            },
            secret_config={},
            arguments={argument: "sk-user-pasted"},
        )


def test_build_custom_api_tool_payload_keeps_secret_out_of_tool_config() -> None:
    from app.services.custom_api_connectors import build_custom_api_tool_payload

    payload = build_custom_api_tool_payload(
        connector_name="AlphaGBM",
        action_name="Analyze Stock",
        description="Analyze one stock through AlphaGBM",
        base_url="https://api.alphagbm.com",
        method="POST",
        path="/api/stock/analyze-sync",
        auth_scheme="api_key",
        auth_location="header",
        auth_name="ALPHAGBMAPKEY",
        parameters_schema={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
        secret_value="agb_123456",
        body_template={"symbol": "{symbol}"},
    )

    assert payload.tool_name == "custom_api__alphagbm__analyze_stock"
    assert payload.tool_config["auth"]["name"] == "ALPHAGBMAPKEY"
    assert "agb_123456" not in json.dumps(payload.tool_config, ensure_ascii=False)
    assert payload.secret_config == {"api_key": "agb_123456"}
    assert payload.config_schema["fields"][0]["type"] == "password"


@contextmanager
def _local_http(status: int, body: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _local_prepared(port: int):
    from app.services.custom_api_connectors import prepare_custom_api_request

    return prepare_custom_api_request(
        tool_name="custom_api__example__read",
        tool_config={
            "base_url": f"http://127.0.0.1:{port}",
            "action": {"method": "GET", "path": "/v1/read"},
            "approved_networks": ["127.0.0.1/32"],
        },
        secret_config={},
        arguments={},
    )


@pytest.mark.asyncio
async def test_custom_api_success_preserves_complete_response_body() -> None:
    from app.services.custom_api_connectors import execute_prepared_custom_api_request

    tail = "CUSTOM_API_DECISIVE_TAIL"
    with _local_http(200, ("b" * 21000 + tail).encode()) as port:
        result = await execute_prepared_custom_api_request("custom_api__example__read", _local_prepared(port))

    assert tail in result
    assert "...[truncated]" not in result


@pytest.mark.asyncio
async def test_custom_api_http_error_preserves_complete_error_evidence() -> None:
    from app.services.custom_api_connectors import execute_prepared_custom_api_request

    tail = "CUSTOM_API_ERROR_DECISIVE_TAIL"
    with _local_http(422, ("e" * 900 + tail).encode()) as port:
        result = await execute_prepared_custom_api_request("custom_api__example__read", _local_prepared(port))

    assert tail in result


def test_network_grant_stays_on_the_connector_and_rejects_a_bad_cidr() -> None:
    from app.services.custom_api_connectors import (
        CustomApiConnectorError,
        merge_preserved_network_grant,
        set_connector_network_grant,
    )

    tool = SimpleNamespace(config={"base_url": "https://api.example.com", "approved_networks": ["10.0.0.0/8"]})
    merged = merge_preserved_network_grant(
        {"base_url": "https://api.example.com", "approved_networks": ["0.0.0.0/0"]},
        tool.config,
    )
    assert merged["approved_networks"] == ["10.0.0.0/8"]
    with pytest.raises(CustomApiConnectorError, match="save again"):
        set_connector_network_grant(tool, ["not-a-cidr"])
    assert tool.config["approved_networks"] == ["10.0.0.0/8"]
    assert set_connector_network_grant(tool, ["10.8.0.0/16", "10.8.0.0/16"]) == ["10.8.0.0/16"]
    assert set_connector_network_grant(tool, []) == []
    assert "approved_networks" not in tool.config
