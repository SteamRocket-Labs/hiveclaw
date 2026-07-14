"""Filesystem tools — workspace file I/O, search, code execution."""

from __future__ import annotations

import inspect
from pathlib import Path

from app.tools.decorator import RESULT_CHARS_UNLIMITED, ToolMeta, tool
from app.tools.result_envelope import ToolContentEnvelope, render_tool_error


async def _maybe_await_tool_result(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _call_with_optional_authority(handler, workspace, arguments, tenant_id, authority_scope):
    """Preserve the public three-argument handler contract outside governed runtime calls."""
    if authority_scope is None:
        return handler(workspace, arguments, tenant_id)
    return handler(
        workspace,
        arguments,
        tenant_id,
        authority_scope=authority_scope,
    )


# -- list_files ---------------------------------------------------------------


@tool(
    ToolMeta(
        name="list_files",
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description="List files and folders in a directory within my workspace. Can also list enterprise_info/ for shared company information.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list, defaults to root (empty string). e.g.: '', 'skills', 'workspace', 'enterprise_info'",
                }
            },
        },
        category="filesystem",
        display_name="List Files",
        icon="\U0001f4c2",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="workspace_args",
    )
)
def list_files(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    from app.services.agent_tool_domains.workspace import _list_files

    return _list_files(workspace, arguments.get("path", ""), tenant_id, authority_scope=authority_scope)


# -- read_file ----------------------------------------------------------------


@tool(
    ToolMeta(
        name="read_file",
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Read file contents from the workspace.\n\n"
            "Usage:\n"
            "- Common files: soul.md (personality), memory/self/self.md (self-model), memory/profiles/owner.md, "
            "memory/knowledge/<concept>.md, memory/milestones/<slug>.md, memory/explicit/MEMORY.md (memory), "
            "tasks.json (tasks), skills/<slug>/SKILL.md (skill files), enterprise_info/ (shared company info)\n"
            "- File semantics are returned completely; oversized transport payloads are preserved in a recoverable workspace artifact.\n"
            "- You can read office documents (PDF, Word, Excel) via the separate `read_document` tool.\n"
            "- If the file does not exist, an error will be returned — this is normal, do not retry.\n"
            "- Prefer reading a file before editing it with `edit_file` to understand its current contents."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, e.g.: tasks.json, soul.md, memory/self/self.md, skills/my-skill/SKILL.md, enterprise_info/company_profile.md",
                }
            },
            "required": ["path"],
        },
        category="filesystem",
        display_name="Read File",
        icon="\U0001f4c4",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="workspace_args",
    )
)
def read_file(
    workspace: Path,
    arguments: dict,
    tenant_id: str | None = None,
    authority_scope=None,
) -> "str | ToolContentEnvelope":
    from app.services.agent_tool_domains.workspace import _read_file

    return _read_file(workspace, arguments.get("path", ""), tenant_id, authority_scope=authority_scope)


# -- write_file ---------------------------------------------------------------


