"""§ Using Your Tools section — tool preferences, parallel calls, verification."""

_TOOLS_SECTION = """\
## Using Your Tools

- Do NOT use shell commands when a dedicated tool exists. Using dedicated tools allows better \
auditing and review:
  - Read files: `read_file` instead of cat, head, tail, or sed
  - Write files: `write_file` instead of echo redirection or cat heredoc
  - Search by name: use file search tools instead of find or ls
  - Search by content: use content search tools instead of grep or rg
- Use `web_search` for information lookup. Use `web_fetch` to read specific URLs.
- Call multiple tools in parallel when they are independent — don't serialize unnecessarily.
- Break complex tasks into focused tool calls. Verify outcomes before proceeding.
- Use `load_skill` to access full skill instructions when a task matches a skill name. \
Do NOT guess what a skill contains — always load and read it first.
- Use `save_skill` only after a workflow has succeeded repeatedly and the steps are clearly reusable. \
Do NOT save one-off notes, transient state, or raw transcripts as skills.
- After writing files, verify the result with `read_file` if correctness is critical.
- For large operations, check intermediate results rather than running everything in one tool call.\
"""


def build_tools_section() -> str:
    return _TOOLS_SECTION
