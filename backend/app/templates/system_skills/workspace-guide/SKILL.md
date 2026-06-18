---
name: Workspace Guide
description: "Use when you need to inspect, create, edit, or deliver workspace files while respecting managed-memory boundaries, file ownership, and artifact handoff rules."
tools:
  - read_document
  - read_file
  - write_file
  - edit_file
  - list_files
  - glob_search
  - grep_search
  - delete_file
  - run_command
  - tool_search
is_system: true
---

# Workspace Guide

<role>
Use this skill when you need to read or write files in your workspace, or
deliver results through a channel. The workspace is where your long-lived work
lives — chat history disappears after the session ends, but files in
`workspace/` and governed memory files persist and can be shared, referenced,
and built upon.
</role>

<when_to_use>
- You need to discover, read, write, or edit files under your agent workspace
- You need to create, update, or hand off workspace artifacts
- You need to deliver a file to the current channel's user
- You want to confirm workspace structure before creating new files
- You need to inspect enterprise-wide shared content under `enterprise_info/`
</when_to_use>

<do_not_use_when>
- You need to write to an integration system (Feishu docs, Confluence) — use the matching integration skill
- You need to write to an automatically-managed pipeline directory (`memory/learnings/`, `evolution/`, `logs/`) — the memory pipeline owns these, writing breaks consistency
- You just need a short conversational reply — not every response needs a file
</do_not_use_when>

## Tool Reference

<tool_reference>

### Workspace Structure

```
soul.md              — Your permanent identity (read-only, updated by dream)
HEARTBEAT.md         — Heartbeat curation protocol
relationships.md     — Your colleague list
tasks.json           — Read-only DB Task snapshot; use Work Ledger for cognitive todos

memory/
  t0/sessions/       — Append-only raw session ledger (T0, auto-generated)
  feedback.md        — User corrections and preferences (T3)
  knowledge.md       — Domain knowledge (T3)
  strategies.md      — Effective approaches (T3)
  blocked.md         — Failed approaches to avoid (T3)
  user.md            — User profile info (T3)
  learnings/         — Episodic learnings from conversations (T2, auto-managed)

evolution/
  scorecard.md       — Performance metrics
  blocklist.md       — Approaches proven impossible
  lineage.md         — Heartbeat/evolution history

logs/                — Legacy/import compatibility logs; not runtime T0 truth
skills/              — Your skill files
workspace/           — Your work files (reports, documents, artifacts)
  uploads/           — Files uploaded from chat or channels
  deep_research_reports/
                      — User-facing Deep Research report packets
  tool_results/      — Large tool outputs spilled to files
runtime_artifacts/   — Runtime recovery/audit artifacts; read only by explicit need
  traces/            — Compatibility invocation span JSONL; DB spans are canonical
enterprise_info/     — Shared company information
```

### File Operation Tools

Always use tools for file operations — tool results are the source of truth:

| Task | Tool |
|------|------|
| Discover files by pattern | `glob_search` |
| Search file contents | `grep_search` |
| Read a file | `read_file` |
| Write a new/overwrite file | `write_file` |
| Edit a specific range in a file | `edit_file` |
| Delete a workspace file after explicit confirmation | `delete_file` |
| Read a structured document (PDF/DOCX/XLSX) | `read_document` |
| Run a diagnostic shell command in the workspace | `run_command` |
| Search available tools/skills when the current toolset is unclear | `tool_search` |
| Send a file to the current channel's user | `send_channel_file` |

### Credential Boundary for `run_command`

- `run_command` works from `workspace/`. Files it creates land under `workspace/`
  and can be found with `list_files(path="workspace")`, `glob_search(root="workspace", pattern="**/*")`,
  or read with paths like `workspace/out.txt`.
- `run_command` is for workspace diagnostics, build/test commands, and local file-oriented checks.
- Do not inspect environment variables or use `run_command` to discover platform, provider, or channel credentials.
- If an integration tool reports auth/config failure, report the configuration gap from that tool; do not look for app IDs, secrets, tokens, or API keys in shell env.

### Messaging Tools

| Target | Tool |
|--------|------|
| Human user in the current channel (outbound message) | use the channel's outbound messaging tool — see the matching channel skill (`Feishu Integration`, etc.) |
| Another digital-employee agent | `send_message_to_agent` |
| The current channel (file delivery) | `send_channel_file` |

</tool_reference>

## Workflow

<workflows>

### Reading before writing
Verify before asserting: `read_file` before claiming a file's contents, `glob_search` or `list_files` before writing to check for existing paths.

### Finding platform-written artifacts

