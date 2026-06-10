# Memory Routing Reference

The memory pipeline has separate layers. Manual memory writes are only for
durable facts that must survive compression and automatic filtering.

## Save Memory When

- The user gives an explicit persistent preference or correction.
- A durable project constraint must be remembered across sessions.
- A repeated failure pattern should be avoided in future work.
- The user says "remember", "from now on", or equivalent.

## Do Not Save Memory When

- The fact is a temporary task detail.
- The information is already captured in workspace files or the work ledger.
- The item is raw tool output, logs, or an intermediate debug observation.
- The same fact has already been saved.

## Category Routing

- `feedback`: user corrections and hard preferences.
- `constraint`: persistent operating constraints.
- `knowledge`: durable project/domain facts.
- `strategy`: proven approaches.
- `blocked`: approaches that failed and should not be retried blindly.
