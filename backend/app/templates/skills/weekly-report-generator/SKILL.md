---
name: Weekly Report Generator
description: "Use when you need to turn project notes, work logs, task updates, or operational evidence into a weekly report with accomplishments, blockers, risks, metrics, and next steps."
license: Proprietary
tools:
  - read_file
  - write_file
  - execute_code
  - send_channel_file
metadata:
  hive.version: 0.1.0
  hive.pack: office_pack
  hive.locale: cloud
  hive.invocation: both
---

# Weekly Report Generator

<role>
Use this skill when the user asks for a weekly report, project update, IC
update, team summary, or executive status note. Produce a structured report
artifact when requested, using `read_file` for provided inputs, `execute_code`
for deterministic rendering, `write_file` for the final report, and
`send_channel_file` when delivery is needed.
</role>

<when_to_use>
- User asks for a weekly or periodic status report
- User provides activity logs, notes, tickets, or meeting fragments
- User wants an executive summary plus risks, blockers, and next actions
</when_to_use>

<anti_patterns>
- Do not invent progress, metrics, owners, or dates that are not in the inputs.
- Do not bury blockers; surface them in a dedicated section.
- Do not over-format a simple status update when Markdown is enough.
</anti_patterns>

<examples>
Input: "用这些 notes 生成本周项目周报，并导出 md"

Output: `write_file(path="weekly-report.md", content="...")` with sections for
summary, shipped work, risks, blockers, next week, and sources.
</examples>

## Workflow

1. Read the supplied notes with `read_file`.
2. Normalize events into shipped work, active work, blockers, risks, and asks.
3. Render Markdown directly or use `execute_code` for templated DOCX output.
4. Save via `write_file`; deliver with `send_channel_file` only if requested.

## Bundled Resources

Load resources by need, not by default:

- `references/tone-guide.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/weekly-report.md`: use as the output scaffold when creating this artifact type.

## Quality Bar

- Do not invent facts, owners, dates, recipients, source evidence, or external system state.
- Prefer deterministic scripts or templates when the skill bundles them for this workflow.
- Keep the final output focused on the artifact or decision the user requested.
- Surface missing credentials, unavailable tools, stale data, and unsupported claims as blockers instead of silently working around them.
