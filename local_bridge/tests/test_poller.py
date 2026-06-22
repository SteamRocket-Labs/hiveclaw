from __future__ import annotations

from hive_bridge.poller import HiveBridgeRunner
from hive_bridge.runtime import WorkResult


class _FakeClient:
    def __init__(self):
        self.reports = []

    def poll_inbox(self):
        return {
            "messages": [
                {"id": "msg-ignore", "content": "hello", "metadata": {}},
                {"id": "msg-work", "content": "do local work", "metadata": {"kind": "work_request"}},
            ]
        }

    def report_result(self, *, message_id, result, attachments=None, metadata=None):
        self.reports.append((message_id, result, attachments or [], metadata or {}))
        return {"status": "ok"}


class _FakeAdapter:
    def __init__(self):
        self.messages = []

    def handle(self, message):
        self.messages.append(message)
        return "done locally"


def test_runner_processes_only_work_requests() -> None:
    client = _FakeClient()
    adapter = _FakeAdapter()
    runner = HiveBridgeRunner(client=client, adapter=adapter)

    processed = runner.run_once()

    assert processed == 1
    assert adapter.messages == [{"id": "msg-work", "content": "do local work", "metadata": {"kind": "work_request"}}]
    assert client.reports == [("msg-work", "done locally", [], {})]


def test_runner_reports_structured_adapter_result() -> None:
    client = _FakeClient()

    class _StructuredAdapter:
        def handle(self, message):
            return WorkResult(
                result=f"handled {message['id']}",
                attachments=[{"path": "workspace/local-bridge/result.md"}],
                metadata={"runtime": "test-runtime"},
            )

    runner = HiveBridgeRunner(client=client, adapter=_StructuredAdapter())

    processed = runner.run_once()

    assert processed == 1
    assert client.reports == [
        (
            "msg-work",
            "handled msg-work",
            [{"path": "workspace/local-bridge/result.md"}],
            {"runtime": "test-runtime"},
        )
    ]
