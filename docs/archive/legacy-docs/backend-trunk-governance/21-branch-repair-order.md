# 21 Branch Repair Order

## Current Strategy

Do not whole-merge `feature/agent-session-feishu`.

Repair order:

1. Migrate governance documents and adapt them to current Autonomy P0-P6.
2. Migrate architecture test ideas and rewrite them against current code.
3. Fix real red tests in narrow slices.
4. Only then cherry-pick implementation assets by subsystem.

## Subsystem Order

```text
tool runtime / permission
session identifiers / channel message contracts
Feishu canonical identity
context / memory provider
long task runtime
evaluator / self-evolution ledger
```

## Stop Conditions

Stop a branch migration immediately if it:

- Reintroduces `focus.md` as objective source.
- Creates a second trigger execution path.
- Reintroduces `AgentSchedule` or scheduler runtime as active execution.
- Bypasses `ToolRuntimeService`.
- Replaces current prompt/memory evolution work wholesale.
- Fails master regression.
