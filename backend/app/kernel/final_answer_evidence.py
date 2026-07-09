"""Final answer checks against structured tool evidence."""

from __future__ import annotations

import re
from typing import Any


_UNBACKED_TOOL_RESULT_MARKERS = (
    "已验证",
    "验证不可用",
    "返回",
    "超时",
    "不响应",
    "未配置",
    "不可用",
    "tool call",
    "tool result",
    "timed out",
    "timeout",
    "returned",
    "not configured",
    "failed with",
)

_NON_ASSERTIVE_CONTEXT_MARKERS = (
    "不得",
    "不要",
    "不能",
    "不应",
    "不会",
    "不调用",
    "无需调用",
    "需要时",
    "才会",
    "可以",
    "用于",
    "模式",
    "示例",
)


def _has_tool_evidence(tool_evidence_summary: dict[str, Any] | None) -> bool:
    if not isinstance(tool_evidence_summary, dict):
        return False
    return bool(tool_evidence_summary.get("has_tool_evidence"))


def _evidenced_tool_names(tool_evidence_summary: dict[str, Any] | None) -> set[str]:
    if not isinstance(tool_evidence_summary, dict):
        return set()
    return {str(name) for name in tool_evidence_summary.get("tool_names") or [] if str(name)}


def _mentions_tool(content: str, tool_name: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(tool_name)}(?![A-Za-z0-9_])", content))


def _looks_like_actual_tool_result_claim(content: str, tool_name: str) -> bool:
    match = re.search(rf"(?<![A-Za-z0-9_]){re.escape(tool_name)}(?![A-Za-z0-9_])", content)
    if not match:
        return False
    lowered = content.lower()
    if not any(marker in lowered or marker in content for marker in _UNBACKED_TOOL_RESULT_MARKERS):
        return False
    window_start = max(0, match.start() - 24)
    window_end = min(len(content), match.end() + 36)
    local_window = content[window_start:window_end].lower()
    if any(marker in local_window or marker in content[window_start:window_end] for marker in _NON_ASSERTIVE_CONTEXT_MARKERS):
        return False
    return True


def verify_final_answer_tool_evidence(
    content: str,
    *,
    available_tool_names: set[str],
    tool_evidence_summary: dict[str, Any] | None,
) -> str:
    """Guard final answers from claiming unobserved tool outcomes."""

    if not content or not available_tool_names:
        return content
    if _has_tool_evidence(tool_evidence_summary):
        evidenced = _evidenced_tool_names(tool_evidence_summary)
        if not evidenced:
            return content
        missing = [
            name
            for name in sorted(available_tool_names)
            if _mentions_tool(content, name)
            and name not in evidenced
            and _looks_like_actual_tool_result_claim(content, name)
        ]
    else:
        missing = [
            name
            for name in sorted(available_tool_names)
            if _looks_like_actual_tool_result_claim(content, name)
        ]
    if not missing:
        return content
    names = ", ".join(f"`{name}`" for name in missing[:8])
    return (
        "我不能确认刚才的工具状态：本轮没有实际工具调用记录，因此不能声称 "
        f"{names} 已返回、失败或超时。请重试该操作，我会先调用对应工具并基于工具结果给出结论。"
    )
