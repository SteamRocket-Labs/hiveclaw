---
name: DOCX Generator
description: >
  Cloud-first DOCX routing skill. Use for creating, filling, or editing Word documents
  with deterministic scripts and narrow output contracts. Prefer short execution loops
  over long formatting tutorials.
license: MIT
tools:
  - read_file
  - read_document
  - write_file
  - execute_code
  - send_channel_file
metadata:
  version: "3.0"
  category: document-processing
---

# DOCX Generator

<role>
Use this skill when the user wants to create, fill, or edit a `.docx` file.
Handle DOCX work directly — do NOT spawn sub-agents. This is a cloud
execution contract: use the smallest `execute_code` script that can do the
job deterministically, save to workspace, and deliver via the channel.
</role>

<when_to_use>
- User asks for a `.docx` report, memo, proposal, contract, or form draft
- User provides a Word template and wants fields filled in
- User wants to edit content in an existing `.docx`
- User wants a finished Word artifact delivered back (workspace file + channel)
</when_to_use>

<do_not_use_when>
- Output would be better as PDF (print-ready) or PPTX (deck) — use the right generator
- Output is spreadsheet-heavy — use `XLSX Processor`
- User only wants plain-text/Markdown response — no need to produce a file
</do_not_use_when>

## Use This Skill For

- Creating a new `.docx` report, memo, proposal, contract, or form draft
- Filling an existing Word template with new values
- Editing content in an existing `.docx`
- Producing a final Word artifact for download or channel delivery

## Do Not Use This Skill For

- Presentation decks
- Spreadsheet-heavy outputs better represented as `.xlsx`
- Pure Markdown/plain-text responses that do not need a Word file

## Tool Reference

<tool_reference>

| Task | Tool | Notes |
|------|------|-------|
| Inspect existing `.docx` structure | `read_document` | Higher-level reader with structure preservation |
| Read raw `.docx` bytes or confirm file exists | `read_file` | Use when you only need text or metadata |
| Run a Python script to create/edit `.docx` | `execute_code` | Use `python-docx` library |
| Write the finished file | `write_file` | Usually handled inside `execute_code`; used when saving text snapshots |
| Deliver the file to the current channel | `send_channel_file` | Workspace-relative path |

</tool_reference>

## Routing

<workflows>

### 1. New document from scratch

Use `execute_code` to generate a new `.docx` file deterministically.

### 2. Existing `.docx` needs content changes

Use:

1. `read_document` or `read_file` first to confirm the structure
2. `execute_code` to apply the narrowest possible edit
3. save to the requested output path

### 3. Template fill / form fill

Use `execute_code` to open the template, fill only the requested fields/sections, and preserve layout unless the user asked to restyle it.

</workflows>

## Examples

<examples>

### Example A — Create a simple memo

Input: `帮我起草一份周会纪要 docx，包含议题、决定、下一步`

Correct flow (inside `execute_code`):
```python
from docx import Document

doc = Document()
doc.add_heading("Weekly Sync — 2026-04-16", level=1)

doc.add_heading("议题", level=2)
doc.add_paragraph("• 下季度 OKR 草案讨论\n• 飞书集成上线进度")

doc.add_heading("决定", level=2)
doc.add_paragraph("• OKR 草案本周五前定稿\n• 飞书集成 4-20 上线")

doc.add_heading("下一步", level=2)
doc.add_paragraph("• Alice: 本周四前完成 OKR 草案\n• Bob: 4-19 前跑完飞书集成冒烟测试")

doc.save("workspace/weekly-sync-2026-04-16.docx")
```
Then: `send_channel_file(file_path="workspace/weekly-sync-2026-04-16.docx")`
Output: `已生成 workspace/weekly-sync-2026-04-16.docx 并发送到当前对话。`

### Example B — Fill a contract template

Input: `把 workspace/contract-template.docx 里的公司名改成"XXX Inc"，合同金额改成 120,000`

Correct flow:
```
read_document(path="workspace/contract-template.docx")  # inspect fields

# execute_code (python-docx):
from docx import Document
doc = Document("workspace/contract-template.docx")
for para in doc.paragraphs:
    if "{{COMPANY}}" in para.text:
        para.text = para.text.replace("{{COMPANY}}", "XXX Inc")
    if "{{AMOUNT}}" in para.text:
        para.text = para.text.replace("{{AMOUNT}}", "120,000")
doc.save("workspace/contract-xxx-inc-v1.docx")
```
Output: `已生成 workspace/contract-xxx-inc-v1.docx，公司名和金额已替换。模板里其他格式保持不变。`

</examples>

## Required Inputs

- Source document path if editing an existing file
- Target output path if the user wants a generated artifact
- Clear section/field names when only part of the document should change
- Style constraints only when they materially affect delivery

If one of these is missing, make the safest reasonable assumption and proceed.

## Execution Rules

- Read before editing.
- Prefer the **smallest working script**.
- Preserve headings, tables, page breaks, images, and numbering unless the user asked to change them.
- Do not silently rewrite the entire document for a small edit.
- If the user only wants extracted content or analysis, return that directly instead of always generating a new `.docx`.
- If you create or modify a file, return the exact output path.

## Success Criteria

- The requested `.docx` exists at the reported path.
- The requested content changes are present.
- Existing structure is preserved unless explicitly changed.
- The response references real files, not imagined output names.

## Fallbacks

- If parsing fails, report whether the problem is corruption, unsupported structure, or missing dependency.
- If the source is not actually a `.docx`, redirect to the correct tool chain instead of forcing DOCX generation.
- If the user’s output requirements are underspecified, pick one conservative default and continue.

## Minimal Execution Pattern

1. Determine whether this is **create** or **edit/fill**
2. Read existing structure when a source file exists
3. Run one narrow document script
4. Save the output
5. Return the file path or extracted result

## Anti-patterns

<anti_patterns>

- ❌ **Rewrite the entire document for a small edit** → loses formatting, images, numbering, and comments. Use targeted paragraph/run replacement with `python-docx`.
- ❌ **Skip `read_document` before editing** → you don't know the existing structure and may destroy it. Always read first.
- ❌ **Claim the file exists without verifying** → `execute_code` may have silently errored. Check with `read_file` or `list_files` that the target path exists after saving.
- ❌ **Write to an absolute path outside workspace** → `send_channel_file` needs workspace-relative paths and the file won't be reachable. Always save under `workspace/`.
- ❌ **Hard-code strings that should be parameters** → the same template fill often needs to run multiple times. Accept the variable values from the user and plug them in.
- ❌ **Produce a `.docx` when the user only wanted analysis** → wastes a round and creates a useless artifact. If the ask is "summarize this docx", return text, not a new file.

</anti_patterns>
