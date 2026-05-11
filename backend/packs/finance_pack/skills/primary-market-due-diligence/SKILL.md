---
name: Primary Market Due Diligence
description: "Use when Codex needs to run primary-market diligence on a private company, including provider readiness, registry data, funding history, risk checks, and source constraints."
tools:
  - finance_run_workflow
  - finance_get_company_registry
  - finance_get_funding_rounds
  - finance_get_provider_status
---

# Primary Market Due Diligence

<role>
Use when Codex needs to run primary-market diligence on a private company, including provider readiness, registry data, funding history, risk checks, and source constraints.
</role>

<when_to_use>
- The user asks for Primary Market Due Diligence output or a closely related workflow.
- The task requires the declared tools, bundled references, or templates in this skill.
- The result must be reusable, source-aware, or artifact-shaped rather than a short ad hoc answer.
</when_to_use>

## Operating Procedure

1. Confirm company identity, jurisdiction, round/stage, diligence scope, and decision context.
2. Check provider readiness and collect registry, funding, and available public-source data.
3. Separate verified company facts from paid-provider gaps and unverified claims.
4. Run the finance workflow only when enough sources are available to support diligence conclusions.
5. Write a diligence packet with source limits, risks, missing documents, and next diligence asks.

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
