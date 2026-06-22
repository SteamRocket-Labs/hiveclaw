from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from hive_bridge.client import HiveBridgeClient
from hive_bridge.token_store import FileTokenStore


def _text_result(payload: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ]
    }


class HiveBridgeMCPServer:
    def __init__(self, *, client_factory: Callable[[], HiveBridgeClient] | None = None) -> None:
        self._client_factory = client_factory or self._client_from_config

    @staticmethod
    def _client_from_config() -> HiveBridgeClient:
        config = FileTokenStore().load()
        if config is None:
            raise RuntimeError("Hive Bridge is not logged in. Run `hive-bridge login` first.")
        return HiveBridgeClient(base_url=config.base_url, token=config.token)

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "hive_status",
                "description": "Check the current Hive Bridge connection.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "hive_poll_inbox",
                "description": "Poll Hive for pending local-agent messages or work requests.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "hive_send_message",
                "description": "Send a message from the local agent bridge to a Hive target.",
                "inputSchema": {
                    "type": "object",
                    "required": ["target", "content"],
                    "properties": {
                        "target": {"type": "string"},
                        "content": {"type": "string"},
                        "channel": {"type": "string"},
                        "client_message_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "hive_report_result",
                "description": "Report the result for a Hive message or work request.",
                "inputSchema": {
                    "type": "object",
                    "required": ["message_id", "result"],
                    "properties": {
                        "message_id": {"type": "string"},
                        "result": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "hive_upload_file",
                "description": "Upload one local file to Hive and attach it as a transcript artifact.",
                "inputSchema": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {"path": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._client_factory()
        if name == "hive_status":
            return client.status()
        if name == "hive_poll_inbox":
            return client.poll_inbox()
        if name == "hive_send_message":
            return client.send_message(
                target=arguments["target"],
                content=arguments["content"],
                channel=arguments.get("channel"),
                client_message_id=arguments.get("client_message_id"),
            )
        if name == "hive_report_result":
            return client.report_result(message_id=arguments["message_id"], result=arguments["result"])
        if name == "hive_upload_file":
            return client.upload_file(Path(arguments["path"]))
        raise ValueError(f"Unknown Hive Bridge tool: {name}")

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "hive-bridge", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                }
            elif method == "notifications/initialized":
                return None
            elif method == "tools/list":
                result = {"tools": self.list_tools()}
            elif method == "tools/call":
                params = request.get("params") or {}
                result = _text_result(self.call_tool(params["name"], params.get("arguments") or {}))
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:  # MCP errors must be JSON-RPC responses, not stderr-only crashes.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def serve(self, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        input_stream = stdin or sys.stdin
        output_stream = stdout or sys.stdout
        for line in input_stream:
            if not line.strip():
                continue
            response = self.handle_jsonrpc(json.loads(line))
            if response is None:
                continue
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()
