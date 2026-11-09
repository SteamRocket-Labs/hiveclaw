"""Bound retained tool text before serialization or accumulation."""

from collections.abc import Iterable


TOOL_OUTPUT_BYTES = 1024 * 1024
TOOL_OUTPUT_TRUNCATION = "\n\n结果过大，只保留了前面部分。"


def bound_tool_output(text: str, *, byte_limit: int = TOOL_OUTPUT_BYTES) -> str:
    # Inspect at most one bounded prefix, including for a single enormous line.
    # PostgreSQL repairs NUL to this literal spelling before storing text.
    prefix = text[:byte_limit].replace("\x00", "\\u0000").encode("utf-8")
    if len(text) <= byte_limit and len(prefix) <= byte_limit:
        return text
    budget = byte_limit - len(TOOL_OUTPUT_TRUNCATION.encode("utf-8"))
    return prefix[:max(0, budget)].decode("utf-8", errors="ignore") + TOOL_OUTPUT_TRUNCATION


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
