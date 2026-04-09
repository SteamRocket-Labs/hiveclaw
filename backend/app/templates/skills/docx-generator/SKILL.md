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

Handle DOCX work directly. Do **not** spawn sub-agents.

This skill is a **cloud execution contract**. Use the smallest script that can create or edit the requested document.

## Use This Skill For

- Creating a new `.docx` report, memo, proposal, contract, or form draft
- Filling an existing Word template with new values
- Editing content in an existing `.docx`
- Producing a final Word artifact for download or channel delivery

## Do Not Use This Skill For

- Presentation decks
- Spreadsheet-heavy outputs better represented as `.xlsx`
- Pure Markdown/plain-text responses that do not need a Word file

## Routing

### 1. New document from scratch

Use `execute_code` to generate a new `.docx` file deterministically.

### 2. Existing `.docx` needs content changes

Use:

1. `read_document` or `read_file` first to confirm the structure
2. `execute_code` to apply the narrowest possible edit
3. save to the requested output path

### 3. Template fill / form fill

Use `execute_code` to open the template, fill only the requested fields/sections, and preserve layout unless the user asked to restyle it.

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
