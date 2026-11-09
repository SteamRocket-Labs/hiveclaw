"""Bound retained tool text before serialization or accumulation."""

from collections.abc import Iterable
import json


TOOL_OUTPUT_BYTES = 64 * 1024
TOOL_OUTPUT_TRUNCATION = "\n\n结果较多，已保留前面部分。请缩小范围后继续查找。"


def bound_tool_output(text: str) -> str:
    # Inspect at most one bounded prefix, including for a single enormous line.
    prefix = text[:TOOL_OUTPUT_BYTES].replace("\x00", "\\u0000")
    if len(text) <= TOOL_OUTPUT_BYTES and len(json.dumps(prefix, ensure_ascii=False).encode("utf-8")) <= TOOL_OUTPUT_BYTES:
        return text
    # Six bytes per character covers JSON control escapes as well as UTF-8.
    budget = TOOL_OUTPUT_BYTES - len(json.dumps(TOOL_OUTPUT_TRUNCATION, ensure_ascii=False).encode("utf-8"))
    return prefix[:budget // 6] + TOOL_OUTPUT_TRUNCATION


class ToolOutputBuffer:
    """Collect a bounded head and stop requesting further result fragments."""

    def __init__(self):
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def append(self, text: str) -> None:
        if self.truncated:
            return
        bounded = bound_tool_output(text)
        self.parts.append(bounded)
        self.size += len(bounded.encode("utf-8")) + (1 if len(self.parts) > 1 else 0)
        if bounded != text or self.size > TOOL_OUTPUT_BYTES:
            self.parts = [bound_tool_output("\n".join(self.parts))]
            self.truncated = True

    def extend(self, parts: Iterable[str]) -> None:
        for part in parts:
            self.append(part)
            if self.truncated:
                break

    def __bool__(self):
        return bool(self.parts)

    def __iter__(self):
        return iter(self.parts)

    def text(self) -> str:
        return "\n".join(self.parts)

    def cut(self) -> None:
        self.parts = [bound_tool_output(self.text() + TOOL_OUTPUT_TRUNCATION)]
        self.truncated = True
