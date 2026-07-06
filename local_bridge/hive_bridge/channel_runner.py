from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from hive_bridge.client import HiveBridgeClient
from hive_bridge.runtime import WorkRequestAdapter, coerce_work_result


class JsonConnection(Protocol):
    def __enter__(self):
        ...

    def __exit__(self, *_args):
        ...

    def send_json(self, payload: dict[str, Any]) -> None:
        ...

    def receive_json(self) -> dict[str, Any]:
        ...


class _WebsocketsSyncConnection:
    def __init__(self, url: str) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover - exercised by package dependency checks
            raise RuntimeError("Install hive-bridge with the websockets dependency to use service mode.") from exc
        self._connect = connect
        self._url = url
        self._ws = None

    def __enter__(self):
        self._ws = self._connect(self._url)
        return self

    def __exit__(self, *_args):
        if self._ws is not None:
            self._ws.close()
        return None

    def send_json(self, payload: dict[str, Any]) -> None:
        assert self._ws is not None
        self._ws.send(json.dumps(payload, ensure_ascii=False))

    def receive_json(self) -> dict[str, Any]:
        assert self._ws is not None
        return json.loads(self._ws.recv())


ConnectionFactory = Callable[[str], JsonConnection]


def default_connection_factory(url: str) -> JsonConnection:
    return _WebsocketsSyncConnection(url)


class HiveBridgeChannelRunner:
    """Foreground Local Agent Channel runner.

    The runner keeps the always-online channel separate from one-shot CLI
    commands.  It can be used by `hive-bridge service start` or foreground
    `hive-bridge run --transport websocket`.
    """

    def __init__(
        self,
        *,
        client: HiveBridgeClient,
        adapter: WorkRequestAdapter,
        connection_factory: ConnectionFactory = default_connection_factory,
        runtime_kind: str = "generic",
        capabilities: dict[str, Any] | None = None,
        downloads_dir: str | Path | None = None,
    ) -> None:
        self.client = client
        self.adapter = adapter
        self.connection_factory = connection_factory
        self.runtime_kind = runtime_kind
        self.capabilities = capabilities or {}
        self.downloads_dir = Path(downloads_dir or ".hive/local-agent-channel/attachments").expanduser()

    def _connect_url(self) -> str:
        ticket = self.client.create_channel_ws_ticket()["ticket"]
        return self.client.channel_ws_url(str(ticket))

    def _prepare_message_for_adapter(self, *, message_id: str, message: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(message)
        attachments: list[dict[str, Any]] = []
        for raw_attachment in list(message.get("attachments") or []):
            if not isinstance(raw_attachment, dict):
                continue
            attachment = dict(raw_attachment)
            source_path = attachment.get("path")
            if not source_path:
                attachments.append(attachment)
                continue
            try:
                downloaded = self.client.download_channel_file(
                    str(source_path),
                    self.downloads_dir / message_id,
                )
                attachment.update(
                    {
                        "local_path": downloaded["path"],
                        "downloaded": True,
                        "size": downloaded["size"],
                    }
                )
            except Exception as exc:
                attachment.update({"downloaded": False, "error": str(exc)})
            attachments.append(attachment)
        prepared["attachments"] = attachments
        return prepared

    def _handle_message(self, connection: JsonConnection, message: dict[str, Any]) -> None:
        message_id = str(message["id"])
        session_id = str(message["session_id"])
        connection.send_json({"type": "ack", "message_id": message_id})

        def emit_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
            self._send_event(connection, session_id, message_id, event_type, payload or {})

        try:
            prepared_message = self._prepare_message_for_adapter(message_id=message_id, message=message)
            self._send_event(connection, session_id, message_id, "typing", {"status": "running"})
            stream_handler = getattr(self.adapter, "handle_stream", None)
            if callable(stream_handler):
                result = coerce_work_result(stream_handler(prepared_message, emit_event))
            else:
                result = coerce_work_result(self.adapter.handle(prepared_message))
        except Exception as exc:
            error_text = str(exc) or type(exc).__name__
            error_metadata = {
                "error": error_text,
                "error_type": type(exc).__name__,
                "runtime_kind": self.runtime_kind,
            }
            self._send_event(connection, session_id, message_id, "error", error_metadata)
            connection.send_json(
                {
                    "type": "result",
                    "session_id": session_id,
                    "message_id": message_id,
                    "status": "failed",
                    "output": f"Local runtime failed: {error_text}",
                    "artifacts": [],
                    "metadata": error_metadata,
                }
            )
            return
        connection.send_json(
            {
                "type": "result",
                "session_id": session_id,
                "message_id": message_id,
                "status": "completed",
                "output": result.result,
                "artifacts": result.attachments,
                "metadata": result.metadata,
            }
        )

    @staticmethod
    def _send_event(
        connection: JsonConnection,
        session_id: str,
        message_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.send_json(
            {
                "type": "event",
                "session_id": session_id,
                "message_id": message_id,
                "event_type": event_type,
                "payload": payload,
            }
        )

    def run_session(self, *, max_messages: int | None = None) -> int:
        """Connect and process channel messages until the socket closes or the limit is reached."""

        with self.connection_factory(self._connect_url()) as connection:
            hello = connection.receive_json()
            if hello.get("type") != "hello":
                raise RuntimeError(f"Expected hello from Hive Local Agent Channel, got {hello!r}")
            connection.send_json(
                {
                    "type": "ready",
                    "runtime_kind": self.runtime_kind,
                    "capabilities": self.capabilities,
                }
            )
            processed = 0
            while True:
                incoming = connection.receive_json()
                incoming_type = incoming.get("type")
                if incoming_type in {"ready_ack", "pong"}:
                    continue
                if incoming_type == "message":
                    self._handle_message(connection, dict(incoming["message"]))
                    processed += 1
                    if max_messages is not None and processed >= max_messages:
                        return processed
                    continue
                if incoming_type == "error":
                    raise RuntimeError(str(incoming.get("error") or incoming))

    def run_once(self) -> int:
        """Connect, process the first channel message, and return processed count."""

        return self.run_session(max_messages=1)

    def run_forever(self, *, max_runs: int | None = None, reconnect_delay_seconds: float = 1.0) -> int:
        runs = 0
        while max_runs is None or runs < max_runs:
            runs += 1
            try:
                self.run_session()
            except Exception:
                if reconnect_delay_seconds > 0:
                    time.sleep(reconnect_delay_seconds)
        return runs
