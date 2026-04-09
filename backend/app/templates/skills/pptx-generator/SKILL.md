---
name: PPTX Generator
description: >
  Cloud-first presentation routing skill. Use for creating, reading, or editing PowerPoint
  decks with deterministic scripts. Prefer small execution loops and explicit output
  contracts over embedded design tutorials.
license: MIT
tools:
  - read_file
  - read_document
  - write_file
  - execute_code
  - send_channel_file
metadata:
  version: "3.0"
  category: productivity
---

# PPTX Generator

Handle presentation work directly. Do **not** spawn sub-agents.

This skill is a **cloud execution contract**. Use the smallest script that can inspect, create, or update the requested deck.

## Use This Skill For

- Creating a new `.pptx` presentation
- Editing text, tables, or images in an existing deck
- Extracting slide text or simple structure from an existing presentation
- Delivering a finished deck artifact back to the user

## Do Not Use This Skill For

- Printable documents better delivered as PDF/DOCX
- Spreadsheet-heavy outputs better delivered as XLSX
- Pure discussion responses that do not need a deck file

## Routing

### 1. Read or inspect an existing deck

Use `read_document` or `execute_code` to extract slide text and basic structure.

### 2. Create a new deck

Use `execute_code` to generate the deck deterministically and save it to the workspace.

### 3. Edit an existing deck

Read first, then apply the narrowest possible slide edit with `execute_code`.

## Required Inputs

- Source deck path if editing or extracting
- Target output path if a file artifact is expected
- Slide-level requirements if the user only wants part of the deck changed
- Design expectations only when they materially affect delivery

If some details are missing, make one conservative assumption and proceed.

## Execution Rules

- Prefer a small, focused script over a full slide-system rewrite.
- Read before editing.
- Preserve slide order and existing assets unless the user requested restructuring.
- If the user only wants extracted content, return that directly instead of always producing a new deck.
- If you create or edit a file, report the exact path.

## Success Criteria

- The requested `.pptx` exists at the reported path.
- The requested slide content or edits are present.
- Existing deck structure is preserved unless explicitly changed.
- The response references a real output file or real extracted content.

## Fallbacks

- If the deck is corrupted or unsupported, say so directly.
- If the request is underspecified, build the minimum viable deck structure and note the assumption.
- If slide rendering assets are missing, report which asset path is missing.

## Minimal Execution Pattern

1. Determine whether this is **read**, **create**, or **edit**
2. Read existing structure when a source file exists
3. Run one narrow deck script
4. Save the output
5. Return the file path or extracted result
