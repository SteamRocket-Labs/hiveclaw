from __future__ import annotations

from hive_bridge.mcp_server import HiveBridgeMCPServer


class _FakeClient:
    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append(("status", None))
        return {"status": "connected"}

    def poll_inbox(self):
        self.calls.append(("poll_inbox", None))
        return {"messages": []}

    def send_message(self, *, target, content, channel=None, client_message_id=None):
        self.calls.append(("send_message", target, content, channel, client_message_id))
        return {"status": "accepted"}

    def report_result(self, *, message_id, result):
        self.calls.append(("report_result", message_id, result))
        return {"status": "ok"}

    def upload_file(self, path):
        self.calls.append(("upload_file", str(path)))
        return {"workspace_path": "workspace/uploads/report.md"}


def test_mcp_server_lists_hive_tools() -> None:
    server = HiveBridgeMCPServer(client_factory=lambda: _FakeClient())

    names = {tool["name"] for tool in server.list_tools()}

    assert names == {
        "hive_status",
        "hive_poll_inbox",
        "hive_send_message",
        "hive_report_result",
        "hive_upload_file",
    }


def test_mcp_server_calls_client_tools() -> None:
    fake = _FakeClient()
    server = HiveBridgeMCPServer(client_factory=lambda: fake)

    assert server.call_tool("hive_status", {})["status"] == "connected"
    assert server.call_tool("hive_send_message", {"target": "Web3研究员", "content": "ping"}) == {
        "status": "accepted"
    }
    assert server.call_tool("hive_upload_file", {"path": "./report.md"}) == {
        "workspace_path": "workspace/uploads/report.md"
    }

    assert fake.calls == [
        ("status", None),
        ("send_message", "Web3研究员", "ping", None, None),
        ("upload_file", "report.md"),
    ]
