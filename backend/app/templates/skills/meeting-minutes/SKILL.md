---
name: Meeting Minutes
description: "Use when you need to convert meeting notes, transcripts, or rough discussion fragments into concise minutes with decisions, action items, owners, risks, open questions, and follow-up artifacts."
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

# Meeting Minutes

<role>
Use this skill when the user provides meeting notes, a transcript, or raw
discussion fragments and wants clean minutes. Use `read_file` for source
material, `execute_code` only for deterministic parsing or rendering,
`write_file` for the minutes, and `send_channel_file` when delivery is needed.
</role>

<when_to_use>
- User asks for meeting minutes, decisions, action items, or follow-ups
- User provides transcript-like text or rough meeting notes
- User needs owners, due dates, unresolved questions, and risks extracted
</when_to_use>

<anti_patterns>
- Do not invent attendees, owners, decisions, or deadlines.
- Do not treat discussion as a decision unless the transcript clearly supports it.
- Do not omit unresolved questions or risks.
</anti_patterns>

<examples>
Input: "整理这个会议 transcript，输出纪要和 action items"

Output: `write_file(path="meeting-minutes.md", content="...")` with decisions,
actions, open questions, risks, and source notes.
</examples>

## Workflow

1. Read source notes with `read_file`.
2. Extract decisions, action items, owners, due dates, risks, and open questions.
3. Use `execute_code` if a structured transcript needs deterministic parsing.
4. Save with `write_file`; deliver with `send_channel_file` only if requested.

## Bundled Resources

Load resources by need, not by default:

- `references/extraction-rubric.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/minutes.md`: use as the output scaffold when creating this artifact type.

## Quality Bar

- Do not invent facts, owners, dates, recipients, source evidence, or external system state.
- Prefer deterministic scripts or templates when the skill bundles them for this workflow.
- Keep the final output focused on the artifact or decision the user requested.
- Surface missing credentials, unavailable tools, stale data, and unsupported claims as blockers instead of silently working around them.
