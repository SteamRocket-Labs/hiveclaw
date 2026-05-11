---
name: Source Ledger Audit
description: "Use when Codex needs to audit a draft, report, memo, or deck against cited sources, classify unsupported claims, stale evidence, contradictions, and replacement citation needs."
tools:
  - read_file
  - web_fetch
  - firecrawl_fetch
  - xcrawl_scrape
  - write_file
  - edit_file
  - send_channel_file
metadata:
  version: '0.1'
  category: research
---

# Source Ledger Audit

<role>
Use when Codex needs to audit a draft, report, memo, or deck against cited sources, classify unsupported claims, stale evidence, contradictions, and replacement citation needs.
</role>

<when_to_use>
- The user asks for Source Ledger Audit output or a closely related workflow.
- The task requires the declared tools, bundled references, or templates in this skill.
- The result must be reusable, source-aware, or artifact-shaped rather than a short ad hoc answer.
</when_to_use>

## Operating Procedure

1. Read the draft and existing source ledger before judging any claim.
2. Fetch every cited source with `web_fetch`; do not trust citation labels without source text.
3. Classify each material claim as verified, inferred, contradicted, stale, or unsupported.
4. Lead the output with blocking unsupported claims before prose suggestions.
5. Write patch-ready replacement wording and replacement citation targets when available.

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
- `templates/report.md`: use as the output scaffold when creating this artifact type.
