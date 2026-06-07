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
- For internet lookup, use `tool_search` to discover `web_search` when it is not already callable; \
use `load_skill` only when you need a research method guide. Use `web_fetch` when you already have a specific URL.
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
- Objective Ledger is the source of truth for goal state: `list_objectives` to inspect, \
`propose_objective` for new work, `update_objective` for blockers/status, and \
`complete_objective` only with concrete evidence. Trigger is wake policy; focus.md is a readable projection.
- DB-backed task ledger is separate from objectives: use `list_tasks` and `get_task` \
to inspect tasks, and `manage_tasks` to create, update, or delete them. Do not edit tasks.json directly.
- After writing files, verify the result with `read_file` if correctness is critical.\
"""


def build_tools_section() -> str:
    return _TOOLS_SECTION
