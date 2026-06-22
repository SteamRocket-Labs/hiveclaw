#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    content = sys.stdin.read().strip()
    if not content:
        print("No task content received.")
        return 0

    if "上传" not in content or "md" not in content.lower() or "workspace" not in content.lower():
        print(f"Codex local auto adapter received the task, but no supported action matched.\n\nTask:\n{content}")
        return 0

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    path = Path(tempfile.gettempdir()) / f"hive-bridge-codex-auto-{timestamp}.md"
    path.write_text(
        "# 你好，我是 Codex\n\n"
        "你好，我是 codex，我现在可以自主执行任务。\n\n"
        f"云端任务原文：{content}\n",
        encoding="utf-8",
    )

    config_path = os.environ.get("HIVE_BRIDGE_CONFIG", "/tmp/hive-bridge-codex-web3.json")
    proc = subprocess.run(
        ["hive-bridge", "--config", config_path, "upload", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(
            "Codex local auto adapter failed to upload the generated Markdown file.\n\n"
            f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
        )
        return 0

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"Codex local auto adapter uploaded the file, but upload output was not JSON:\n{proc.stdout}")
        return 0

    artifact = (payload.get("artifacts") or [{}])[0]
    print(
        "Codex local auto adapter completed the task without manual intervention.\n\n"
        f"Generated local file: {path}\n"
        f"Workspace path: {payload.get('workspace_path')}\n"
        f"Artifact id: {artifact.get('artifact_id')}\n"
        f"Upload message id: {payload.get('message_id')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
