---
name: Office Productivity
description: "Use when Codex needs one entrypoint for office artifacts, including DOCX, XLSX, PPTX, PDF, meeting minutes, weekly reports, pitch decks, and delivery workflows."
license: Proprietary
tools:
  - read_file
  - read_document
  - list_files
  - write_file
  - edit_file
  - execute_code
  - send_channel_file
  - send_email
  - read_emails
  - reply_email
  - feishu_doc_read
  - feishu_doc_create
  - feishu_doc_append
  - feishu_doc_share
  - feishu_sheet_info
  - feishu_sheet_read
metadata:
  version: '0.2'
  category: productivity
  hive.pack: office_pack
---

# Office Productivity

<role>
Use when Codex needs one entrypoint for office artifacts, including DOCX, XLSX, PPTX, PDF, meeting minutes, weekly reports, pitch decks, and delivery workflows.
</role>

<when_to_use>
- The user asks for Office Productivity output or a closely related workflow.
- The task requires the declared tools, bundled references, or templates in this skill.
- The result must be reusable, source-aware, or artifact-shaped rather than a short ad hoc answer.
</when_to_use>

## Operating Procedure

1. Identify the requested artifact type: DOCX, XLSX, PPTX, PDF, meeting minutes, weekly report, or pitch deck.
2. Read supplied source files first and choose the narrowest internal mode that can complete the artifact.
3. Use deterministic generation or editing paths for structured files; avoid free-form prose when a template or script exists.
4. Save outputs under the workspace and deliver them only when the user asks for channel delivery.
5. Flag missing inputs, invented facts, rendering risks, and manual review needs before final handoff.

## Quality Bar

- Do not invent facts, owners, dates, recipients, source evidence, or external system state.
- Prefer deterministic scripts or templates when the skill bundles them for this workflow.
- Keep the final output focused on the artifact or decision the user requested.
- Surface missing credentials, unavailable tools, stale data, and unsupported claims as blockers instead of silently working around them.

<anti_patterns>
- Do not treat a search result, filename, or prior memory as proof without reading the underlying source or file.
- Do not load every reference file by default; use progressive disclosure and read only the relevant resource.
- Do not call destructive or externally visible tools unless the user asked for that action and required confirmation is satisfied.
</anti_patterns>

<examples>
- Input: "Create the requested artifact from these notes." Output: inspect the inputs, load the relevant template/reference, call the declared tools, save the artifact, and report validation notes.
- Input: "Check whether this is safe / supported / current." Output: gather evidence first, classify unsupported or stale claims, and give a direct recommendation with source or file references.
</examples>

## Bundled Resources

Load resources by need, not by default:

- `references/playbook.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/report.md`: use as the output scaffold when creating this artifact type.
