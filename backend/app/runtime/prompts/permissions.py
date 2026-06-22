"""Effective permission prompt rendering."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PermissionsPromptContext:
    approval_policy: str
    network_access: str
    writable_roots: list[str] = field(default_factory=list)
    denied_reads: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    denied_actions: list[str] = field(default_factory=list)
    request_permission_tool_enabled: bool = False


def _lines(label: str, values: list[str]) -> list[str]:
    if not values:
        return [f"{label}: none"]
    return [f"{label}:"] + [f"- {value}" for value in values]


def build_permissions_prompt(ctx: PermissionsPromptContext) -> str:
    lines = [
        "# Effective Runtime Permissions",
        f"approval_policy: {ctx.approval_policy}",
        f"network_access: {ctx.network_access}",
        f"request_permission_tool_enabled: {str(ctx.request_permission_tool_enabled).lower()}",
        *_lines("writable_roots", ctx.writable_roots),
        *_lines("denied_reads", ctx.denied_reads),
        *_lines("allowed_tools", ctx.allowed_tools),
        *_lines("denied_actions", ctx.denied_actions),
        "These instructions describe the current authority boundary; platform gates still enforce it mechanically.",
    ]
    return "\n".join(lines) + "\n"
