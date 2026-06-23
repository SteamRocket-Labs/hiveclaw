from __future__ import annotations

import json
import queue
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hive_bridge.runtime import WorkResult


class _JsonRpcProcess:
    def __init__(self, *, command: list[str], cwd: str | None, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        self._next_id = 0
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stash: list[dict[str, Any]] = []
        self._proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self._messages.put(payload)

    def _write_json(self, payload: dict[str, Any]) -> None:
        if self._proc.stdin is None:
            raise RuntimeError("ACP process stdin is closed")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _respond_method_not_found(self, rpc_id: Any) -> None:
        self._write_json(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": "method not implemented by Hive Bridge ACP client"},
            }
        )

    def call(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        rpc_id = self._next_id
        self._write_json({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})

        while True:
            for index, message in enumerate(list(self._stash)):
                if message.get("id") == rpc_id and "method" not in message:
                    self._stash.pop(index)
                    return self._result_or_raise(method, message)

            try:
                message = self._messages.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                raise TimeoutError(f"ACP call timed out: {method}") from exc

            if message.get("method") and "id" in message:
                self._respond_method_not_found(message.get("id"))
                continue
            if message.get("method"):
                continue
            if message.get("id") == rpc_id:
                return self._result_or_raise(method, message)
            self._stash.append(message)

    @staticmethod
    def _result_or_raise(method: str, message: dict[str, Any]) -> Any:
        if message.get("error"):
            error = message["error"]
            raise RuntimeError(f"ACP {method} failed: {error.get('message') or error}")
        return message.get("result")

    def close(self) -> None:
        if self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2)


def _blocks_to_text(blocks: list[Any]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text") or block.get("content") or block.get("message")
            if text:
                parts.append(str(text))
    return "\n".join(part for part in parts if part)


def _extract_text(payload: Any) -> str:
    if payload is None:
        return "(ACP session completed with no text response)"
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("text", "content", "message", "result", "output"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                text = _blocks_to_text(value)
                if text:
                    return text
        for key in ("messages", "blocks"):
            value = payload.get(key)
            if isinstance(value, list):
                text = _blocks_to_text(value)
                if text:
                    return text
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@dataclass
class ACPAdapter:
    command: list[str]
    work_dir: str | None = None
    timeout_seconds: int = 600

    def handle(self, message: dict[str, Any]) -> WorkResult:
        cwd = str(Path(self.work_dir or ".").expanduser().resolve())
        proc = _JsonRpcProcess(command=self.command, cwd=cwd, timeout_seconds=self.timeout_seconds)
        session_id = ""
        try:
            proc.call(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "hive-bridge", "version": "0.1.4"},
                },
            )
            session = proc.call("session/new", {"cwd": cwd, "mcpServers": []})
            if isinstance(session, dict):
                session_id = str(session.get("sessionId") or "")
            if not session_id:
                raise RuntimeError("ACP session/new returned no sessionId")
            prompt_result = proc.call(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": str(message.get("content") or "")}],
                },
            )
            return WorkResult(
                result=_extract_text(prompt_result),
                metadata={"runtime": "acp", "session_id": session_id, "command": self.command},
            )
        finally:
            proc.close()
