from __future__ import annotations

import json

import httpx

from hive_bridge.client import HiveBridgeClient


def test_client_uses_bridge_bearer_for_gateway_poll() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"messages": [], "relationships": []})

    client = HiveBridgeClient(
        base_url="https://hive.example",
        token="hb_test",
        transport=httpx.MockTransport(handler),
    )

    result = client.poll_inbox()

    assert result == {"messages": [], "relationships": []}
    assert seen == {
        "method": "GET",
        "url": "https://hive.example/api/v1/gateway/poll",
        "authorization": "Bearer hb_test",
    }


def test_client_uploads_file_as_multipart(tmp_path) -> None:
    file_path = tmp_path / "report.md"
    file_path.write_text("# Report\n", encoding="utf-8")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = body.decode("utf-8", errors="replace")
        return httpx.Response(200, json={"workspace_path": "workspace/uploads/report.md"})

    client = HiveBridgeClient(
        base_url="https://hive.example",
        token="hb_test",
        transport=httpx.MockTransport(handler),
    )

    result = client.upload_file(file_path)

    assert result["workspace_path"] == "workspace/uploads/report.md"
    assert seen["method"] == "POST"
    assert seen["url"] == "https://hive.example/api/v1/local-bridge/upload"
    assert seen["authorization"] == "Bearer hb_test"
    assert seen["content_type"].startswith("multipart/form-data; boundary=")
    assert 'name="file"; filename="report.md"' in seen["body"]
    assert "# Report" in seen["body"]


def test_client_downloads_channel_file_to_destination(tmp_path) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"# Cloud Brief\n")

    client = HiveBridgeClient(
        base_url="https://hive.example",
        token="hb_test",
        transport=httpx.MockTransport(handler),
    )

    result = client.download_channel_file("workspace/uploads/cloud-brief.md", tmp_path / "downloads")

    saved_path = tmp_path / "downloads" / "cloud-brief.md"
    assert result == {
        "path": str(saved_path),
        "source_path": "workspace/uploads/cloud-brief.md",
        "size": len(b"# Cloud Brief\n"),
    }
    assert saved_path.read_bytes() == b"# Cloud Brief\n"
    assert seen == {
        "method": "GET",
        "url": "https://hive.example/api/v1/local-bridge/channel/workspace/download?path=workspace%2Fuploads%2Fcloud-brief.md",
        "authorization": "Bearer hb_test",
    }


def test_pairing_exchange_pending_and_active() -> None:
    responses = [
        httpx.Response(200, json={"status": "pending", "interval": 3}),
        httpx.Response(200, json={"status": "active", "access_token": "hb_live", "token_type": "Bearer"}),
    ]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.read().decode("utf-8")))
        return responses.pop(0)

    client = HiveBridgeClient(base_url="https://hive.example", transport=httpx.MockTransport(handler))

    assert client.exchange_pairing("dev_code")["status"] == "pending"
    assert client.exchange_pairing("dev_code")["access_token"] == "hb_live"
    assert calls == [{"device_code": "dev_code"}, {"device_code": "dev_code"}]


def test_client_reports_result_with_attachments_and_metadata() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"status": "ok"})

    client = HiveBridgeClient(
        base_url="https://hive.example",
        token="hb_test",
        transport=httpx.MockTransport(handler),
    )

    result = client.report_result(
        message_id="msg-1",
        result="done",
        attachments=[{"path": "workspace/local-bridge/report.md"}],
        metadata={"runtime": "command", "exit_code": 0},
    )

    assert result == {"status": "ok"}
    assert seen == {
        "method": "POST",
        "url": "https://hive.example/api/v1/gateway/report",
        "authorization": "Bearer hb_test",
        "json": {
            "message_id": "msg-1",
            "result": "done",
            "attachments": [{"path": "workspace/local-bridge/report.md"}],
            "metadata": {"runtime": "command", "exit_code": 0},
        },
    }


def test_client_creates_channel_ws_ticket_and_builds_ws_url() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(201, json={"ticket": "hbt_secret", "expires_in": 60, "single_use": True})

    client = HiveBridgeClient(
        base_url="https://hive.example",
        token="hb_test",
        transport=httpx.MockTransport(handler),
    )

    ticket = client.create_channel_ws_ticket()

    assert ticket == {"ticket": "hbt_secret", "expires_in": 60, "single_use": True}
    assert client.channel_ws_url("hbt_secret") == "wss://hive.example/api/v1/local-bridge/channel/ws?ticket=hbt_secret"
    assert seen == {
        "method": "POST",
        "url": "https://hive.example/api/v1/local-bridge/channel/ws-ticket",
        "authorization": "Bearer hb_test",
    }


def test_client_reports_local_agent_channel_result() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"status": "completed"})

    client = HiveBridgeClient(
        base_url="http://localhost:8008",
        token="hb_test",
        transport=httpx.MockTransport(handler),
    )

    result = client.report_channel_result(
        session_id="session-1",
        message_id="message-1",
        output="done over channel",
        artifacts=[{"path": "workspace/local/done.md"}],
        metadata={"runtime": "command"},
    )

    assert result == {"status": "completed"}
    assert seen == {
        "method": "POST",
        "url": "http://localhost:8008/api/v1/local-bridge/channel/report",
        "authorization": "Bearer hb_test",
        "json": {
            "session_id": "session-1",
            "message_id": "message-1",
            "status": "completed",
            "output": "done over channel",
            "artifacts": [{"path": "workspace/local/done.md"}],
            "metadata": {"runtime": "command"},
        },
    }
