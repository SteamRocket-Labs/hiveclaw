---
name: Secondary Equity Deep Dive
description: "Use when Codex needs to analyze a listed company with filings, price history, valuation, comps, source ledger, and an investment-style research packet."
tools:
  - finance_run_workflow
  - finance_compile_research_packet
  - finance_compute_dcf
  - finance_build_comps
---

# Secondary Equity Deep Dive

<role>
Use when Codex needs to analyze a listed company with filings, price history, valuation, comps, source ledger, and an investment-style research packet.
</role>

<when_to_use>
- The user asks for Secondary Equity Deep Dive output or a closely related workflow.
- The task requires the declared tools, bundled references, or templates in this skill.
- The result must be reusable, source-aware, or artifact-shaped rather than a short ad hoc answer.
</when_to_use>

## Operating Procedure

1. Confirm ticker, exchange, date range, investor audience, and required output depth.
2. Run filings, price history, financial statements, DCF, comps, and source-ledger checks as needed.
3. Separate valuation mechanics from investment conclusion and label stale or missing data.
4. Use `finance_compile_research_packet` to assemble the final evidence-backed research output.
5. Write the memo with thesis, valuation, risks, catalysts, source ledger, and next checks.

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
