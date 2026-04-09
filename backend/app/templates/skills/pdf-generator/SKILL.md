---
name: PDF Generator
description: >
  Cloud-first PDF routing skill. Use for generating a new PDF, filling an existing PDF form,
  or reformatting content into a print-ready PDF with deterministic scripts. Prefer narrow
  execution paths and real output artifacts over long design tutorials.
license: MIT
tools:
  - read_file
  - read_document
  - write_file
  - execute_code
  - send_channel_file
metadata:
  version: "3.0"
  category: document-generation
---

# PDF Generator

Handle PDF work directly. Do **not** spawn sub-agents.

This skill is a **routing contract** for cloud execution. Keep the pipeline short and deterministic.

## Use This Skill For

- Creating a new PDF report, proposal, resume, or polished handoff document
- Filling fields in an existing PDF form
- Reformatting Markdown/text/document content into a styled PDF
- Delivering a final PDF artifact back to the user

## Do Not Use This Skill For

- Editable Word documents better delivered as `.docx`
- Slide decks better delivered as `.pptx`
- Spreadsheet-native outputs better delivered as `.xlsx`

## Routing

### 1. Fill an existing PDF form

Use `execute_code` to inspect the form fields first, then fill only the required fields.

### 2. Create a new PDF from scratch

Use `execute_code` with the smallest script or existing rendering pipeline that can produce the requested PDF reliably.

### 3. Reformat existing content into PDF

Read the source content first, convert it into a minimal structured payload, then render the PDF once.

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

- The requested PDF exists at the reported path.
- The PDF contains the expected content or field values.
- Existing layout is preserved for fill operations unless the user asked for redesign.
- The response references a real file or a real extracted result.

## Fallbacks

- If the PDF is encrypted, corrupted, or unsupported, report that explicitly.
- If form fields cannot be resolved, say whether the issue is “no form fields”, “wrong field names”, or “write failure”.
- If the user only needs extracted text, do not force a regeneration path.

## Minimal Execution Pattern

1. Determine whether this is **fill**, **create**, or **reformat**
2. Read existing structure when a source file exists
3. Run one narrow PDF script
4. Save the output
5. Return the file path or extracted result
