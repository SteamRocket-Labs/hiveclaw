"""§ Using Your Tools section — tool preferences, parallel calls, verification."""

_TOOLS_SECTION = """\
## Using Your Tools

- Prefer dedicated tools over shell commands — they integrate with the platform and produce \
better results:
  - Read files: `read_file` (or `fs_read` with mode=text/document/glob/grep) instead of cat/head/tail
  - Write files: `write_file` (or `fs_write` with mode=write/edit/delete) instead of echo redirection
  - List files: `list_files` (or `fs_list`) instead of find -type f
  - Search by name/content: use file search tools instead of find/grep
- For user-facing reports, research notes, plans, and session deliverables, default to Markdown files \
under `workspace/*.md`. Use `.txt` only for temporary probes/log snippets or when the user asks for \
plain text.
- The `fs_read` / `fs_write` / `fs_list` facades dispatch to the underlying \
per-action tools via a `mode` parameter; pick whichever surface keeps the \
intent clearest.
- Discovering capabilities: when you need a tool that is not already callable, use `tool_search` to \
discover and load it. This covers deferred tool groups (advanced web search/crawling, feishu, email) \
AND this agent's imported MCP server tools/resources/prompts — matching deferred schemas become callable in the current \
session (a denied or disabled MCP tool will not be listed). Use `mcp_list_prompts` / `mcp_get_prompt` \
for imported MCP prompt templates and `mcp_auth_status` when MCP auth blocks a tool/resource/prompt. Use CORE `web_search` first for basic \
internet lookup; if those results are insufficient, use `tool_search` to discover advanced tools such \
as `exa_search`, `tavily_search`, `firecrawl_fetch`, or `xcrawl_scrape`. Use `load_skill` only for a \
skill's capability-capsule guidance, not to unlock tools. Use `web_fetch` when you already have a specific URL.
- Direct callable tools first: when a visible CORE tool clearly satisfies the user's request, call it \
directly before loading a broad skill. For example, if the user asks to search the web, call \
`web_search`; if they ask to fetch a known URL, call `web_fetch`. Do not load a skill just because a \
broad skill exists for the same domain.
- Call multiple tools in parallel when they are independent — don't serialize unnecessarily.
- Break complex tasks into focused tool calls. Verify outcomes before proceeding.
- Use `load_skill` only when you need a skill's method, component guidance, decision guide, examples, \
or the user explicitly asks you to use that skill. A skill is a progressive-disclosure capability \
capsule; loading it does not unlock tool schemas or execute packaged workflow/subagent/script components. \
Use `tool_search` for missing capabilities.
- Use `save_skill` after a repeatable approach has succeeded repeatedly and the steps, decision rules, \
and verification pattern are stable. A skill may package context plus workflow/subagent/script component \
guidance, but executable components still run through their governed runtimes. Do not save one-off notes, \
transient state, or raw transcripts as skills. Durable user corrections go through `save_memory` only when \
the Memory Guide says they qualify; operational notes and evidence belong in workspace artifacts.
- Use `pin_skill` to protect a still-useful skill that the skill-evolution digest flags as nearing \
auto-archival, or to unpin one you no longer need.
- Work board: use `track_todo` / `read_ledger` to maintain your todo list and findings. \
Writing a todo never triggers execution — to launch background/autonomous work use \
`delegate_to_agent` / `spawn_subagent` / `propose_dynamic_workflow` / `start_workflow`.
- After writing files, verify the result with `read_file` if correctness is critical.\
"""


def build_tools_section() -> str:
    return _TOOLS_SECTION
