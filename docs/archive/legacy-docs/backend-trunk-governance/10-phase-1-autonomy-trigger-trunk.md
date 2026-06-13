# 10 Autonomy Trigger Trunk

## Current State

Autonomy P0-P6 is complete. The current trunk is:

```text
AgentObjective = objective ledger
AgentTrigger = wake policy
RuntimeTask = attempt ledger
artifact/evaluator = result ledger
focus.md = projection
```

## Non-Negotiable Rules

- Do not reintroduce `focus.md` as a source of truth.
- Do not let scheduled triggers silently become objectives.
- Do not fire objective work without an attempt ledger row.
- Do not expose raw trigger config as the default UI contract.
- Do not reintroduce `AgentSchedule` as an execution runtime; legacy schedules are only a trigger facade.

## Guard Tests

```bash
pytest tests/architecture/test_phase0r_boundaries.py \
       tests/architecture/test_legacy_schedule_trunk.py \
       tests/services/test_autonomous_audit.py \
       tests/services/test_trigger_daemon.py \
       tests/services/test_objective_wake_reconciler.py
```

## Allowed Future Work

- Stronger reconciler for stale objectives.
- Better event-wait lifecycle policies.
- Long-task runtime integration in Harness H4.
