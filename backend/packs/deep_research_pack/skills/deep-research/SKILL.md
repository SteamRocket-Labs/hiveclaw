---
name: Deep Research
description: "Use when Codex needs to run first-class source-attributed deep research with dedicated planning, source ledger, claim ledger, evaluation gates, progress artifacts, and report output."
tools:
  - deep_research_run
  - deep_research_start
  - deep_research_check
  - deep_research_cancel
  - deep_research_export
metadata:
  version: '1.0'
  category: research
  hive.pack: deep_research_pack
---

# Deep Research

<role>
Use this skill as the router for the dedicated Deep Research tools. The
research capability lives in `deep_research_run` and `deep_research_start`;
this skill decides which tool to call and how to interpret the artifacts. Do
not manually reproduce deep research with a few `web_search` calls unless the
dedicated tools are unavailable.
</role>

<when_to_use>
- The user asks for "deep research", "source-backed", "market map", "industry research", "competitor research", "due diligence", or "audit this draft".
- The answer needs current facts, source quality review, contradiction checks, regulatory facts, market sizing, or a reusable research artifact.
- The user expects evidence that survives review, not a quick search summary.
</when_to_use>

<do_not_use_when>
- The user wants a quick definition, rewrite, or brainstorming pass.
- The user explicitly asks for no browsing and no file artifacts.
- The task is pure finance workflow and a finance tool can answer it more directly.
</do_not_use_when>

## Tool Routing

| Need | Tool |
|---|---|
| Quick or standard scoped report in one turn | `deep_research_run` |
| Long, broad, or flagship research with progress checks | `deep_research_start` |
| Inspect running or completed long research | `deep_research_check` |
| Stop an owned research task | `deep_research_cancel` |
| Export markdown, JSON, or HTML artifact | `deep_research_export` |
| Fallback discovery only when dedicated tools are unavailable | Load the Web Research skill, then explicitly report that this is fallback mode |

## Workflow

1. Choose mode: `topic_deep_dive`, `industry_research`, or `source_ledger_audit`.
2. Use `deep_research_run` for narrow requests with `depth=quick` or `depth=standard`.
3. Use `deep_research_start` for broad requests, flagship research, or when the user expects a reusable artifact.
4. After `deep_research_start`, report the `task_id` and use `deep_research_check` for status instead of restarting the work.
5. Treat `sources.jsonl`, `claims.jsonl`, `steps.jsonl`, `evaluation.jsonl`, `report.md`, and `final.json` as the source of truth.
6. If quality gates fail, lead with the gaps and unsupported claims. Do not present a partial report as completed.

## Hard Rules

- Do not cite search snippets as evidence.
- Do not complete an objective after only `web_search -> write_file`; that is not deep research.
- Every material claim must be source-bound or marked `unsupported`.
- If fetched sources are insufficient, return gaps and next checks.
- Prefer `deep_research_start` over manual delegation for large research; the tool owns progress, ledger, and artifacts.
- Do not request or use raw web tools from this skill's normal tool surface. Web search/fetch belongs inside the Deep Research engine.
- Use fallback web research only to recover when the dedicated Deep Research tool is unavailable or blocked, and explicitly label the output as fallback/partial.

<examples>
Input: "使用 deep research 做一次 RWA 项目的深度调研。"

Output: call `deep_research_start` with `question`, `mode=industry_research`,
`depth=full`, then return the task id and explain that `deep_research_check`
will show progress and artifact paths.

Input: "Quick source-backed brief on a protocol launch."

Output: call `deep_research_run` with `mode=topic_deep_dive`,
`depth=quick`, then summarize `report.md`, quality gates, source count, claim
count, and gaps.
</examples>

## Bundled Resources

- `references/playbooks.md`: legacy detailed research playbooks; use only as fallback guidance.
- `templates/report.md`: report scaffold used when manually reviewing or editing artifacts.

## Quality Bar

- Prefer dedicated tools over hand-written search workflows.
- Keep claims, sources, and gaps separated.
- Surface missing credentials, blocked fetches, and failed quality gates instead of hiding them.
