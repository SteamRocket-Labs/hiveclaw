---
name: PDF Generator
description: "Use when you need to create print-ready PDF reports, fill PDF forms, render source-attributed documents, or produce a PDF artifact from structured markdown and workspace inputs."
license: MIT
tools:
  - read_file
  - read_document
  - write_file
  - execute_code
  - send_channel_file
metadata:
  version: '3.0'
  category: document-generation
---

# PDF Generator

<role>
Use this skill when the user wants a `.pdf` artifact — a fresh report,
resume, or polished handoff document; a filled PDF form; or a reformatted
version of existing content as a print-ready PDF. Handle PDF work directly
— do NOT spawn sub-agents. This is a cloud execution contract: one narrow
`execute_code` render pass per task.
</role>

<when_to_use>
- User asks for a new PDF (report, proposal, resume, invoice, polished handoff)
- User provides a PDF form and wants fields filled in
- User wants Markdown/text/document content reformatted into a styled PDF
- User wants a final PDF artifact delivered back via the channel
</when_to_use>

<do_not_use_when>
- User wants an editable Word document — use `DOCX Generator`
- User wants a slide deck — use `PPTX Generator`
- User wants a spreadsheet — use `XLSX Processor`
- User only wants text extracted from an existing PDF — return the text, don't generate a new one
</do_not_use_when>

## Tool Reference

<tool_reference>

| Task | Tool | Notes |
|------|------|-------|
| Inspect existing PDF structure / form fields | `read_document` | Preferred for structure-aware reading |
| Read raw PDF bytes or confirm file exists | `read_file` | Low-level check |
| Generate/fill PDF with a Python script | `execute_code` | Use `reportlab`, `weasyprint`, `pypdf`, or `pdfplumber` depending on task |
| Save the output file | `write_file` | Usually handled inside `execute_code` |
| Deliver to the current channel | `send_channel_file` | Workspace-relative path |

</tool_reference>

## Routing

<workflows>

### 1. Fill an existing PDF form
1. `read_document(path="...")` to list form fields.
2. `execute_code` with `pypdf` to fill only the requested fields, preserving layout.
3. Save under `workspace/`, confirm with `read_file`, deliver with `send_channel_file`.

### 2. Create a new PDF from scratch
1. `execute_code` with the smallest script that can produce the layout (reportlab for simple, weasyprint for styled).
2. Save under `workspace/`, confirm, deliver.

### 3. Reformat existing content into PDF
1. Read the source content first (`read_file` or `read_document`).
2. Convert into a minimal structured payload (dict / list of sections).
3. Render the PDF once — no speculative multi-pass iterations.

</workflows>

## Examples

<examples>

### Example A — Create a simple one-page brief

Input: `把刚才的调研总结导出成 PDF`

Correct flow (inside `execute_code`):
```python
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

styles = getSampleStyleSheet()
doc = SimpleDocTemplate("workspace/research-brief-2026-04-16.pdf", pagesize=A4)
story = [
    Paragraph("AI Infra 融资周报", styles["Title"]),
    Spacer(1, 12),
    Paragraph("Top 5 rounds 2026-04-13 ~ 04-15 ...", styles["BodyText"]),
]
doc.build(story)
```
Then `send_channel_file(file_path="workspace/research-brief-2026-04-16.pdf")`.
Output: `已生成 workspace/research-brief-2026-04-16.pdf 并发送到当前对话。`

### Example B — Fill a PDF form

Input: `把 workspace/application-form.pdf 里的 name 字段填成"张三"，date 填成"2026-04-16"`

Correct flow:
```
read_document(path="workspace/application-form.pdf")
# → returns field names: name, date, signature

# execute_code:
from pypdf import PdfReader, PdfWriter
reader = PdfReader("workspace/application-form.pdf")
writer = PdfWriter(clone_from=reader)
writer.update_page_form_field_values(writer.pages[0],
    {"name": "张三", "date": "2026-04-16"})
with open("workspace/application-form-filled.pdf", "wb") as f:
    writer.write(f)
```
Output: `已填好字段（name, date），保存在 workspace/application-form-filled.pdf。signature 字段留空，需要你手写签名后再签。`

</examples>

## Required Inputs

- Source file path if reformatting or filling an existing PDF
- Exact output path if the user needs a saved artifact
- Field names/values for forms
- Style expectations only when they materially affect delivery

If some details are missing, make one conservative assumption and proceed.

## Execution Rules

- Prefer one deterministic render/fill pass.
- Read before mutating an existing PDF.
- Do not claim the PDF exists unless the file is actually written.
- If visual polish matters, implement it in code/scripts, not in the skill text.
- Return the exact output path for generated files.

## Success Criteria

<success_criteria>
- The requested PDF exists at the reported path (verified by `read_file` or save confirmation).
- The PDF contains the expected content or field values.
- Existing layout is preserved for fill operations unless the user asked for redesign.
- The response references a real file or a real extracted result — never a fabricated path.
</success_criteria>

## Fallbacks

- If the PDF is encrypted, corrupted, or unsupported, report that explicitly.
- If form fields cannot be resolved, say whether the issue is "no form fields", "wrong field names", or "write failure".
- If the user only needs extracted text, do not force a regeneration path.

## Anti-patterns

<anti_patterns>

- ❌ **Run multiple speculative render passes** trying different layouts → token-expensive and often produces conflicting outputs. Plan the layout first, render once.
- ❌ **Claim the PDF was generated without verifying the file exists** → `execute_code` may have silently raised and returned a partial output. Check with `read_file` after save.
- ❌ **Put visual design rules in this skill text** → design belongs in the rendering script. Keep the skill short and route-focused.
- ❌ **Force a regeneration when the user only asked for text extraction** → wastes a round. If the user says "read this PDF", return text, not a new file.
- ❌ **Write to an absolute path outside workspace** → the file won't be reachable by `send_channel_file`. Always save under `workspace/`.
- ❌ **Skip `read_document` on fill tasks** → you may target the wrong field names, producing an empty or corrupted form. Inspect first.

</anti_patterns>

## Minimal Execution Pattern

1. Determine whether this is **fill**, **create**, or **reformat**
2. Read existing structure when a source file exists
3. Run one narrow PDF script
4. Save the output
5. Return the file path or extracted result

## Bundled Resources

Load resources by need, not by default:

- `references/pdf-cookbook.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/report.md`: use as the output scaffold when creating this artifact type.
- `scripts/fill_inspect.py`: run for deterministic generation or inspection, then validate its output on representative input.
- `scripts/fill_write.py`: run for deterministic generation or inspection, then validate its output on representative input.
- `scripts/make.sh`: run for deterministic generation or inspection, then validate its output on representative input.
- `scripts/merge.py`: run for deterministic generation or inspection, then validate its output on representative input.
- `scripts/palette.py`: run for deterministic generation or inspection, then validate its output on representative input.
- `scripts/render_body.py`: run for deterministic generation or inspection, then validate its output on representative input.
- `scripts/render_cover.py`: run for deterministic generation or inspection, then validate its output on representative input.

When a script is used, run or validate it on representative input before final delivery and report any rendering or parsing failure.
