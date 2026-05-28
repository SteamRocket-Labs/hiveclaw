---
name: Topic Deep Dive
description: "Use when Codex needs to research a bounded topic, product, policy, company, or technical question with primary sources, contradiction checks, and a claim-level evidence ledger."
tools:
  - deep_research_run
  - deep_research_start
  - deep_research_check
  - deep_research_export
metadata:
  version: '0.1'
  category: research
---

# Topic Deep Dive

<role>
Route bounded topic, product, policy, company, or technical research to the dedicated Deep Research v2 engine with `mode=topic_deep_dive`.
</role>

<when_to_use>
- The user asks for Topic Deep Dive output or a closely related workflow.
- The task requires the declared tools, bundled references, or templates in this skill.
- The result must be reusable, source-aware, or artifact-shaped rather than a short ad hoc answer.
</when_to_use>

## Operating Procedure

1. Restate the bounded question, scope, time window, and evidence standard.
2. Call `deep_research_run` for quick/standard bounded requests or `deep_research_start` for full/flagship reusable artifacts, always with `mode=topic_deep_dive`.
3. Let the engine run its orchestrator-worker workflow; workers collect source-grounded digests and persist `worker_reports.jsonl`.
4. Inspect `sources.jsonl`, `claims.jsonl`, `source_notes.jsonl`, `lane_summaries.jsonl`, `worker_reports.jsonl`, `evaluation.jsonl`, and `final.json`.
5. Export and summarize the Deep Research artifact; do not hand-write a separate report.

## Quality Bar

- Do not invent facts, owners, dates, recipients, source evidence, or external system state.
- Do not use raw web tools or manual `write_file` reports from this subskill; source discovery belongs inside Deep Research v2.
- Unknown source ids are blockers: every `src_*` in the report must resolve to `sources.jsonl`.
- Prefer deterministic scripts or templates when the skill bundles them for this workflow.
- Keep the final output focused on the artifact or decision the user requested.
- Surface missing credentials, unavailable tools, stale data, and unsupported claims as blockers instead of silently working around them.

<anti_patterns>
- Do not treat a search result, filename, prior memory, or worker digest as final proof unless it resolves through the Deep Research ledger.
- Do not load every reference file by default; use progressive disclosure and read only the relevant resource.
- Do not call destructive or externally visible tools unless the user asked for that action and required confirmation is satisfied.
</anti_patterns>

<examples>
- Input: "Deep dive this protocol launch." Output: call `deep_research_run` with `mode=topic_deep_dive`, then report source count, synthesis gate, gaps, and artifact paths.
- Input: "Full research this company." Output: call `deep_research_start` with `mode=topic_deep_dive` and use `deep_research_check` for progress.
</examples>

## Bundled Resources

Load resources by need, not by default:

- `references/playbook.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/report.md`: use as the output scaffold when creating this artifact type.
