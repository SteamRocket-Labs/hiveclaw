---
name: Source Ledger Audit
description: "Use when Codex needs to audit a draft, report, memo, or deck against cited sources, classify unsupported claims, stale evidence, contradictions, and replacement citation needs."
tools:
  - deep_research_run
  - deep_research_start
  - deep_research_check
  - deep_research_export
metadata:
  version: '0.1'
  category: research
---

# Source Ledger Audit

<role>
Route source-ledger audits to Deep Research v2 with `mode=source_ledger_audit`; use the runtime ledger and citation gate as the authority.
</role>

<when_to_use>
- The user asks for Source Ledger Audit output or a closely related workflow.
- The task requires the declared tools, bundled references, or templates in this skill.
- The result must be reusable, source-aware, or artifact-shaped rather than a short ad hoc answer.
</when_to_use>

## Operating Procedure

1. Restate the draft, source ledger, and audit scope in the Deep Research question.
2. Call `deep_research_run` for quick/standard audits or `deep_research_start` for broad/full audits, always with `mode=source_ledger_audit`.
3. Let the orchestrator-worker runtime fetch evidence and persist `worker_reports.jsonl`, `sources.jsonl`, `claims.jsonl`, `source_notes.jsonl`, and `evaluation.jsonl`.
4. Lead with failed quality gates, unsupported claims, stale evidence, contradictions, and unknown source ids.
5. Use `deep_research_export` for patch-ready wording and replacement citation targets when available.

## Quality Bar

- Do not invent facts, owners, dates, recipients, source evidence, or external system state.
- Do not manually fetch/write the audit from this subskill; the dedicated runtime owns source fetching, claims, and exports.
- Unknown `src_*` citations are blockers and must not be presented as completed audit evidence.
- Prefer deterministic scripts or templates when the skill bundles them for this workflow.
- Keep the final output focused on the artifact or decision the user requested.
- Surface missing credentials, unavailable tools, stale data, and unsupported claims as blockers instead of silently working around them.

<anti_patterns>
- Do not treat a search result, filename, prior memory, or worker digest as final proof unless it resolves through the Deep Research ledger.
- Do not load every reference file by default; use progressive disclosure and read only the relevant resource.
- Do not call destructive or externally visible tools unless the user asked for that action and required confirmation is satisfied.
</anti_patterns>

<examples>
- Input: "Audit this memo's citations." Output: call `deep_research_run` with `mode=source_ledger_audit`, then report unsupported claims, gates, gaps, and artifact paths.
- Input: "Full citation audit for this deck." Output: call `deep_research_start` with `mode=source_ledger_audit` and use `deep_research_check` for progress.
</examples>

## Bundled Resources

Load resources by need, not by default:

- `references/playbook.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/report.md`: use as the output scaffold when creating this artifact type.
