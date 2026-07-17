# Session disclosure design QA

Date: 2026-07-17

## Scope

- One turn-level Session disclosure contains commentary, tool activity, compaction boundaries, and the final-answer settlement state.
- A running tool phase occupies one collapsed line and names the current tool.
- The running line stays interactive; expanding it reveals the complete tool-call history accumulated in that phase.
- A new running tool updates the one-line label without closing an already-open history disclosure.
- The final typed answer settles and collapses the whole turn; reopening restores the chronological process view without `Writing response` or `Reports` wrappers.

## Reference and implementation comparison

- Reference: `codex-clipboard-146d3a76-4327-45f6-973f-67a372e67619.png` and `codex-clipboard-15dc9897-28b6-47e7-a0dd-5db0d6f65d68.png` supplied by the owner.
- Implementation: local Vite harness rendered the production `RunDisclosureBlock` and production CSS at desktop and 390 px content widths.
- Layout, density, typography, icon family, disclosure hierarchy, one-line truncation, chronological ordering, and light-mode surface treatment match the reference intent.
- No overlap, clipping, awkward wrapping, extra card surface, or raw ordinary-tool payload appeared in the tested states.

## Interaction and accessibility checks

- Native `button`, `details`, and `summary` controls expose keyboard-reachable disclosure semantics.
- Running turn starts expanded; completed turn starts collapsed.
- Running tool history opened successfully and stayed open after the current tool changed from `Read file` to `Run command`.
- Expanded history retained completed calls and showed the new running call in order.
- Command evidence remains recoverable through a nested disclosure; ordinary tool payloads remain progressively disclosed.
- Desktop and narrow-width renders preserved readable line lengths, ellipsis behavior, and usable controls.

## Automated evidence

- `npm test`: 120 files, 700 tests passed.
- `npm run build`: TypeScript, Vite production build, and bundle budgets passed.
- Targeted disclosure/reducer/timeline suite: 3 files, 56 tests passed.

final result: passed