@tool(
    ToolMeta(
        name="write_file",
        description=(
            "Write or create a file in the workspace.\n\n"
            "Usage:\n"
            "- For modifying existing files, prefer `edit_file` instead — it only changes a specific snippet "
            "without rewriting the entire file, which is safer and preserves content you didn't intend to change.\n"
            "- Use `write_file` when creating new files or when the entire file content needs to be replaced.\n"
            "- Common deliverables default to Markdown under workspace/*.md (reports/documents). "
            "Use .txt only for temporary probes/log snippets or when the user explicitly requests plain text. "
            "Use `save_skill` for skill activation candidates, and the work ledger for durable goal state.\n"
            "- Governed paths: memory/, logs/, evolution/, and runtime_artifacts/ are platform-managed; direct writes there are refused.\n"
            "- Skill paths: skills/** are active skill directories; direct writes are refused and promotion must go through Skill Gate.\n"
            "- Protected paths: soul.md is updated only through Dream/Soul candidate promotion; direct writes are refused.\n"
            "- This tool overwrites the file completely — if you only need to change part of a file, use `edit_file`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, e.g.: workspace/report.md, skills/data-analysis/SKILL.md",
                },
                "content": {
                    "type": "string",
                    "description": "File content to write",
                },
            },
            "required": ["path", "content"],
        },
        category="filesystem",
        display_name="Write File",
        icon="\u270f\ufe0f",
        workspace_mutating=True,
        governance="sensitive",
        adapter="workspace_args",
    )
)
def write_file(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    from app.services.agent_tool_domains.workspace import _write_file

    return _write_file(
        workspace,
        arguments.get("path", ""),
        arguments.get("content", ""),
        authority_scope=authority_scope,
    )


# -- edit_file ----------------------------------------------------------------


@tool(
    ToolMeta(
        name="edit_file",
        description=(
            "Edit an existing text file by replacing a specific text snippet.\n\n"
            "Usage:\n"
            "- You SHOULD read the file with `read_file` first to understand its current contents before editing.\n"
            "- The `old_text` must be an exact match of text currently in the file — including whitespace and newlines.\n"
            "- The edit will FAIL if `old_text` is not found or matches multiple locations. Provide enough surrounding "
            "context to make your match unique, or use `replace_all: true` to change every occurrence.\n"
            "- Governed paths: memory/, logs/, evolution/, and runtime_artifacts/ are platform-managed; direct edits there are refused.\n"
            "- Skill paths: skills/** are active skill directories; direct edits are refused and promotion must go through Skill Gate.\n"
            "- Prefer this over `write_file` for existing files — it only changes what you specify, preserving the rest."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to edit, e.g. workspace/report.md",
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to replace",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "When true, replace all occurrences instead of exactly one",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
        category="filesystem",
        display_name="Edit File",
        icon="\u270f\ufe0f",
        workspace_mutating=True,
        adapter="workspace_args",
    )
)
def edit_file(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    from app.services.agent_tool_domains.workspace import _edit_file

    return _edit_file(
        workspace,
        arguments.get("path", ""),
        arguments.get("old_text", ""),
        arguments.get("new_text", ""),
        arguments.get("replace_all", False),
        authority_scope=authority_scope,
    )


# -- glob_search --------------------------------------------------------------


@tool(
    ToolMeta(
        name="glob_search",
        description="Find files by path pattern inside the workspace. Use this to discover candidate files before reading them.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern such as '**/*.md' or 'skills/*/SKILL.md'",
                },
                "root": {
                    "type": "string",
                    "description": "Optional workspace-relative root path to search from",
                },
            },
            "required": ["pattern"],
        },
        category="filesystem",
        display_name="Glob Search",
        icon="\U0001f50e",
        read_only=True,
        parallel_safe=True,
        adapter="workspace_args",
    )
)
def glob_search(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    from app.services.agent_tool_domains.workspace import _glob_search

    return _glob_search(
        workspace,
        arguments.get("pattern", ""),
        arguments.get("root", ""),
        authority_scope=authority_scope,
    )


# -- grep_search --------------------------------------------------------------


@tool(
    ToolMeta(
        name="grep_search",
        description="Search file contents for a text pattern inside the workspace. Prefer this before opening many files one by one.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The text or regex pattern to search for",
                },
                "root": {
                    "type": "string",
                    "description": "Optional workspace-relative root path to search from",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional explicit maximum. Omit it to return every authorized match.",
                },
            },
            "required": ["pattern"],
        },
        category="filesystem",
        display_name="Grep Search",
        icon="\U0001f50d",
        read_only=True,
        parallel_safe=True,
        adapter="workspace_args",
    )
)
def grep_search(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    from app.services.agent_tool_domains.workspace import _grep_search

    return _grep_search(
        workspace,
        arguments.get("pattern", ""),
        arguments.get("root", ""),
        arguments.get("max_results"),
        authority_scope=authority_scope,
    )


# -- delete_file --------------------------------------------------------------


@tool(
    ToolMeta(
        name="delete_file",
        destructive=True,
        description=(
            "Delete a file from the workspace. This is a DESTRUCTIVE operation — the file cannot be recovered.\n\n"
            "Usage:\n"
            "- Protected files (soul.md, tasks.json) cannot be deleted.\n"
            "- Before deleting, consider whether the user explicitly requested deletion.\n"
            "- If you need to clear file contents without deleting, use `write_file` with empty content instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to delete",
                }
            },
            "required": ["path"],
        },
        category="filesystem",
        display_name="Delete File",
        icon="\U0001f5d1",
        workspace_mutating=True,
        governance="sensitive",
        adapter="workspace_args",
    )
)
def delete_file(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    from app.services.agent_tool_domains.workspace import _delete_file

    return _delete_file(workspace, arguments.get("path", ""), authority_scope=authority_scope)


# -- read_document ------------------------------------------------------------


@tool(
    ToolMeta(
        name="read_document",
        timeout_seconds=60.0,
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description="Read office document contents (PDF, Word, Excel, PPT, etc.) and extract text. Suitable for reading knowledge base documents.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Document file path, e.g.: workspace/knowledge_base/report.pdf",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "fast", "ocr", "layout", "vision"],
                    "description": "Conversion mode. Default auto.",
                },
                "max_pages": {
                    "type": "integer",
                    "description": "Optional page cap for conversion engines that support it.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional explicit character cap when return_format=preview; omit for complete content.",
                },
                "force_refresh": {
                    "type": "boolean",
                    "description": "Rebuild the Markdown artifact instead of reusing the cached conversion.",
                },
                "return_format": {
                    "type": "string",
                    "enum": ["preview", "markdown", "metadata", "pages"],
                    "description": "Return preview plus artifact paths, full markdown, metadata, or page artifact listing. Default markdown (complete content).",
                },
            },
            "required": ["path"],
        },
        category="filesystem",
        display_name="Read Document",
        icon="\U0001f4d1",
        read_only=True,
        parallel_safe=True,
        workspace_mutating=True,
        governance="safe",
        adapter="workspace_args",
    )
)
async def read_document(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    from app.services.agent_tool_domains.workspace import _read_document

    raw_max_chars = arguments.get("max_chars")
    if raw_max_chars is None:
        max_chars = None
    else:
        try:
            max_chars = max(1, int(raw_max_chars))
        except (TypeError, ValueError):
            return "[Error] max_chars must be a positive integer when provided."
    return await _read_document(
        workspace,
        arguments.get("path", ""),
        max_chars=max_chars,
        tenant_id=tenant_id,
        mode=str(arguments.get("mode") or "auto"),
        max_pages=arguments.get("max_pages"),
        force_refresh=bool(arguments.get("force_refresh", False)),
        return_format=str(arguments.get("return_format") or "markdown"),
        authority_scope=authority_scope,
    )


# -- execute_code -------------------------------------------------------------


@tool(
    ToolMeta(
        name="execute_code",
        timeout_seconds=120.0,
        risk_class="code_execution",
        description=(
            "Execute code (Python, Bash, or Node.js) in a sandboxed environment within your workspace directory.\n\n"
            "Usage:\n"
            "- Working directory is your workspace/ — file paths in code are relative to it.\n"
            "- Python: standard libraries available (json, csv, math, re, collections, pathlib, etc.).\n"
            "- Default timeout: 30 seconds (max 60). Long-running code will be killed.\n"
            "- SECURITY: governed sandbox providers default to deny-all network and block system-level operations "
            "(rm -rf, chmod, etc.); any explicit network policy is platform-controlled. Never include credentials, "
            "API keys, or user secrets in code.\n"
            "- If code fails, read the error output carefully before retrying — fix the root cause, "
            "do not blindly retry the same code.\n"
            "- For file operations, prefer dedicated tools (read_file, write_file) over code-based I/O."
        ),
        parameters={
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": ["python", "bash", "node"],
                    "description": "Programming language to execute",
                },
                "code": {
                    "type": "string",
                    "description": "Code to execute. For Python, you can import standard libraries (json, csv, math, re, collections, etc.). Working directory is your workspace/.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max execution time in seconds (default 30, max 60)",
                },
            },
            "required": ["language", "code"],
        },
        category="filesystem",
        display_name="Execute Code",
        icon="\u25b6\ufe0f",
        workspace_mutating=True,
        adapter="workspace_args",
    )
)
async def execute_code(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    from app.services.agent_tool_domains.code_exec import (
        WorkspaceExecutionAuthorityError,
        authorized_execution_workspace,
    )
    from app.services.agent_tools import _execute_code

    try:
        with authorized_execution_workspace(workspace, authority_scope) as execution_workspace:
            return await _execute_code(
                execution_workspace,
                arguments,
                canonical_workspace=workspace,
            )
    except WorkspaceExecutionAuthorityError as exc:
        return render_tool_error(
            tool_name="execute_code",
            error_class=exc.code,
            message=exc.message,
            provider="resource_authority",
            retryable=False,
            actionable_hint="Use a new path in your workspace or request an explicit resource grant.",
        )


# -- run_command --------------------------------------------------------------


@tool(
    ToolMeta(
        name="run_command",
        timeout_seconds=120.0,
        risk_class="code_execution",
        description=(
            "Run a shell command inside the configured sandbox provider and the agent workspace directory.\n\n"
            "Usage:\n"
            "- Working directory is your workspace/ inside the container.\n"
            "- Use this for local project commands such as `git status`, `pytest`, `npm test`, `node script.js`, or `python3 script.py`.\n"
            "- Prefer this over `execute_code` when you need normal shell tooling rather than embedding a short script.\n"
            "- This is NOT a general admin shell: dangerous system commands, package manager commands, docker/kubernetes commands, and direct network fetch commands are blocked.\n"
            "- Keep commands non-interactive and scoped to the current task."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run in the workspace, e.g. 'git status' or 'pytest -q'",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max execution time in seconds (default 60, max 120)",
                },
            },
            "required": ["command"],
        },
        category="filesystem",
        display_name="Run Command",
        icon="\U0001f4bb",
        workspace_mutating=True,
        governance="sensitive",
        adapter="workspace_args",
    )
)
async def run_command(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    from app.services.agent_tool_domains.code_exec import (
        WorkspaceExecutionAuthorityError,
        authorized_execution_workspace,
    )
    from app.services.agent_tools import _run_command

    try:
        with authorized_execution_workspace(workspace, authority_scope) as execution_workspace:
            return await _run_command(execution_workspace, arguments)
    except WorkspaceExecutionAuthorityError as exc:
        return render_tool_error(
            tool_name="run_command",
            error_class=exc.code,
            message=exc.message,
            provider="resource_authority",
            retryable=False,
            actionable_hint="Use a new path in your workspace or request an explicit resource grant.",
        )


# ── P1-W3-6 — Unified filesystem facades ─────────────────────────
# Audit observation: nine fine-grained tools (list/read/write/edit/
# delete/glob/grep/read_document/execute_code) overload the LLM's tool
# selection step. The three facades below dispatch to the existing
# implementations via a `mode` parameter, so an LLM that follows the
# rubric in `tools` section can choose one tool then narrow by mode.
# Old per-action tools stay registered for backwards compatibility —
# new agent prompts can ship with only the facades on day one.


@tool(
    ToolMeta(
        name="fs_read",
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Unified filesystem read. Pick a `mode`:\n"
            "  - `text` (default): plain text / markdown / json — same as read_file\n"
            "  - `document`: PDF / Word / Excel — same as read_document\n"
            "  - `glob`: list paths matching a pattern — same as glob_search\n"
            "  - `grep`: search file contents for a regex — same as grep_search\n"
            "Always provide `path` (file path or starting directory). For grep also "
            "provide `pattern`; for glob, `pattern` defaults to '*'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["text", "document", "glob", "grep"],
                    "description": "Read mode (default text)",
                },
                "path": {"type": "string", "description": "File or directory path"},
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern or regex (used by glob/grep modes)",
                },
            },
            "required": ["path"],
        },
        category="filesystem",
        display_name="Read (unified)",
        icon="\U0001f4d6",
        read_only=True,
        parallel_safe=True,
        workspace_mutating=True,
        governance="safe",
        adapter="workspace_args",
    )
)
def fs_read(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    mode = (arguments.get("mode") or "text").strip().lower()
    if mode == "text":
        return _call_with_optional_authority(
            read_file,
            workspace,
            {"path": arguments.get("path", "")},
            tenant_id,
            authority_scope,
        )
    if mode == "document":
        return _call_with_optional_authority(
            read_document,
            workspace,
            {"path": arguments.get("path", "")},
            tenant_id,
            authority_scope,
        )
    if mode == "glob":
        return _call_with_optional_authority(
            glob_search,
            workspace,
            {
                "root": arguments.get("path", ""),
                "pattern": arguments.get("pattern") or "*",
            },
            tenant_id,
            authority_scope,
        )
    if mode == "grep":
        return _call_with_optional_authority(
            grep_search,
            workspace,
            {
                "root": arguments.get("path", ""),
                "pattern": arguments.get("pattern") or "",
            },
            tenant_id,
            authority_scope,
        )
    return f"⚠️ fs_read: unknown mode {mode!r}. Use one of: text, document, glob, grep."


@tool(
    ToolMeta(
        name="fs_write",
        description=(
            "Unified filesystem write. Pick a `mode`:\n"
            "  - `write` (default): full replace — same as write_file\n"
            "  - `edit`: in-place edit — same as edit_file (needs `old_string`/`new_string`)\n"
            "  - `delete`: remove file — same as delete_file\n"
            "Always provide `path`. For `write`, also `content`. For `edit`, "
            "also `old_string` and `new_string`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["write", "edit", "delete"],
                    "description": "Write mode (default write)",
                },
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "New content (write mode)"},
                "old_string": {"type": "string", "description": "Existing snippet (edit mode)"},
                "new_string": {"type": "string", "description": "Replacement snippet (edit mode)"},
            },
            "required": ["path"],
        },
        category="filesystem",
        display_name="Write (unified)",
        icon="\U0001f4dd",
        workspace_mutating=True,
        governance="sensitive",
        adapter="workspace_args",
    )
)
async def fs_write(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    mode = (arguments.get("mode") or "write").strip().lower()
    if mode == "write":
        return await _maybe_await_tool_result(
            _call_with_optional_authority(
                write_file,
                workspace,
                {
                    "path": arguments.get("path", ""),
                    "content": arguments.get("content", ""),
                },
                tenant_id,
                authority_scope,
            )
        )
    if mode == "edit":
        return await _maybe_await_tool_result(
            _call_with_optional_authority(
                edit_file,
                workspace,
                {
                    "path": arguments.get("path", ""),
                    "old_text": arguments.get("old_string", ""),
                    "new_text": arguments.get("new_string", ""),
                },
                tenant_id,
                authority_scope,
            )
        )
    if mode == "delete":
        return await _maybe_await_tool_result(
            _call_with_optional_authority(
                delete_file,
                workspace,
                {"path": arguments.get("path", "")},
                tenant_id,
                authority_scope,
            )
        )
    return f"⚠️ fs_write: unknown mode {mode!r}. Use one of: write, edit, delete."


@tool(
    ToolMeta(
        name="fs_list",
        max_result_chars=RESULT_CHARS_UNLIMITED,
        description=(
            "Unified filesystem listing. Wraps list_files. Provide `path` "
            "(directory, defaults to root) to enumerate immediate children."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path (defaults to root)",
                },
            },
        },
        category="filesystem",
        display_name="List (unified)",
        icon="\U0001f5c2",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="workspace_args",
    )
)
def fs_list(workspace: Path, arguments: dict, tenant_id: str | None = None, authority_scope=None) -> str:
    return _call_with_optional_authority(
        list_files,
        workspace,
        {"path": arguments.get("path", "")},
        tenant_id,
        authority_scope,
    )
