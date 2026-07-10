from __future__ import annotations

import types
import sys

from hive_bridge.channel_runner import HiveBridgeChannelRunner
from hive_bridge.execution_receipts import LocalExecutionReceiptStore
from hive_bridge.runtime import CommandAdapter, WorkResult


class _FakeClient:
    def __init__(self):
        self.ticket_requests = 0
        self.downloads = []

    def create_channel_ws_ticket(self):
        self.ticket_requests += 1
        return {"ticket": "hbt_secret", "expires_in": 60, "single_use": True}

    def channel_ws_url(self, ticket: str) -> str:
        assert ticket == "hbt_secret"
        return "wss://hive.example/api/v1/local-bridge/channel/ws?ticket=hbt_secret"

    def download_channel_file(self, path: str, destination_dir):
        destination_dir.mkdir(parents=True, exist_ok=True)
        local_path = destination_dir / path.split("/")[-1]
        local_path.write_text("downloaded from Hive\n", encoding="utf-8")
        result = {
            "path": str(local_path),
            "source_path": path,
            "size": local_path.stat().st_size,
        }
        self.downloads.append(result)
        return result


class _FakeConnection:
    def __init__(self):
        self.sent = []
        self.received = [
            {"type": "hello", "connection_id": "conn-1", "owner_user_id": "user-1"},
            {
                "type": "ready_ack",
                "status": "online",
                "snapshot_hash": "snapshot-1",
                "effective_capabilities": [
                    "event_stream",
                    "execute",
                    "file_upload",
                    "result_report",
                ],
                "expires_at": "2026-07-11T00:00:00+00:00",
            },
            {
                "type": "message",
                "message": {
                    "id": "message-1",
                    "session_id": "session-1",
                    "content": "do local work",
                    "attachments": [],
                    "metadata": {"source": "web"},
                    "replay_key": "local:message-1",
                },
            },
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send_json(self, payload):
        self.sent.append(payload)

    def receive_json(self):
        if not self.received:
            return {"type": "pong"}
        return self.received.pop(0)


class _FakeAdapter:
    def __init__(self):
        self.messages = []

    def handle(self, message):
        self.messages.append(message)
        return WorkResult(
            result="done locally",
            attachments=[{"path": "workspace/local/result.md"}],
            metadata={"runtime": "test"},
        )


class _MemoryReceiptStore:
    def __init__(self):
        self.records = {}

    def get(self, replay_key):
        result = self.records.get(replay_key)
        return dict(result) if result is not None else None

    def put(self, replay_key, result):
        self.records[replay_key] = dict(result)


class _AttachmentConnection(_FakeConnection):
    def __init__(self):
        super().__init__()
        self.received[1]["effective_capabilities"].append("file_download")
        self.received[2]["message"]["attachments"] = [
            {"path": "workspace/uploads/cloud-brief.md", "filename": "cloud-brief.md"}
        ]


class _MultiMessageConnection(_FakeConnection):
    def __init__(self):
        super().__init__()
        self.received = [
            {"type": "hello", "connection_id": "conn-1", "owner_user_id": "user-1"},
            {
                "type": "ready_ack",
                "status": "online",
                "snapshot_hash": "snapshot-1",
                "effective_capabilities": ["event_stream", "execute", "result_report"],
                "expires_at": "2026-07-11T00:00:00+00:00",
            },
            {
                "type": "message",
                "message": {
                    "id": "message-1",
                    "session_id": "session-1",
                    "content": "first",
                    "attachments": [],
                    "metadata": {"source": "web"},
                    "replay_key": "local:message-1",
                },
            },
            {
                "type": "message",
                "message": {
                    "id": "message-2",
                    "session_id": "session-1",
                    "content": "second",
                    "attachments": [],
                    "metadata": {"source": "web"},
                    "replay_key": "local:message-2",
                },
            },
        ]


class _StreamingAdapter:
    def handle_stream(self, message, emit_event):
        emit_event("delta", {"text": f"working on {message['content']}"})
        return WorkResult(result="stream done", metadata={"runtime": "stream-test"})


class _FailingAdapter:
    def handle(self, message):
        raise RuntimeError("adapter exploded")


def test_channel_runner_processes_one_websocket_message() -> None:
    client = _FakeClient()
    connection = _FakeConnection()
    adapter = _FakeAdapter()
    opened = {}

    def connection_factory(url: str):
        opened["url"] = url
        return connection

    runner = HiveBridgeChannelRunner(
        client=client,
        adapter=adapter,
        connection_factory=connection_factory,
        runtime_kind="codex",
        capabilities={"file_upload": True},
        receipt_store=_MemoryReceiptStore(),
    )

    processed = runner.run_once()

    assert processed == 1
    assert client.ticket_requests == 1
    assert (
        opened["url"]
        == "wss://hive.example/api/v1/local-bridge/channel/ws?ticket=hbt_secret"
    )
    assert adapter.messages == [
        {
            "id": "message-1",
            "session_id": "session-1",
            "content": "do local work",
            "attachments": [],
            "metadata": {"source": "web"},
            "replay_key": "local:message-1",
        }
    ]
    assert connection.sent == [
        {
            "type": "ready",
            "runtime_kind": "codex",
            "capabilities": {
                "event_stream": True,
                "execute": True,
                "file_upload": True,
                "result_report": True,
            },
        },
        {"type": "ack", "message_id": "message-1"},
        {
            "type": "event",
            "session_id": "session-1",
            "message_id": "message-1",
            "event_type": "typing",
            "payload": {"status": "running"},
        },
        {
            "type": "result",
            "session_id": "session-1",
            "message_id": "message-1",
            "status": "completed",
            "output": "done locally",
            "artifacts": [{"path": "workspace/local/result.md"}],
            "metadata": {
                "runtime": "test",
                "capability_snapshot_hash": "snapshot-1",
                "replay_key": "local:message-1",
            },
        },
    ]
    assert runner.capability_snapshot_hash == "snapshot-1"


def test_channel_runner_downloads_message_attachments_before_adapter(tmp_path) -> None:
    client = _FakeClient()
    connection = _AttachmentConnection()
    adapter = _FakeAdapter()

    runner = HiveBridgeChannelRunner(
        client=client,
        adapter=adapter,
        connection_factory=lambda _url: connection,
        runtime_kind="codex",
        capabilities={"file_download": True},
        downloads_dir=tmp_path / "downloads",
        receipt_store=_MemoryReceiptStore(),
    )

    processed = runner.run_once()

    assert processed == 1
    assert len(client.downloads) == 1
    attachment = adapter.messages[0]["attachments"][0]
    assert attachment["path"] == "workspace/uploads/cloud-brief.md"
    assert attachment["local_path"].endswith("downloads/message-1/cloud-brief.md")
    assert attachment["downloaded"] is True
    assert attachment["size"] == len("downloaded from Hive\n")
    assert (tmp_path / "downloads" / "message-1" / "cloud-brief.md").read_text(
        encoding="utf-8"
    ) == "downloaded from Hive\n"


def test_channel_runner_processes_multiple_messages_on_one_websocket_session() -> None:
    connection = _MultiMessageConnection()
    runner = HiveBridgeChannelRunner(
        client=_FakeClient(),
        adapter=_FakeAdapter(),
        connection_factory=lambda _url: connection,
        runtime_kind="codex",
        receipt_store=_MemoryReceiptStore(),
    )

    assert runner.run_session(max_messages=2) == 2
    assert [
        payload["message_id"] for payload in connection.sent if payload["type"] == "ack"
    ] == [
        "message-1",
        "message-2",
    ]
    assert [
        payload["message_id"]
        for payload in connection.sent
        if payload["type"] == "result"
    ] == [
        "message-1",
        "message-2",
    ]


def test_channel_runner_streams_adapter_delta_events_before_result() -> None:
    connection = _FakeConnection()
    runner = HiveBridgeChannelRunner(
        client=_FakeClient(),
        adapter=_StreamingAdapter(),
        connection_factory=lambda _url: connection,
        runtime_kind="codex",
        receipt_store=_MemoryReceiptStore(),
    )

    assert runner.run_once() == 1
    events = [payload for payload in connection.sent if payload["type"] == "event"]
    assert {
        "type": "event",
        "session_id": "session-1",
        "message_id": "message-1",
        "event_type": "delta",
        "payload": {"text": "working on do local work"},
    } in events
    assert connection.sent[-1]["type"] == "result"


def test_channel_runner_streams_command_adapter_output_before_result() -> None:
    connection = _FakeConnection()
    runner = HiveBridgeChannelRunner(
        client=_FakeClient(),
        adapter=CommandAdapter(
            command=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('alpha\\n'); sys.stdout.flush(); sys.stdout.write('beta\\n')",
            ],
        ),
        connection_factory=lambda _url: connection,
        runtime_kind="command",
        receipt_store=_MemoryReceiptStore(),
    )

    assert runner.run_once() == 1
    deltas = [
        payload
        for payload in connection.sent
        if payload["type"] == "event" and payload["event_type"] == "delta"
    ]
    assert "".join(str(payload["payload"]["text"]) for payload in deltas).startswith(
        "alpha"
    )
    assert connection.sent[-1]["type"] == "result"
    assert "beta" in connection.sent[-1]["output"]


