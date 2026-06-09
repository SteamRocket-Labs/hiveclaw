"""§ Using Your Tools section — tool preferences, parallel calls, verification."""

_TOOLS_SECTION = """\
## Using Your Tools

- Prefer dedicated tools over shell commands — they integrate with the platform and produce \
better results:
  - Read files: `read_file` (or `fs_read` with mode=text/document/glob/grep) instead of cat/head/tail
  - Write files: `write_file` (or `fs_write` with mode=write/edit/delete) instead of echo redirection
  - List files: `list_files` (or `fs_list`) instead of find -type f
  - Search by name/content: use file search tools instead of find/grep
- The `fs_read` / `fs_write` / `fs_list` facades dispatch to the underlying \
per-action tools via a `mode` parameter; pick whichever surface keeps the \
intent clearest.
- Discovering capabilities: when you need a tool that is not already callable, use `tool_search` to \
discover and load it. This covers integration packs (web, feishu, email), `web_search` for internet \
lookup, AND this agent's imported MCP server tools — matching deferred schemas become callable in \
the current session (a denied or disabled MCP tool will not be listed). Use `load_skill` only for a \
skill's method guide, not to unlock tools. Use `web_fetch` when you already have a specific URL.
- Call multiple tools in parallel when they are independent — don't serialize unnecessarily.
- Break complex tasks into focused tool calls. Verify outcomes before proceeding.
- Use `load_skill` to access full skill instructions when a task matches a skill name — \
always load and read it first so you have the complete workflow.
- Use `save_skill` after a workflow has succeeded repeatedly and the steps are clearly reusable. \
Do not save one-off notes, transient state, or raw transcripts as skills. Durable user corrections \
go through `save_memory` only when the Memory Guide says they qualify; operational notes and \
evidence belong in workspace artifacts.
- Use `pin_skill` to protect a still-useful skill that the skill-evolution digest flags as nearing \
auto-archival, or to unpin one you no longer need.
- Work board: use `track_todo` / `read_ledger` to maintain your todo list and findings. \
Writing a todo never triggers execution — to launch background/autonomous work use \
`delegate_to_agent` / `spawn_subagent` / `start_workflow`.
- After writing files, verify the result with `read_file` if correctness is critical.\
"""


def build_tools_section() -> str:
    return _TOOLS_SECTION
