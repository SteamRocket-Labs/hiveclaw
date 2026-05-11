---
name: Finance Research
description: "Use when Codex needs to run source-ledger-backed finance research, provider readiness checks, filings review, DCF or comps workflows, and explicit market-data boundary reporting."
tools:
  - finance_get_provider_status
metadata:
  version: '0.2'
  category: finance
  hive.pack: finance_pack
---

# Finance Research

<role>
Use when Codex needs to run source-ledger-backed finance research, provider readiness checks, filings review, DCF or comps workflows, and explicit market-data boundary reporting.
</role>

<when_to_use>
- The user asks for Finance Research output or a closely related workflow.
- The task requires the declared tools, bundled references, or templates in this skill.
- The result must be reusable, source-aware, or artifact-shaped rather than a short ad hoc answer.
</when_to_use>

## Operating Procedure

1. Check provider readiness before promising current market data or paid-source coverage.
2. Resolve the entity and define market, security, geography, date range, and output type.
3. Use finance workflow tools for filings, price history, financials, DCF, comps, and source ledger where available.
4. Separate data-provider gaps from financial conclusions; never fill missing market data from memory.
5. Compile a source-ledger-backed packet or stop with a provider/readiness blocker.

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
- `templates/ic-memo.md`: use as the output scaffold when creating this artifact type.
