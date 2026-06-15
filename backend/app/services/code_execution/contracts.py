"""Shared contracts for code execution providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CodeExecutionResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    error: str | None = None
    timed_out: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


def render_command_result(command_label: str | None, result: CodeExecutionResult) -> str:
    if result.error:
        return result.error

    result_parts: list[str] = []
    if command_label:
        result_parts.append(f"💻 Command: {command_label}")
    if result.stdout.strip():
        result_parts.append(f"📤 Output:\n{result.stdout}")
    if result.stderr.strip():
        result_parts.append(f"⚠️ Stderr:\n{result.stderr}")
    if result.exit_code != 0:
        result_parts.append(f"Exit code: {result.exit_code}")
    elif not result.stdout.strip() and not result.stderr.strip():
        result_parts.append("✅ Command executed successfully (no output)")

    return "\n\n".join(result_parts)
