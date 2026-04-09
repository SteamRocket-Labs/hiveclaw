---
name: XLSX Processor
description: "Cloud-first spreadsheet routing skill. Use for local Excel/CSV analysis, workbook creation or edits, and Feishu Sheets reading. Prefer deterministic tools and short execution loops over long tutorial-style reasoning."
license: MIT
tools:
  - feishu_sheet_info
  - feishu_sheet_read
  - read_file
  - read_document
  - write_file
  - execute_code
  - send_channel_file
metadata:
  version: "3.0"
  category: productivity
---

# XLSX Processor

Handle spreadsheet work directly. Do **not** spawn sub-agents.

This skill is a **routing contract**, not a cookbook. Keep execution short, deterministic, and cloud-friendly.

## Use This Skill For

- Reading or summarizing local `.xlsx`, `.xlsm`, `.csv`, `.tsv`
- Creating a new workbook for the user
- Editing an existing workbook while preserving structure
- Reading Feishu Sheets content through Feishu tools
- Producing a spreadsheet report and returning the resulting file

## Do Not Use This Skill For

- Slide decks or visual presentations
- Rich document writing better handled as DOCX/PDF
- Database-style querying when plain text/CSV export is enough

## Routing

### 1. Feishu Sheets URL or token

Use:

1. `feishu_sheet_info` to discover worksheet IDs and titles
2. `feishu_sheet_read` to pull the needed range

Do **not** download or simulate the sheet manually if the Feishu tools can read it.

### 2. Local spreadsheet inspection

Use:

1. `read_document` or `read_file` for quick structure inspection
2. `execute_code` only when you need tabular analysis, validation, transformation, or workbook-level edits

### 3. Create a new workbook

Use `execute_code` to generate the workbook deterministically, save it into the workspace, then use `send_channel_file` when the user needs the artifact delivered.

### 4. Edit an existing workbook

Use `execute_code` with a minimal script that:

- opens the target file
- applies the requested edit only
- saves to the requested output path

Avoid broad rewrites when a narrow edit is enough.

## Required Inputs

- Source file path or Feishu sheet link/token
- Exact output path if the user wants a file artifact
- Clear target sheet/range when the request is about a subset of cells
- Formatting expectations if output fidelity matters

If one of these is missing, infer the safest default and state it briefly in the work log.

## Execution Rules

- Prefer the **smallest working script**.
- Read first before editing.
- Keep formulas unless the user explicitly asks for values only.
- Do not silently drop worksheets, formulas, merged cells, or number formats.
- If the request is only analysis, return findings directly instead of always generating a new file.
- If you create a new file, tell the user the exact output path.

## Success Criteria

- The requested workbook, range, or summary is produced correctly.
- File paths in the response match real workspace files.
- For edits, the original workbook structure remains intact unless the user asked to restructure it.
- For Feishu Sheets, the answer cites the actual worksheet/range read.

## Fallbacks

- If `feishu_sheet_info` / `feishu_sheet_read` are unavailable, ask for export only after tool access is confirmed unavailable.
- If workbook parsing fails, report whether the failure is format, corruption, or dependency-related.
- If the task is blocked by missing output requirements, make one conservative assumption and proceed.

## Minimal Execution Pattern

1. Identify whether the source is **Feishu** or **local file**
2. Use the matching read path first
3. Apply the narrowest possible transformation
4. Save only when needed
5. Return the result path or analysis summary
