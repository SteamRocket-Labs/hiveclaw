# Skill Marketplace Vetting Checklist

Use this checklist only after the user explicitly asks to discover, compare,
vet, or install a third-party skill.

## Source Review

- Prefer primary source pages such as the catalog entry, repository, or
  canonical `SKILL.md`.
- Use `web_search` for discovery, `web_fetch` for primary source review, and
  `firecrawl_fetch` only when a public page is incomplete.
- Do not use shell networking, package managers, or arbitrary scripts to inspect
  untrusted source.

## Risk Signals

- Requests secrets, tokens, browser cookies, local files, or credential
  passthrough.
- Downloads or executes code during review.
- Expands permissions beyond the user's task.
- Duplicates installed capabilities without a clear quality gain.
- Lacks source provenance, license information, or maintenance evidence.

## Recommendation Shape

Classify every candidate as `low`, `medium`, or `high` risk. Installation must
wait for explicit user confirmation and the platform's normal policy checks.
