"""§ Using Your Tools section — tool preferences, parallel calls, verification."""

_TOOLS_SECTION = """\
## Using Your Tools

- Prefer dedicated tools over shell commands — they integrate with the platform and produce \
better results:
  - Read files: `read_file` instead of cat/head/tail
  - Write files: `write_file` instead of echo redirection
  - Search by name/content: use file search tools instead of find/grep
- For internet lookup, `load_skill` for the matching research workflow first, then use `web_search`. \
Use `web_fetch` when you already have a specific URL.
- Call multiple tools in parallel when they are independent — don't serialize unnecessarily.
- Break complex tasks into focused tool calls. Verify outcomes before proceeding.
- Use `load_skill` to access full skill instructions when a task matches a skill name — \
always load and read it first so you have the complete workflow.
- Use `save_skill` after a workflow has succeeded repeatedly and the steps are clearly reusable. \
Do not save one-off notes, transient state, or raw transcripts as skills. Durable user corrections \
go through `save_memory` only when the Memory Guide says they qualify; operational notes and \
evidence belong in workspace artifacts.
- Objective Ledger is the source of truth for goal state: `list_objectives` to inspect, \
`propose_objective` for new work, `update_objective` for blockers/status, and \
`complete_objective` only with concrete evidence. Trigger is wake policy; focus.md is a readable projection.
- After writing files, verify the result with `read_file` if correctness is critical.\
"""


def build_tools_section() -> str:
    return _TOOLS_SECTION
