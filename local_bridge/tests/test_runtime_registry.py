from __future__ import annotations

import json
import sys

import pytest

from hive_bridge.runtime import WorkResult, create_default_runtime_registry


def test_default_runtime_registry_creates_noop_runtime() -> None:
    registry = create_default_runtime_registry()

    adapter = registry.create("noop")
    result = adapter.handle({"content": "hello local agent"})

    assert isinstance(result, WorkResult)
    assert "hello local agent" in result.result
    assert result.metadata["runtime"] == "noop"


def test_default_runtime_registry_creates_command_runtime() -> None:
    registry = create_default_runtime_registry()
    adapter = registry.create(
        "command",
        command=[sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        timeout_seconds=30,
    )

    result = adapter.handle({"content": "hello from hive"})

    assert isinstance(result, WorkResult)
    assert result.result == "HELLO FROM HIVE"
    assert result.metadata["runtime"] == "command"
    assert result.metadata["exit_code"] == 0


def test_default_runtime_registry_rejects_unknown_runtime() -> None:
    registry = create_default_runtime_registry()

    with pytest.raises(ValueError, match="Unknown local runtime"):
        registry.create("missing")


def test_default_runtime_registry_creates_acp_runtime(tmp_path) -> None:
    script = tmp_path / "fake_acp.py"
    transcript = tmp_path / "transcript.jsonl"
    script.write_text(
        """
import json
import sys
from pathlib import Path

transcript = Path(sys.argv[1])
session_id = "session-test"

for line in sys.stdin:
    request = json.loads(line)
    transcript.write_text(transcript.read_text() + json.dumps(request) + "\\n" if transcript.exists() else json.dumps(request) + "\\n")
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": False, "sessionCapabilities": {}}}
    elif method == "session/new":
        result = {"sessionId": session_id}
    elif method == "session/prompt":
        text = request["params"]["prompt"][0]["text"]
        result = {"text": "ACP processed: " + text}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": "missing"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    registry = create_default_runtime_registry()
    adapter = registry.create(
        "acp",
        command=[sys.executable, str(script), str(transcript)],
        work_dir=str(tmp_path),
        timeout_seconds=5,
    )

    result = adapter.handle({"content": "hello acp"})

    assert isinstance(result, WorkResult)
    assert result.result == "ACP processed: hello acp"
    assert result.metadata["runtime"] == "acp"
    assert result.metadata["session_id"] == "session-test"
    methods = [json.loads(line)["method"] for line in transcript.read_text(encoding="utf-8").splitlines()]
    assert methods == ["initialize", "session/new", "session/prompt"]
