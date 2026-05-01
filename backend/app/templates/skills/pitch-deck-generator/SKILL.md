---
name: Pitch Deck Generator
description: Generate investor pitch deck outlines and PPTX artifacts from a company brief and explicit assumptions.
license: Proprietary
tools:
  - read_file
  - write_file
  - execute_code
  - send_channel_file
metadata:
  hive.version: "0.1.0"
  hive.pack: office_pack
  hive.locale: cloud
  hive.invocation: both
---

# Pitch Deck Generator

<role>
Use this skill when the user asks for an investor deck, fundraising deck,
strategy deck, or board-style presentation. Use `read_file` for supplied
briefs, `execute_code` for deterministic PPTX generation, `write_file` for
outline or intermediate JSON, and `send_channel_file` when delivery is needed.
</role>

<when_to_use>
- User asks for a pitch deck or fundraising presentation
- User provides company, market, traction, product, financial, or team notes
- User wants a `.pptx` artifact or a slide-by-slide outline
</when_to_use>

<anti_patterns>
- Do not invent traction, customers, revenue, or funding details.
- Do not create text-heavy slides; split dense material across slides.
- Do not skip an outline step when the brief is incomplete or ambiguous.
</anti_patterns>

<examples>
Input: "根据这个 brief 做 10 页 Seed 轮 pitch deck"

Output: `execute_code` creates `pitch-deck.pptx`; `write_file` saves the slide
outline JSON and notes.
</examples>

## Workflow

1. Read the brief with `read_file` when provided.
2. Build an outline: title, problem, market, product, traction, model,
   competition, go-to-market, financials, team, ask.
3. Use `execute_code` with `python-pptx` to render a clean deck.
4. Save outline and deck with `write_file`; deliver with `send_channel_file`
   only if requested.
