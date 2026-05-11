---
name: IC Memo Generator
description: "Use when Codex needs to create an investment committee memo from finance research outputs, source ledgers, valuation work, risks, catalysts, and decision recommendations."
tools:
  - finance_run_workflow
  - finance_compile_research_packet
  - finance_get_source_ledger
---

# IC Memo Generator

<role>
Use when Codex needs to create an investment committee memo from finance research outputs, source ledgers, valuation work, risks, catalysts, and decision recommendations.
</role>

<when_to_use>
- The user asks for IC Memo Generator output or a closely related workflow.
- The task requires the declared tools, bundled references, or templates in this skill.
- The result must be reusable, source-aware, or artifact-shaped rather than a short ad hoc answer.
</when_to_use>

## Operating Procedure

1. Confirm the investment decision, committee audience, security/company, time horizon, and required recommendation format.
2. Collect or run the underlying finance workflow before drafting the memo.
3. Compile source-ledger-backed research with `finance_compile_research_packet`.
4. Write thesis, valuation, catalysts, risks, diligence gaps, and decision recommendation without unsupported claims.
5. Use the template and mark any blocker that prevents a committee-ready memo.

## Quality Bar

- Do not invent facts, owners, dates, recipients, source evidence, or external system state.
- Prefer deterministic scripts or templates when the skill bundles them for this workflow.
- Keep the final output focused on the artifact or decision the user requested.
- Surface missing credentials, unavailable tools, stale data, and unsupported claims as blockers instead of silently working around them.

<anti_patterns>
- Do not treat a search result, filename, or prior memory as proof without reading the underlying source or file.
- Do not load every reference file by default; use progressive disclosure and read only the relevant resource.
- Do not call destructive or externally visible tools unless the user asked for that action and required confirmation is satisfied.
</anti_patterns>

<examples>
- Input: "Create the requested artifact from these notes." Output: inspect the inputs, load the relevant template/reference, call the declared tools, save the artifact, and report validation notes.
- Input: "Check whether this is safe / supported / current." Output: gather evidence first, classify unsupported or stale claims, and give a direct recommendation with source or file references.
</examples>

## Bundled Resources

Load resources by need, not by default:

- `references/playbook.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/output.md`: use as the output scaffold when creating this artifact type.