- Uploaded files are saved under `workspace/uploads/`.
- Deep Research copies user-facing artifacts to `workspace/deep_research_reports/<run_id>/`; internal recovery evidence remains under `runtime_artifacts/`.
- Oversized tool outputs can be written under `workspace/tool_results/`; read those files before relying on truncated inline output.
- Treat `runtime_artifacts/` as recovery/audit evidence, not the default deliverable folder. Prefer the mirrored `workspace/` path when one is provided by a tool result.

### Work Tracking

- When you discover follow-up work → create a trigger as a wake policy and classify it (`scheduled_job` / `event_wait` / `system_maintenance`).
- When you complete a task → record the outcome with concrete evidence in your work ledger and cancel obsolete triggers.

### File delivery

- **Send a file to the current channel user**: `send_channel_file(file_path="workspace/report.md")` — works across Feishu, Slack, Discord, and the web UI.
- File paths must be workspace-relative (e.g. `workspace/xxx.md`, not `/data/agents/xxx`).
- Files must exist before delivery — verify with `read_file` or `glob_search`.

### Attribution when messaging on behalf of someone
When sending a message on behalf of a third party, ALWAYS say who asked you:
> "Hi B, A asked me to let you know: the meeting has been moved to 3pm."

### Waiting for a reply
After sending a message and needing to wait for a response, set an
`on_message` trigger — use `reply_to_current_sender: true` and `max_fires: 1` to scope it
to the current sender, or `from_user_identity` / `from_agent_id` to
match a specific cross-session party. See the Trigger Management Guide
for full config details.

</workflows>

## Examples

<examples>

### Example A — Write a research brief and deliver it

Input: `把刚才整理的调研结果存成文件发给我`

Correct flow:
```
write_file(path="workspace/market-research-2026-04-16.md",
           content="# Market Research — 2026-04-16\n\n## Key findings\n...")
read_file(path="workspace/market-research-2026-04-16.md")  # verify
send_channel_file(file_path="workspace/market-research-2026-04-16.md")
```
Output: `已保存到 workspace/market-research-2026-04-16.md 并通过当前对话渠道发给你。`

### Example B — Schedule a future deliverable

Input: `下周帮我出一份季度总结`

Correct flow:
```
list_triggers()  # check for an existing equivalent wake policy
# Then load the Trigger Management Guide and set a `once` wake policy with
# trigger_class="scheduled_job"
```

### Example C — Do NOT write to managed directories

Input: `你直接把学到的东西存到 memory/learnings/ 里`

Correct response: `memory/learnings/ 是由记忆管道自动管理的，手动写入会和每次对话后的自动提取冲突并破坏数据。需要保存可以用 save_memory（给用户级偏好）或者写到 workspace/ 里做个 markdown 笔记。`

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Write directly to `memory/t0/`, `memory/learnings/`, `evolution/`, or `logs/`** → the automated memory pipeline manages these. Writing causes conflicts and data corruption. Use `save_memory` for explicit user-level preferences or write to `workspace/` for general notes.
- ❌ **Claim a file exists without verifying via `read_file` or `glob_search`** → the tool result is the source of truth; don't assert based on what you wrote earlier in the session (might have failed silently).
- ❌ **Use absolute paths** like `/data/agents/xxx` for channel file delivery → `send_channel_file` expects workspace-relative paths (`workspace/xxx`). Absolute paths either fail or leak internal infrastructure.
- ❌ **Hide durable work state in ad hoc scratch files** → durable state belongs in your work ledger or workspace artifacts; create triggers for active follow-up.
- ❌ **Overwrite an existing file without reading it first** → you may clobber prior work. Read before write, or use `edit_file` for a scoped update.
- ❌ **Forward a message without attribution** → target user can't tell who really asked. Always name the requester ("A asked me to...").
- ❌ **Invent tool names for operations the workspace doesn't support** → e.g. a fabricated `delete_*` or `move_*` tool when your current toolset doesn't include one. Use what's there; if no tool exists, ask the user or the admin.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every file claim (exists, contains X, was updated) is backed by a `read_file` or `glob_search` result in this session.
- Paths delivered via `send_channel_file` are workspace-relative and verified to exist first.
- Follow-up work is captured as a classified trigger; completed work is recorded with evidence in the work ledger.
- Automatically-managed directories (`memory/t0/`, `memory/learnings/`, `evolution/`, `logs/`) are never written to by this agent directly.
- Messages forwarded on behalf of someone else always name the original requester.
</success_criteria>

## Bundled Resources

Load resources by need, not by default:

- `references/workspace-boundary.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/workspace-artifact-plan.md`: use as the output scaffold when creating this artifact type.
