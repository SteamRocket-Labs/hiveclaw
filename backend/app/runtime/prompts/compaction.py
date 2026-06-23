"""Prompt fragments for session compaction and continuation handoff."""

from __future__ import annotations


COMPACTION_LONG_RUN_STATE_CONTRACT = """\
- progress claims require evidence: keep the tool result, test output, artifact,
  diff, or explicit blocker that supports each reported status.
- preserve assumptions, tradeoffs, and user-approved scope when they affect the
  next turn's allowed work.
- do not convert tactical state into durable memory; summarize it as current
  session state only.
- do not ask the next turn to reveal hidden reasoning; preserve decisions,
  evidence, and unresolved questions instead.
- resume from the latest explicit request, not from an older tangent."""
