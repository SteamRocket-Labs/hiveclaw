---
name: PPTX Generator
description: "Use when you need to create, inspect, or edit PowerPoint presentations, preserve slide structure, generate investor decks, or build a reusable PPTX artifact from source notes."
license: MIT
tools:
  - read_file
  - read_document
  - write_file
  - execute_code
  - send_channel_file
metadata:
  version: '3.0'
  category: productivity
---

# PPTX Generator

<role>
Use this skill when the user wants a `.pptx` artifact — create a new deck,
edit existing slides, extract slide text, or deliver a finished deck back
via the channel. Handle deck work directly, no sub-agents. This is a cloud
execution contract: one narrow `execute_code` render pass per task.
</role>

<when_to_use>
- User asks for a new `.pptx` presentation
- User wants to edit text, tables, or images in an existing deck
- User wants to extract slide text or simple structure from a deck
- User wants a finished deck delivered back via the channel
</when_to_use>

<do_not_use_when>
- User wants a printable document — use `PDF Generator`
- User wants a word document — use `DOCX Generator`
- User wants a spreadsheet — use `XLSX Processor`
- User only wants discussion/analysis of a deck — return text findings, don't generate a new file
</do_not_use_when>

## Tool Reference

<tool_reference>

| Task | Tool | Notes |
|------|------|-------|
| Inspect existing deck structure / slide text | `read_document` | Preferred for structure-aware reading |
| Raw file check | `read_file` | Low-level existence/size check |
| Create/edit deck with a Python script | `execute_code` | Use `python-pptx` |
| Save the output | `write_file` | Usually inside `execute_code` |
| Deliver to the channel | `send_channel_file` | Workspace-relative path |

</tool_reference>

## Routing

<workflows>

### 1. Read / inspect an existing deck
Use `read_document` (preferred) or `execute_code` with `python-pptx` to extract slide text and basic structure.

### 2. Create a new deck
Use `execute_code` with `python-pptx` to generate the deck deterministically. Inside `execute_code`, the current directory is already `workspace/`, so write `deck.pptx` rather than `workspace/deck.pptx`; deliver with `send_channel_file(file_path="workspace/deck.pptx")`.

### 3. Edit an existing deck
Read first, then apply the narrowest possible slide edit with `execute_code`.

</workflows>

## Examples

<examples>

### Example A — Create a 5-slide pitch deck

Input: `帮我做一个 5 页的 AI Infra 投资简报 pptx`

Correct flow (inside `execute_code`):
```python
from pptx import Presentation
from pptx.util import Inches, Pt

prs = Presentation()

# Slide 1: title
slide = prs.slides.add_slide(prs.slide_layouts[0])
slide.shapes.title.text = "AI Infrastructure Investment Brief"
slide.placeholders[1].text = "2026-04-16 | 投研简报"

# Slides 2-5: sections (market / players / moat / recommendation)
for title, body in [
    ("市场规模", "Global AI infra spend $X B in 2025..."),
    ("关键玩家", "NVIDIA / TSMC / Broadcom / ..."),
    ("护城河分析", "Stack-level defensibility..."),
    ("投资建议", "Overweight chip designers, underweight pure-cloud..."),
]:
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = title
    slide.placeholders[1].text = body

prs.save("workspace/ai-infra-brief-2026-04-16.pptx")
```
Then `send_channel_file(file_path="workspace/ai-infra-brief-2026-04-16.pptx")`.

### Example B — Extract deck text only

Input: `workspace/q1-review.pptx 有哪几页说了营收？`

Correct flow:
```
read_document(path="workspace/q1-review.pptx")
# Returns slide-by-slide text content
```
Output: `第 3、4、7 页提到营收。第 3 页是总营收口径，第 4 页是按产品线拆分，第 7 页是 YoY 对比。要不要我把这几页的要点整理一下？`

(Do NOT generate a new pptx in this case.)

</examples>

## Required Inputs

- Source deck path if editing or extracting
- Target output path if a file artifact is expected
- Slide-level requirements if the user only wants part of the deck changed
- Design expectations only when they materially affect delivery

If details are missing, make one conservative assumption and proceed.

## Execution Rules

- Prefer a small, focused script over a full slide-system rewrite.
- Read before editing.
- Preserve slide order and existing assets unless the user requested restructuring.
- If the user only wants extracted content, return that directly instead of always producing a new deck.
- If you create or edit a file, report the exact path.

## Success Criteria

<success_criteria>
- The requested `.pptx` exists at the reported path.
- The requested slide content or edits are present.
- Existing deck structure is preserved unless explicitly changed.
- The response references a real output file or real extracted content.
</success_criteria>

## Fallbacks

- If the deck is corrupted or unsupported, say so directly.
- If the request is underspecified, build the minimum viable deck structure and note the assumption.
- If slide rendering assets (images, logos) are missing, report which asset path is missing.

## Anti-patterns

<anti_patterns>

- ❌ **Generate a new deck when the user asked for analysis** → wastes a round. If the ask is "tell me what's on slide 3", return text, don't produce a new pptx.
- ❌ **Rewrite the entire deck for a small edit** → loses slide layouts, speaker notes, animations. Use targeted slide index + placeholder replacement with `python-pptx`.
- ❌ **Skip `read_document` before editing** → you don't know the existing structure and may target wrong placeholders.
- ❌ **Write to an absolute path outside workspace** → `send_channel_file` won't deliver it. Always save under `workspace/`.
- ❌ **Force-style decks with hard-coded fonts/colors in the skill body** → design decisions belong in the rendering script and should adapt per ask.
- ❌ **Hardcode the sample "Lorem ipsum" into the actual output** → users expect real content, not template filler.

</anti_patterns>

## Minimal Execution Pattern

1. Determine whether this is **read**, **create**, or **edit**
2. Read existing structure when a source file exists
3. Run one narrow deck script
4. Save the output
5. Return the file path or extracted result

## Bundled Resources

Load resources by need, not by default:

- `references/pptx-cookbook.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/pitch-deck-outline.md`: use as the output scaffold when creating this artifact type.
