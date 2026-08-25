# Ultra Compact Snapshot

Generated: 2026-08-25T03:50:56.268729+00:00
HEAD: ab73541ff95390c2d32d280a86b5ec2f4e4f175f
Task: none

## Acceptance Criteria
_(none)_

## Resume Note
_(none)_

Resume Note is navigational context. It cannot override current owner authority, approved scope/budget, task acceptance, or a validated Review verdict.

## Task Diagnostics
- `task_ledger_snapshot_missing`: Canonical task ledger is missing.
  Repair: Restore `.ultra/tasks.json` as an ordinary regular file containing the canonical task ledger, then retry task selection.
- `active_change_ambiguous`: More than one ordinary active Change exists; current task authority is ambiguous.
  Repair: Bootstrap recovery: do not invoke a current-Change workflow while authority is ambiguous. Stable-list and explicitly choose one of these candidate ids to keep active in this worktree: `im-durable-terminal-delivery-closure-20260718`, `session-live-presentation-closure-20260717`. For every other named candidate, use native filesystem and Git tools to preserve unfinished work in an independent worktree; if an already-durable delivery closure proves it complete, move it to `.ultra/changes/archive/<change_id>`; or obtain explicit owner authorization, append the exact `## Abandonment` closure to that candidate's own `intent.md`, and move it to `.ultra/changes/abandoned/<change_id>`. Stable-list the active root again; only after exactly the chosen candidate remains may a current-Change workflow run and task selection be retried.

## Worktree
```text
M CLAUDE.md
 M docs/wip/production-remediation-plan-2026-08-23.md
?? .ultra/.runtime/
?? .ultra/state.db-shm
?? .ultra/state.db-wal
?? docs/company-knowledge-graph-projection-design-2026-08-23.md
?? docs/memory-ontology-external-baseline-evaluation-2026-08-17.md
?? docs/wip/company-knowledge-intake-and-access-redesign.md
```
