from __future__ import annotations

import json

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


def test_prepare_custom_api_request_rejects_credential_arguments() -> None:
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
            arguments={"api_key": "sk-user-pasted"},
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
