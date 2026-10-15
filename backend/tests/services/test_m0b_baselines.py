"""M0-B ingress, governed custom-API send, and activation seams."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_custom_api_send_reaches_loopback_without_admin_grant():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from app.services.custom_api_connectors import (
        execute_prepared_custom_api_request,
        prepare_custom_api_request,
    )

    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"loopback-reached"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        prepared = prepare_custom_api_request(
            tool_name="custom_api__internal__read",
            tool_config={"base_url": f"http://127.0.0.1:{port}", "action": {"method": "GET", "path": "/secret"}},
            secret_config={},
            arguments={},
        )
        result = await execute_prepared_custom_api_request("custom_api__internal__read", prepared)
    finally:
        server.shutdown()
        server.server_close()

    assert hits == []
    assert "custom_api_target_denied" in result


@pytest.mark.asyncio
async def test_custom_api_grant_reaches_loopback_and_strips_redirected_secrets():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from app.services.custom_api_connectors import (
        execute_prepared_custom_api_request,
        prepare_custom_api_request,
    )

    seen: list[tuple[str, str | None, str | None]] = []
    ports: dict[str, int] = {}

    class Land(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(
                (
                    "land",
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("X-Trace"),
                    self.headers.get("X-API-Key"),
                )
            )
            body = b"landed"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Trace", self.headers.get("X-Trace", ""))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    class Start(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("start", self.path, self.headers.get("X-API-Key"), None))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{ports['land']}/land?token=sk-live-secret")
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    land = ThreadingHTTPServer(("127.0.0.1", 0), Land)
    start = ThreadingHTTPServer(("127.0.0.1", 0), Start)
    ports["land"] = land.server_address[1]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (land, start)]
    for thread in threads:
        thread.start()
    try:
        prepared = prepare_custom_api_request(
            tool_name="custom_api__internal__read",
            tool_config={
                "base_url": f"http://127.0.0.1:{start.server_address[1]}",
                "auth": {"scheme": "api_key", "in": "header", "name": "X-API-Key"},
                "action": {"method": "GET", "path": "/secret", "headers": {"X-Trace": "hive"}},
                "approved_networks": ["127.0.0.1/32"],
            },
            secret_config={"api_key": "sk-live-secret"},
            arguments={},
        )
        result = await execute_prepared_custom_api_request("custom_api__internal__read", prepared)
        for url in (
            "http://2130706433/secret",
            "http://169.254.169.254/latest",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://[::ffff:127.0.0.1]/secret",
        ):
            denied = prepare_custom_api_request(
                tool_name="custom_api__internal__read",
                tool_config={
                    "base_url": url,
                    "action": {"method": "GET", "path": "/"},
                    "approved_networks": ["0.0.0.0/0", "::/0"],
                },
                secret_config={},
                arguments={},
            )
            denied_result = await execute_prepared_custom_api_request("custom_api__internal__read", denied)
            assert "custom_api_target_denied" in denied_result
            assert "sk-live-secret" not in denied_result
    finally:
        land.shutdown()
        start.shutdown()
        land.server_close()
        start.server_close()

    assert "landed" in result
    assert "sk-live-secret" not in result
    assert seen[0][0] == "start" and seen[0][2] == "sk-live-secret"
    assert seen[1] == ("land", "/land", None, "hive", None)


def test_mcp_runtime_approval_survives_live_endpoint_drift():
    from app.services.mcp_metadata_trust import is_mcp_metadata_runtime_approved, prepare_mcp_metadata_candidate

    approved = prepare_mcp_metadata_candidate(
        server_name="ledger",
        server_url="https://mcp.example/ledger",
        tool_name="post_entry",
        raw_description="Post one ledger entry",
        raw_schema={"type": "object", "properties": {}},
    )
    tool = SimpleNamespace(
        type="mcp",
        enabled=True,
        mcp_trust_status="approved",
        mcp_metadata_fingerprint=approved.fingerprint,
        mcp_reviewed_fingerprint=approved.fingerprint,
        mcp_server_name="ledger",
        mcp_server_url="https://attacker.example/ledger",
        mcp_tool_name="post_entry",
        mcp_raw_description="Post one ledger entry",
        mcp_raw_schema={"type": "object", "properties": {}},
        description=approved.canonical_description,
        config={},
    )

    assert is_mcp_metadata_runtime_approved(tool) is False


@pytest.mark.asyncio
async def test_company_denial_omits_one_capability_and_keeps_another():
    from app.services.local_agent_channel_service import _agent_capability_names
    from app.services.local_bridge_service import BridgeAuthContext

    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    context = BridgeAuthContext(
        connection_id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        scopes=("local_agent:receive",),
        client_kind="desktop",
        device_name="fixture",
    )

    class _AgentResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=agent_id, owner_user_id=user_id, creator_id=None, deleted_at=None)

    class _PolicyScalars:
        def all(self):
            return [
                SimpleNamespace(agent_id=None, capability="local_agent.file_upload", allowed=False),
                SimpleNamespace(agent_id=agent_id, capability="local_agent.file_upload", allowed=True),
                SimpleNamespace(agent_id=None, capability="local_agent.file_download", allowed=True),
            ]

    class _PolicyResult:
        def scalars(self):
            return _PolicyScalars()

    class _DB:
        def __init__(self):
            self._calls = 0

        async def execute(self, _statement):
            self._calls += 1
            return _AgentResult() if self._calls == 1 else _PolicyResult()

    allowed = await _agent_capability_names(
        _DB(),
        context=context,
        server_capabilities=("file_upload", "file_download"),
    )

    assert allowed == ("file_download",)
