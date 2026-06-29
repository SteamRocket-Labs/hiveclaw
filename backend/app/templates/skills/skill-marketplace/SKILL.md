---
name: Skill Marketplace Vetting
description: "Use when a user explicitly asks to discover, compare, vet, or install a third-party skill from a public catalog or repository."
tools:
  - web_search
  - web_fetch
  - firecrawl_fetch
  - execute_code
is_system: false
is_default: true
---

# Skill Marketplace Vetting

<role>
Use this skill when the user explicitly asks to find, review, or install a
third-party skill. A skill is a progressive-disclosure capability capsule:
instructions, references, templates, scripts, evals, workflow definitions, and
subagent definitions may be packaged together, but executable components still
run through their governed runtimes.
</role>

<when_to_use>
- The user asks to find or compare installable skills for a capability.
- The user provides a public skill URL or repository and wants it reviewed.
- The user asks to install a skill after seeing candidates.
- A candidate skill must be vetted before it can be recommended or installed.
</when_to_use>

<do_not_use_when>
- The current Core tools or installed skills already handle the task.
- The request is a one-off task; do the task directly instead of installing.
- The user did not ask to install or evaluate external skill code.
- The candidate requires secrets, local binaries, private network access, or
  credential passthrough that the platform has not configured.
</do_not_use_when>

## Tool Reference

<tool_reference>

| Step | Tool | Purpose |
|------|------|---------|
| Discover public candidates | `web_search` | Find catalog or repository entries |
| Read source metadata or SKILL.md | `web_fetch` | Primary source review |
| Escalate incomplete web reads | `firecrawl_fetch` | JS-heavy or incomplete public pages |
| Run approved installer after confirmation | `execute_code` | Platform-approved install command only |

</tool_reference>

## Workflow

<workflows>

### 1. Discover candidates

Use `web_search` for public catalogs or repositories. Prefer candidates with:

- Clear source repository or catalog page
- Recent maintenance signal
- Explicit `SKILL.md`
- Minimal permissions and no credential harvesting
- Existing user or install evidence when available

### 2. Vet every candidate before recommending

Read the source with `web_fetch`; use `firecrawl_fetch` only when the public page
is incomplete. Check:

- What tools, scripts, commands, or external services it asks for
- Whether it requests secrets, API keys, tokens, browser cookies, or local files
- Whether it shells out, downloads code, phones home, or exfiltrates data
- Whether the behavior duplicates installed capabilities
- Whether the instructions are stable enough to reuse

Classify risk as `low`, `medium`, or `high`. Reject high-risk skills unless the
user explicitly accepts the risk and the platform policy still allows it.

### 3. Ask before installation

Never install from search results alone. Present:

- Candidate name and source URL
- Why it fits the request
- Risk rating and blocking concerns
- Required tools or permissions
- Exact install action you plan to run

Install only after explicit user confirmation. Use `execute_code` only for the
approved installer command; do not use shell commands for source review.

</workflows>

<anti_patterns>

- Do not install a skill just because a task is unfamiliar.
- Do not install another marketplace/vetting skill to do this job.
- Do not use shell networking, `curl`, package managers, or arbitrary scripts
  to inspect untrusted skill source.
- Do not treat stars, install counts, or catalog ranking as a security review.
- Do not bypass platform policy when a skill needs unavailable credentials or
  private resources.

</anti_patterns>

<examples>

Input: "Find a skill for parsing invoices."
Output: search public candidates, fetch the best candidate source, produce a
risk-rated recommendation, and ask before installation.

Input: "Install this skill from GitHub: <url>"
Output: fetch and vet `SKILL.md` first, summarize risks and required tools, then
ask for confirmation before running the approved install command.

</examples>

## Bundled Resources

Load resources by need, not by default:

- `references/vetting-checklist.md`: use when a candidate has non-trivial
  install, permission, source-provenance, or supply-chain risk.
- `templates/recommendation.md`: use when presenting a structured candidate
  recommendation before asking for installation confirmation.
