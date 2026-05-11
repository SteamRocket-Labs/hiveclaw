---
name: Office Productivity
description: Single office workflow entrypoint for DOCX, XLSX, PPTX, PDF, meeting minutes, weekly reports, and pitch decks.
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
  version: "0.2"
  category: productivity
  hive.pack: office_pack
---

# Office Productivity

Use this skill as the only office entrypoint. Pick one internal mode:

- `docx`: create, fill, inspect, or edit Word documents.
- `xlsx`: read, analyze, create, or edit spreadsheets.
- `pptx`: create, inspect, or edit presentations.
- `pdf`: create print-ready PDFs or fill PDF forms.
- `meeting_minutes`: extract decisions, action items, risks, and owners.
- `weekly_report`: summarize work, blockers, risks, and next steps.
- `pitch_deck`: build an investor or board deck outline and artifact.

## Routing

1. Read supplied files first with `read_file` or `read_document`.
2. Use deterministic `execute_code` for artifact generation or structured edits.
3. Save outputs under the workspace with `write_file` or the script itself.
4. Use Feishu document or sheet tools only when the user provides a Feishu URL/token or asks for Feishu-native output.
5. Deliver files with `send_channel_file` only when the user asks for delivery.

## Quality Bar

- Do not invent metrics, attendees, owners, due dates, or company facts.
- Prefer one narrow render/edit pass over long tutorial-style reasoning.
- Keep artifact type choices explicit when a request could be DOCX, PDF, XLSX, or PPTX.
