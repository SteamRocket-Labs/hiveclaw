from __future__ import annotations

import pytest


def test_slack_api_url_defaults_to_official_endpoint(monkeypatch) -> None:
    from app.api.slack import _slack_api_url

    monkeypatch.delenv("SLACK_API_BASE_URL", raising=False)
    assert _slack_api_url("chat.postMessage") == "https://slack.com/api/chat.postMessage"


def test_slack_api_url_supports_governed_compatible_gateway(monkeypatch) -> None:
    from app.api.slack import _slack_api_url

    monkeypatch.setenv("SLACK_API_BASE_URL", "http://127.0.0.1:18010/slack/api/")
    assert _slack_api_url("users.info") == "http://127.0.0.1:18010/slack/api/users.info"


def test_slack_api_url_rejects_credentials_and_non_http_schemes(monkeypatch) -> None:
    from app.api.slack import _slack_api_url

    monkeypatch.setenv("SLACK_API_BASE_URL", "https://token@slack-proxy.example/api")
    with pytest.raises(RuntimeError, match="userinfo"):
        _slack_api_url("chat.postMessage")

    monkeypatch.setenv("SLACK_API_BASE_URL", "file:///tmp/slack")
    with pytest.raises(RuntimeError, match="http"):
        _slack_api_url("chat.postMessage")
