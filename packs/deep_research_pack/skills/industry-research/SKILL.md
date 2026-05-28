---
name: Industry Research
description: "Use when Codex needs to build an industry landscape, market map, competitor structure, adoption signal review, regulatory scan, and source-attributed research report."
tools:
  - deep_research_run
  - deep_research_start
  - deep_research_check
  - deep_research_export
metadata:
  version: '0.1'
  category: research
---

# Industry Research

Use this skill to route industry maps, market landscape research, sector briefings,
competitor scans, and customer-facing research packets to Deep Research v2 with
`mode=industry_research`.

## Workflow

1. Frame the industry boundary and segment taxonomy.
2. Call `deep_research_start` for full or flagship landscape work; use
   `deep_research_run` only for quick/standard scoped briefs.
3. Pass `mode=industry_research` so the runtime uses orchestrator-worker fan-out,
   worker digests, `source_notes.jsonl`, `lane_summaries.jsonl`, and
   `worker_reports.jsonl`.
4. Use `deep_research_check` for progress and `deep_research_export` for the
   user-visible markdown/JSON/HTML artifact.
5. Evaluate the market map, key players table, demand drivers, constraints,
   regulation, and source ledger from the exported Deep Research artifact.

## Required Sections

- Market definition and segmentation.
- Value chain and buyer groups.
- Competitor or provider landscape.
- Demand drivers and adoption blockers.
- Regulation and risk.
- Evidence ledger with source dates.

## Quality Bar

- Prefer data with dates, methodology, and publisher identity.
- Label estimates and ranges clearly.
- Separate market facts from strategic interpretation.
- Do not fabricate market size if the source set is weak.
- Do not use raw web tools, `delegate_to_agent`, or manual `write_file` reports from this subskill.
- Unknown `src_*` citations are blockers; every citation must resolve to `sources.jsonl`.

## Bundled Resources

Load resources by need, not by default:

- `references/playbook.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/report.md`: use as the output scaffold when creating this artifact type.

## Anti-patterns

- Do not treat a search result, filename, prior memory, or worker digest as final proof unless it resolves through the Deep Research ledger.
- Do not load every reference file by default; use progressive disclosure and read only the relevant resource.
- Do not call destructive or externally visible tools unless the user asked for that action and required confirmation is satisfied.

## Examples

- Input: "Map this market." Output: call `deep_research_start` with `mode=industry_research`, then report task id and artifact paths from `deep_research_check`.
- Input: "Quick market brief." Output: call `deep_research_run` with `mode=industry_research`, then summarize gates, gaps, source count, and export path.