def test_channel_runner_reports_failed_result_when_adapter_raises() -> None:
    connection = _FakeConnection()
    runner = HiveBridgeChannelRunner(
        client=_FakeClient(),
        adapter=_FailingAdapter(),
        connection_factory=lambda _url: connection,
        runtime_kind="codex",
        receipt_store=_MemoryReceiptStore(),
    )

    assert runner.run_once() == 1
    assert {"type": "ack", "message_id": "message-1"} in connection.sent
    error_events = [
        payload
        for payload in connection.sent
        if payload["type"] == "event" and payload["event_type"] == "error"
    ]
    assert error_events == [
        {
            "type": "event",
            "session_id": "session-1",
            "message_id": "message-1",
            "event_type": "error",
            "payload": {
                "error": "adapter exploded",
                "error_type": "RuntimeError",
                "runtime_kind": "codex",
                "capability_snapshot_hash": "snapshot-1",
                "replay_key": "local:message-1",
            },
        }
    ]
    assert connection.sent[-1] == {
        "type": "result",
        "session_id": "session-1",
        "message_id": "message-1",
        "status": "failed",
        "output": "Local runtime failed: adapter exploded",
        "artifacts": [],
        "metadata": {
            "error": "adapter exploded",
            "error_type": "RuntimeError",
            "runtime_kind": "codex",
            "capability_snapshot_hash": "snapshot-1",
            "replay_key": "local:message-1",
        },
    }


