"""Optional coding pack manifest.

Cloud core never executes these capabilities directly. The manifest is the
single source used by taxonomy, command registry, and command execution
contracts to route coding-only features through Local Bridge / coding plugin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CodingPackCommandSpec:
    name: str
    description: str
    handler_ref: str
    tools: tuple[str, ...]
    input_schema: dict[str, Any] | None = None
    aliases: tuple[str, ...] = ()


CODING_PACK_NAME = "coding"
CODING_PACK_PERMISSION_MODE = "coding_pack"
CODING_PACK_SOURCE = "local_bridge"
CODING_PACK_TOOLS: tuple[str, ...] = (
    "lsp_symbol_search",
    "lsp_references",
    "worktree_create",
    "worktree_remove",
    "notebook_edit",
    "notebook_view",
    "persistent_shell_exec",
    "persistent_shell_state",
    "powershell_exec",
    "browser_ui_open",
    "browser_ui_snapshot",
    "browser_qa_run",
    "local_terminal_state",
)

CODING_PACK_COMMANDS: tuple[CodingPackCommandSpec, ...] = (
    CodingPackCommandSpec(
        name="worktree_enter",
        description="Enter a governed coding worktree context.",
        handler_ref="coding_pack:worktree_enter",
        tools=("worktree_create",),
    ),
    CodingPackCommandSpec(
        name="worktree_exit",
        description="Leave the current governed coding worktree context.",
        handler_ref="coding_pack:worktree_exit",
        tools=("worktree_remove",),
    ),
    CodingPackCommandSpec(
        name="diff",
        description="Inspect workspace changes through the governed coding pack.",
        handler_ref="coding_pack:diff",
        tools=("worktree_create", "local_terminal_state"),
    ),
    CodingPackCommandSpec(
        name="commit",
        description="Create a governed source-control commit.",
        handler_ref="coding_pack:commit",
        tools=("local_terminal_state",),
    ),
    CodingPackCommandSpec(
        name="commit_push_pr",
        description="Create a governed commit, push, and pull request.",
        handler_ref="coding_pack:commit_push_pr",
        tools=("local_terminal_state",),
    ),
    CodingPackCommandSpec(
        name="pr_comments",
        description="Read or address governed pull request comments.",
        handler_ref="coding_pack:pr_comments",
        tools=("local_terminal_state",),
    ),
    CodingPackCommandSpec(
        name="github_review",
        description="Run governed GitHub review workflows.",
        handler_ref="coding_pack:github_review",
        tools=("local_terminal_state",),
    ),
    CodingPackCommandSpec(
        name="review",
        description="Run a governed code review command.",
        handler_ref="coding_pack:review",
        tools=("lsp_symbol_search", "lsp_references", "local_terminal_state"),
    ),
    CodingPackCommandSpec(
        name="security_review",
        description="Run a governed security review command.",
        handler_ref="coding_pack:security_review",
        tools=("lsp_symbol_search", "lsp_references", "local_terminal_state"),
    ),
    CodingPackCommandSpec(
        name="lsp",
        description="Query governed language-server diagnostics.",
        handler_ref="coding_pack:lsp",
        tools=("lsp_symbol_search", "lsp_references"),
    ),
    CodingPackCommandSpec(
        name="notebook",
        description="Open a governed notebook-style coding workspace.",
        handler_ref="coding_pack:notebook",
        tools=("notebook_view", "notebook_edit"),
    ),
    CodingPackCommandSpec(
        name="shell_pack",
        description="Use governed shell-pack tasks through the code execution provider.",
        handler_ref="coding_pack:shell_pack",
        tools=("persistent_shell_exec", "persistent_shell_state"),
    ),
    CodingPackCommandSpec(
        name="persistent_shell",
        description="Run a governed persistent local shell session.",
        handler_ref="coding_pack:persistent_shell",
        tools=("persistent_shell_exec", "persistent_shell_state"),
    ),
    CodingPackCommandSpec(
        name="powershell",
        description="Run governed PowerShell tasks through the local coding plugin.",
        handler_ref="coding_pack:powershell",
        tools=("powershell_exec", "persistent_shell_state"),
    ),
    CodingPackCommandSpec(
        name="browser_ui",
        description="Open or inspect a governed local browser UI session.",
        handler_ref="coding_pack:browser_ui",
        tools=("browser_ui_open", "browser_ui_snapshot"),
    ),
    CodingPackCommandSpec(
        name="browser_qa",
        description="Run governed browser QA through the local coding plugin.",
        handler_ref="coding_pack:browser_qa",
        tools=("browser_ui_snapshot", "browser_qa_run"),
    ),
)

CODING_PACK_COMMAND_NAMES: frozenset[str] = frozenset(command.name for command in CODING_PACK_COMMANDS)
_COMMANDS_BY_NAME: dict[str, CodingPackCommandSpec] = {command.name: command for command in CODING_PACK_COMMANDS}


def coding_pack_command_spec(name: str) -> CodingPackCommandSpec | None:
    return _COMMANDS_BY_NAME.get(str(name or "").strip())


def coding_pack_command_manifest(name: str) -> dict[str, Any]:
    spec = coding_pack_command_spec(name)
    if spec is None:
        return {}
    return {
        "name": spec.name,
        "handler_ref": spec.handler_ref,
        "tools": list(spec.tools),
        "source": CODING_PACK_SOURCE,
        "requires_local_bridge": True,
        "permission_mode": CODING_PACK_PERMISSION_MODE,
    }
