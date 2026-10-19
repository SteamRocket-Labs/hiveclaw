from contextlib import contextmanager
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace

import pytest

from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError


async def test_binary_credentials_are_rejected_before_remote_dispatch():
    with remote_server() as remote:
        with pytest.raises(RemoteA2AError, match="remote_a2a_credential_in_request"):
            await remote.endpoint.rpc("SendMessage", {"message": {"messageId": "original", "role": "ROLE_USER",
                "parts": [{"raw": base64.b64encode(b"prefix::synthetic-remote-credential::suffix").decode()}]}})
        assert not remote.requests


@contextmanager
def remote_server():
    state = SimpleNamespace(
        requests=[], reply=lambda request: {"id": "remote-task", "status": {"state": "TASK_STATE_WORKING"}}
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.requests.append(("GET", self.path, dict(self.headers), None))
            self.respond(state.card)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(("POST", self.path, dict(self.headers), request))
            self.respond({"jsonrpc": "2.0", "id": request["id"], "result": state.reply(request)})

        def respond(self, body):
            content = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    state.origin = f"http://127.0.0.1:{server.server_port}"
    state.card = {
        "name": "Reference Agent",
        "version": "1.0.0",
        "capabilities": {},
        "supportedInterfaces": [{"url": state.origin + "/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
    }
    state.endpoint = RemoteA2AEndpoint(
        state.origin + "/card", state.origin + "/rpc", "synthetic-remote-credential", allowed_networks=("127.0.0.1/32",)
    )
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


async def test_remote_card_and_rpc_use_exact_configured_sink_and_preserve_full_content():
    with remote_server() as remote:
        endpoint = RemoteA2AEndpoint(
            remote.origin + "/card",
            (remote.origin + "/rpc").replace("http:", "HTTP:"),
            "synthetic-remote-credential",
            allowed_networks=("127.0.0.1/32",),
        )
        remote.card["vendorDeclaration"] = {"actionInterception": True}
        card = await endpoint.discover()
        assert card["vendorDeclaration"] == {"actionInterception": True}
        text = "Complete result\n" * 4000
        remote.reply = lambda request: {
            "task": {
                "id": "remote-task",
                "contextId": "remote-context",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {"artifactId": "report", "parts": [{"text": text}, {"url": remote.origin + "/untrusted-file"}]}
                ],
            }
        }
        result = await remote.endpoint.rpc(
            "SendMessage",
            {
                "message": {
                    "messageId": "original-input",
                    "role": "ROLE_USER",
                    "parts": [{"text": "Write the report."}],
                },
                "configuration": {"returnImmediately": True},
            },
            request_id="stable-rpc-id",
        )
        assert result["task"]["artifacts"][0]["parts"][0]["text"] == text
        assert [(method, path) for method, path, _headers, _body in remote.requests] == [
            ("GET", "/card"),
            ("POST", "/rpc"),
        ]
        assert all(
            headers["Authorization"] == "Bearer synthetic-remote-credential" for _, _, headers, _ in remote.requests
        )
        assert remote.requests[-1][3]["id"] == "stable-rpc-id"


@pytest.mark.parametrize("method", ["GetTask", "CancelTask"])
async def test_remote_task_response_cannot_switch_original_task(method):
    with remote_server() as remote:
        with pytest.raises(RemoteA2AError, match="remote_a2a_task_identity_mismatch") as error:
            await remote.endpoint.rpc(method, {"id": "original-task"})
        assert error.value.execution_status == "unknown"
        assert len(remote.requests) == 1


@pytest.mark.parametrize(
    "method,params",
    [
        ("GetTask", {}),
        ("CancelTask", {}),
        ("SendMessage", {"message": {"role": "ROLE_USER", "parts": [{"text": "Missing stable input ID"}]}}),
    ],
)
async def test_remote_missing_request_identity_never_crosses_network(method, params):
    with remote_server() as remote:
        with pytest.raises(RemoteA2AError, match="remote_a2a_invalid_request"):
            await remote.endpoint.rpc(method, params)
        assert not remote.requests


async def test_remote_card_cannot_redirect_credential_or_expose_exact_secret():
    with remote_server() as remote:
        remote.card["description"] = "prefix::synthetic-remote-credential::suffix"
        card = await remote.endpoint.discover()
        assert card["description"] == "prefix::[REDACTED_SECRET]::suffix"
        remote.card["supportedInterfaces"][0]["url"] = remote.origin + "/other-rpc"
        with pytest.raises(RemoteA2AError, match="remote_a2a_configured_interface_not_supported"):
            await remote.endpoint.discover()
        assert all(path == "/card" for _, path, _, _ in remote.requests)


async def test_private_remote_endpoint_requires_explicit_network_grant():
    with remote_server() as remote:
        endpoint = RemoteA2AEndpoint(remote.origin + "/card", remote.origin + "/rpc")
        with pytest.raises(RemoteA2AError, match="network_target_denied"):
            await endpoint.discover()
        assert not remote.requests


async def test_remote_wrong_context_is_preserved_as_untrusted_observation_for_reconciliation():
    with remote_server() as remote:
        remote.reply = lambda _request: {
            "task": {
                "id": "remote-task",
                "contextId": "other-context",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "metadata": {"partialResult": "partial::synthetic-remote-credential"},
            }
        }
        with pytest.raises(RemoteA2AError, match="remote_a2a_context_identity_mismatch") as error:
            await remote.endpoint.rpc(
                "SendMessage",
                {
                    "message": {
                        "messageId": "stable-input",
                        "contextId": "original-context",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Continue original work"}],
                    }
                },
            )
        assert error.value.execution_status == "unknown"
        assert error.value.observation["result"]["task"]["metadata"]["partialResult"] == "partial::[REDACTED_SECRET]"
        assert len(remote.requests) == 1


async def test_public_cleartext_endpoint_cannot_receive_a_remote_credential(monkeypatch):
    from app.services import governed_egress

    def forbidden_client(*_args, **_kwargs):
        pytest.fail("Cleartext public credentials reached HTTP client creation")

    monkeypatch.setattr(governed_egress, "_default_client_factory", forbidden_client)
    endpoint = RemoteA2AEndpoint("http://8.8.8.8/card", "http://8.8.8.8/rpc", "synthetic-remote-only")
    with pytest.raises(RemoteA2AError, match="network_credential_transport_denied"):
        await endpoint.discover()