def test_channel_runner_replays_durable_local_result_without_reexecuting_adapter(
    tmp_path,
) -> None:
    receipt_store = LocalExecutionReceiptStore(tmp_path / "receipts.json")
    first_connection = _FakeConnection()
    first_adapter = _FakeAdapter()
    first_runner = HiveBridgeChannelRunner(
        client=_FakeClient(),
        adapter=first_adapter,
        connection_factory=lambda _url: first_connection,
        receipt_store=receipt_store,
    )

    assert first_runner.run_once() == 1
    assert len(first_adapter.messages) == 1

    replay_connection = _FakeConnection()
    replay_adapter = _FakeAdapter()
    replay_runner = HiveBridgeChannelRunner(
        client=_FakeClient(),
        adapter=replay_adapter,
        connection_factory=lambda _url: replay_connection,
        receipt_store=LocalExecutionReceiptStore(tmp_path / "receipts.json"),
    )

    assert replay_runner.run_once() == 1
    assert replay_adapter.messages == []
    replay_result = [
        payload for payload in replay_connection.sent if payload["type"] == "result"
    ][-1]
    assert replay_result["output"] == "done locally"
    assert replay_result["metadata"]["idempotent_replay"] is True
    assert replay_result["metadata"]["replay_key"] == "local:message-1"


def test_channel_runner_foreground_loop_retries_after_transient_disconnect() -> None:
    runner = HiveBridgeChannelRunner(
        client=_FakeClient(),
        adapter=_FakeAdapter(),
        connection_factory=lambda _url: _FakeConnection(),
        receipt_store=_MemoryReceiptStore(),
    )
    attempts = {"count": 0}

    def fake_run_session(self):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("network dropped")
        return 0

    runner.run_session = types.MethodType(fake_run_session, runner)

    assert runner.run_forever(max_runs=2, reconnect_delay_seconds=0) == 2
    assert attempts["count"] == 2
