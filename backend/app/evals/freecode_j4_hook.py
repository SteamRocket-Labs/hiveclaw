#!/usr/bin/env python3
"""Fail-closed PreToolUse path boundary for the P08-J4 FreeCode run."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


WORKSPACE_ROOT_ENV = "HIVE_J4_WORKSPACE_ROOT"
LOG_PATH_ENV = "HIVE_J4_FREECODE_HOOK_LOG"
FILE_TOOLS = frozenset({"Read", "Write", "Edit", "Glob", "Grep"})


def _required_workspace_root() -> Path:
    raw = os.environ.get(WORKSPACE_ROOT_ENV, "").strip()
    path = Path(raw).expanduser() if raw else Path()
    if not raw or not path.is_absolute():
        raise ValueError("workspace root unavailable")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("workspace root unavailable")
    return resolved


def _required_log_path() -> Path:
    raw = os.environ.get(LOG_PATH_ENV, "").strip()
    path = Path(raw).expanduser() if raw else Path()
    if not raw or not path.is_absolute() or path.is_symlink():
        raise ValueError("hook log unavailable")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("hook log unavailable")
    return parent / path.name


def _inside_workspace(workspace_root: Path, value: str, *, relative_to: Path | None = None) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (relative_to or workspace_root) / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("outside workspace") from exc
    return resolved


def _validate_glob_pattern(workspace_root: Path, search_root: Path, pattern: Any) -> None:
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        raise ValueError("invalid glob pattern")
    if (
        Path(pattern).is_absolute()
        or ".." in pattern
        or pattern.startswith(("!", "~"))
        or any(character in pattern for character in "{}\\")
    ):
        raise ValueError("unsafe glob pattern")
    match = re.search(r"[*?[{]", pattern)
    if match is None:
        probe = pattern
    else:
        prefix = pattern[: match.start()]
        probe = prefix if prefix.endswith(("/", os.sep)) else str(Path(prefix).parent)
    _inside_workspace(workspace_root, probe or ".", relative_to=search_root)


def _relative_path(workspace_root: Path, resolved: Path) -> str:
    relative = resolved.relative_to(workspace_root).as_posix()
    return relative or "."


def _evaluate(payload: Any, workspace_root: Path) -> tuple[bool, str, str, str, str]:
    if not isinstance(payload, dict):
        return False, "<invalid>", "invalid hook input", "<invalid>", hashlib.sha256(b"").hexdigest()
    raw_tool_name = payload.get("tool_name")
    tool_name = raw_tool_name if raw_tool_name in FILE_TOOLS else "<invalid>"
    tool_use_id = payload.get("tool_use_id")
    tool_use_id_hash = hashlib.sha256(tool_use_id.encode("utf-8") if isinstance(tool_use_id, str) else b"").hexdigest()
    tool_input = payload.get("tool_input")
    if (
        payload.get("hook_event_name") != "PreToolUse"
        or tool_name == "<invalid>"
        or not isinstance(tool_use_id, str)
        or not tool_use_id
        or not isinstance(tool_input, dict)
    ):
        return False, "<invalid>", "invalid PreToolUse input", tool_name, tool_use_id_hash

    path_key = "file_path" if tool_name in {"Read", "Write", "Edit"} else "path"
    raw_path = tool_input.get(path_key, "." if tool_name in {"Glob", "Grep"} else None)
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return False, "<invalid>", "file tool path is required", tool_name, tool_use_id_hash
    try:
        resolved = _inside_workspace(workspace_root, raw_path)
        if tool_name == "Glob":
            _validate_glob_pattern(workspace_root, resolved, tool_input.get("pattern"))
    except (OSError, RuntimeError, ValueError):
        return False, "<outside>", "file tool path is outside the J4 workspace", tool_name, tool_use_id_hash
    return True, _relative_path(workspace_root, resolved), "J4 workspace path attested", tool_name, tool_use_id_hash


def _append_log(log_path: Path, record: dict[str, Any]) -> None:
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(log_path, flags, 0o600)
    try:
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if os.write(descriptor, encoded) != len(encoded):
            raise OSError("short hook log write")
    finally:
        os.close(descriptor)


def _decision(allowed: bool, reason: str) -> dict[str, Any]:
    decision = "allow" if allowed else "deny"
    return {
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        },
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        payload = None

    try:
        workspace_root = _required_workspace_root()
        allowed, relative_path, reason, tool_name, tool_use_id_hash = _evaluate(payload, workspace_root)
        log_path = _required_log_path()
        _append_log(
            log_path,
            {
                "allowed": allowed,
                "resolved_relative_path": relative_path,
                "tool_name": tool_name,
                "tool_use_id_hash": tool_use_id_hash,
            },
        )
    except Exception:  # A missing boundary input or audit sink must deny the effect.
        allowed = False
        reason = "J4 file boundary unavailable"
    print(json.dumps(_decision(allowed, reason), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
