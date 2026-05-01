# Delegation Quality Reference

Delegation should reduce critical-path work without losing accountability.

## Good Delegation Briefs Include

- Goal: exact deliverable and success criteria.
- Scope: files, systems, or research boundaries.
- Constraints: tools, language, style, and things to avoid.
- Evidence: tests, citations, command output, or changed file list.
- Return format: concise result the parent agent can integrate.

## Do Not Delegate

- The immediate blocker on the current critical path.
- Work that requires private context not included in the brief.
- Broad ownership without a clear output.
- Multiple agents writing the same files.

## Follow-Up Rules

- Store the returned `task_id`.
- Check status before spawning duplicate tasks.
- Cancel stale tasks when the user changes direction.
- Schedule a trigger only when the work must continue later.
