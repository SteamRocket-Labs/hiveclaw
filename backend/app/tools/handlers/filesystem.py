"""Filesystem tools — workspace file I/O, search, code execution."""

from __future__ import annotations

from pathlib import Path

from app.tools.decorator import ToolMeta, tool


# -- list_files ---------------------------------------------------------------

@tool(ToolMeta(
    name="list_files",
    description="List files and folders in a directory within my workspace. Can also list enterprise_info/ for shared company information.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path to list, defaults to root (empty string). e.g.: '', 'skills', 'workspace', 'enterprise_info', 'enterprise_info/knowledge_base'",
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
))
def list_files(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _list_files
    return _list_files(workspace, arguments.get("path", ""), tenant_id)


# -- read_file ----------------------------------------------------------------

@tool(ToolMeta(
    name="read_file",
    description=(
        "Read file contents from the workspace.\n\n"
        "Usage:\n"
        "- Common files: soul.md (personality), memory/feedback.md or memory/knowledge.md (memory), focus.md (current priorities), "
        "tasks.json (tasks), skills/*.md (skill files), enterprise_info/ (shared company info)\n"
        "- For large files, the output may be truncated. Check if the result ends with a truncation marker.\n"
        "- You can read office documents (PDF, Word, Excel) via the separate `read_document` tool.\n"
        "- If the file does not exist, an error will be returned — this is normal, do not retry.\n"
        "- Prefer reading a file before editing it with `edit_file` to understand its current contents."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, e.g.: tasks.json, soul.md, memory/knowledge.md, skills/xxx.md, enterprise_info/company_profile.md",
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
))
def read_file(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _read_file
    return _read_file(workspace, arguments.get("path", ""), tenant_id)


# -- write_file ---------------------------------------------------------------

@tool(ToolMeta(
    name="write_file",
    description=(
        "Write or create a file in the workspace.\n\n"
        "Usage:\n"
        "- For modifying existing files, prefer `edit_file` instead — it only changes a specific snippet "
        "without rewriting the entire file, which is safer and preserves content you didn't intend to change.\n"
        "- Use `write_file` when creating new files or when the entire file content needs to be replaced.\n"
        "- Common targets: memory/knowledge.md, focus.md, workspace/*.md (reports/documents), skills/*.md (new skills)\n"
        "- Protected paths: soul.md can be written but should only be modified carefully as it defines your personality.\n"
        "- This tool overwrites the file completely — if you only need to change part of a file, use `edit_file`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, e.g.: memory/knowledge.md, workspace/report.md, skills/data_analysis.md",
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
    governance="sensitive",
    adapter="workspace_args",
))
def write_file(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _write_file
    return _write_file(workspace, arguments.get("path", ""), arguments.get("content", ""))


# -- edit_file ----------------------------------------------------------------

@tool(ToolMeta(
    name="edit_file",
    description=(
        "Edit an existing text file by replacing a specific text snippet.\n\n"
        "Usage:\n"
        "- You SHOULD read the file with `read_file` first to understand its current contents before editing.\n"
        "- The `old_text` must be an exact match of text currently in the file — including whitespace and newlines.\n"
        "- The edit will FAIL if `old_text` is not found or matches multiple locations. Provide enough surrounding "
        "context to make your match unique, or use `replace_all: true` to change every occurrence.\n"
        "- Prefer this over `write_file` for existing files — it only changes what you specify, preserving the rest."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to edit, e.g. workspace/report.md or focus.md",
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
    adapter="workspace_args",
))
def edit_file(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _edit_file
    return _edit_file(
        workspace,
        arguments.get("path", ""),
        arguments.get("old_text", ""),
        arguments.get("new_text", ""),
        arguments.get("replace_all", False),
    )


# -- glob_search --------------------------------------------------------------

@tool(ToolMeta(
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
))
def glob_search(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _glob_search
    return _glob_search(workspace, arguments.get("pattern", ""), arguments.get("root", ""))


# -- grep_search --------------------------------------------------------------

@tool(ToolMeta(
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
                "description": "Maximum number of matches to return",
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
))
def grep_search(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _grep_search
    return _grep_search(
        workspace,
        arguments.get("pattern", ""),
        arguments.get("root", ""),
        arguments.get("max_results", 50),
    )


# -- delete_file --------------------------------------------------------------

@tool(ToolMeta(
    name="delete_file",
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
    governance="sensitive",
    adapter="workspace_args",
))
def delete_file(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _delete_file
    return _delete_file(workspace, arguments.get("path", ""))


# -- read_document ------------------------------------------------------------

@tool(ToolMeta(
    name="read_document",
    description="Read office document contents (PDF, Word, Excel, PPT, etc.) and extract text. Suitable for reading knowledge base documents.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Document file path, e.g.: workspace/knowledge_base/report.pdf, enterprise_info/knowledge_base/policy.docx",
            }
        },
        "required": ["path"],
    },
    category="filesystem",
    display_name="Read Document",
    icon="\U0001f4d1",
    read_only=True,
    parallel_safe=True,
    governance="safe",
    adapter="workspace_args",
))
async def read_document(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _read_document
    return await _read_document(workspace, arguments.get("path", ""), tenant_id=tenant_id)


# -- execute_code -------------------------------------------------------------

@tool(ToolMeta(
    name="execute_code",
    description=(
        "Execute code (Python, Bash, or Node.js) in a sandboxed environment within your workspace directory.\n\n"
        "Usage:\n"
        "- Working directory is your workspace/ — file paths in code are relative to it.\n"
        "- Python: standard libraries available (json, csv, math, re, collections, pathlib, etc.).\n"
        "- Default timeout: 30 seconds (max 60). Long-running code will be killed.\n"
        "- SECURITY: No network access, no system-level operations (rm -rf, chmod, etc.). "
        "Never include credentials, API keys, or user secrets in code.\n"
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
    adapter="workspace_args",
))
async def execute_code(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.code_exec import _execute_code
    return await _execute_code(workspace, arguments)


# -- run_command --------------------------------------------------------------

@tool(ToolMeta(
    name="run_command",
    description=(
        "Run a shell command inside the cloud container and the agent workspace directory.\n\n"
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
    governance="sensitive",
    adapter="workspace_args",
))
async def run_command(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.code_exec import _run_command
    return await _run_command(workspace, arguments)
