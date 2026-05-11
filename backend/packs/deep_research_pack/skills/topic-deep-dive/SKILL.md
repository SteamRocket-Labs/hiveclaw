---
name: Topic Deep Dive
description: "Use when Codex needs to research a bounded topic, product, policy, company, or technical question with primary sources, contradiction checks, and a claim-level evidence ledger."
tools:
  - web_search
  - web_fetch
  - firecrawl_fetch
  - xcrawl_scrape
  - read_file
  - write_file
  - delegate_to_agent
  - send_channel_file
metadata:
  version: '0.1'
  category: research
---

# Topic Deep Dive

<role>
Use when Codex needs to research a bounded topic, product, policy, company, or technical question with primary sources, contradiction checks, and a claim-level evidence ledger.
</role>

<when_to_use>
- The user asks for Topic Deep Dive output or a closely related workflow.
- The task requires the declared tools, bundled references, or templates in this skill.
- The result must be reusable, source-aware, or artifact-shaped rather than a short ad hoc answer.
</when_to_use>

## Operating Procedure

1. Restate the bounded question, scope, time window, and evidence standard.
2. Search primary sources first, then fetch source pages and record dates.
3. Build a claim ledger as research proceeds; do not rely on search snippets.
4. Separate confirmed facts, inference, contradictions, stale data, and unresolved gaps.
5. Write a concise source-attributed packet with the supplied template.

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
