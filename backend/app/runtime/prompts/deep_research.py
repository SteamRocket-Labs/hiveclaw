"""Tool descriptions for Deep Research routing."""

from __future__ import annotations


DEEP_RESEARCH_RUN_DESCRIPTION = (
    "Run a source-ledger-backed deep research workflow synchronously for quick or "
    "standard scopes; not for simple lookup. Use web_search/web_fetch for simple "
    "lookups and direct source reads. Deep Research has a preview/confirmation gate: "
    "without explicit user approval it returns a plan, clarifying questions, and "
    "worker topics instead of executing. Produces report.md, sources.jsonl, "
    "claims.jsonl, steps.jsonl, and final.json artifacts."
)

DEEP_RESEARCH_START_DESCRIPTION = (
    "Start a long-running source-ledger-backed deep research workflow. Use this for "
    "full or flagship research that needs fan-out, resumable artifacts, or a long "
    "RuntimeTask; it requires the same preview/confirmation gate before execution. "
    "Creates a RuntimeTask and writes resumable artifacts under runtime_artifacts/long_tasks."
)
