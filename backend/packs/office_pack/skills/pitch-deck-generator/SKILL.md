---
name: Pitch Deck Generator
description: "Use when Codex needs to create investor, board, or sales pitch deck outlines and slide artifacts from a company brief, product narrative, fundraising context, or strategic memo."
license: Proprietary
tools:
  - read_file
  - write_file
  - execute_code
  - send_channel_file
metadata:
  hive.version: 0.1.0
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

## Bundled Resources

Load resources by need, not by default:

- `references/deck-structures.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/outline.md`: use as the output scaffold when creating this artifact type.

## Quality Bar

- Do not invent facts, owners, dates, recipients, source evidence, or external system state.
- Prefer deterministic scripts or templates when the skill bundles them for this workflow.
- Keep the final output focused on the artifact or decision the user requested.
- Surface missing credentials, unavailable tools, stale data, and unsupported claims as blockers instead of silently working around them.
