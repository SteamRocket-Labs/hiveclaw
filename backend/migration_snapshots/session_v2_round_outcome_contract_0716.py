"""Frozen SQL contract for immutable Session V2 next-round plan generations."""

from __future__ import annotations


UPGRADE_SQL = """
ALTER TABLE session_next_round_plans
  ADD COLUMN plan_generation integer NOT NULL DEFAULT 1;

ALTER TABLE session_next_round_plans
  DROP CONSTRAINT uq_session_next_round_plan_round;

ALTER TABLE session_next_round_plans
  ADD CONSTRAINT uq_session_next_round_plan_generation
  UNIQUE (run_id, next_round_id, plan_generation);

ALTER TABLE session_next_round_plans
  ADD CONSTRAINT uq_session_next_round_plan_hash
  UNIQUE (run_id, next_round_id, plan_hash);

CREATE UNIQUE INDEX uq_session_next_round_plan_current
  ON session_next_round_plans (run_id, next_round_id)
  WHERE state IN ('committed','dispatched','needs_reconciliation');
"""

UPGRADE_SQL_STATEMENTS = (
    "ALTER TABLE session_next_round_plans ADD COLUMN plan_generation integer NOT NULL DEFAULT 1",
    "ALTER TABLE session_next_round_plans DROP CONSTRAINT uq_session_next_round_plan_round",
    """
    ALTER TABLE session_next_round_plans
      ADD CONSTRAINT uq_session_next_round_plan_generation
      UNIQUE (run_id, next_round_id, plan_generation)
    """,
    """
    ALTER TABLE session_next_round_plans
      ADD CONSTRAINT uq_session_next_round_plan_hash
      UNIQUE (run_id, next_round_id, plan_hash)
    """,
    """
    CREATE UNIQUE INDEX uq_session_next_round_plan_current
      ON session_next_round_plans (run_id, next_round_id)
      WHERE state IN ('committed','dispatched','needs_reconciliation')
    """,
)


DOWNGRADE_GUARD_SQL = """
LOCK TABLE session_next_round_plans IN SHARE MODE;

DO $session_v2_round_outcome_downgrade_guard$
BEGIN
  IF EXISTS (
    SELECT 1 FROM session_next_round_plans WHERE plan_generation <> 1
  ) OR EXISTS (
    SELECT run_id, next_round_id
    FROM session_next_round_plans
    GROUP BY run_id, next_round_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION
      'session_v2_round_outcome_downgrade_blocked: immutable plan generation evidence exists'
      USING ERRCODE='23514';
  END IF;
END;
$session_v2_round_outcome_downgrade_guard$;
"""

DOWNGRADE_GUARD_SQL_STATEMENTS = (
    "LOCK TABLE session_next_round_plans IN SHARE MODE",
    """
    DO $session_v2_round_outcome_downgrade_guard$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM session_next_round_plans WHERE plan_generation <> 1
      ) OR EXISTS (
        SELECT run_id, next_round_id
        FROM session_next_round_plans
        GROUP BY run_id, next_round_id
        HAVING count(*) > 1
      ) THEN
        RAISE EXCEPTION
          'session_v2_round_outcome_downgrade_blocked: immutable plan generation evidence exists'
          USING ERRCODE='23514';
      END IF;
    END;
    $session_v2_round_outcome_downgrade_guard$
    """,
)


DOWNGRADE_SQL = """
DROP INDEX uq_session_next_round_plan_current;

ALTER TABLE session_next_round_plans
  DROP CONSTRAINT uq_session_next_round_plan_hash;

ALTER TABLE session_next_round_plans
  DROP CONSTRAINT uq_session_next_round_plan_generation;

ALTER TABLE session_next_round_plans
  ADD CONSTRAINT uq_session_next_round_plan_round
  UNIQUE (run_id, next_round_id);

ALTER TABLE session_next_round_plans
  DROP COLUMN plan_generation;
"""

DOWNGRADE_SQL_STATEMENTS = (
    "DROP INDEX uq_session_next_round_plan_current",
    "ALTER TABLE session_next_round_plans DROP CONSTRAINT uq_session_next_round_plan_hash",
    "ALTER TABLE session_next_round_plans DROP CONSTRAINT uq_session_next_round_plan_generation",
    """
    ALTER TABLE session_next_round_plans
      ADD CONSTRAINT uq_session_next_round_plan_round
      UNIQUE (run_id, next_round_id)
    """,
    "ALTER TABLE session_next_round_plans DROP COLUMN plan_generation",
)


__all__ = [
    "DOWNGRADE_GUARD_SQL",
    "DOWNGRADE_GUARD_SQL_STATEMENTS",
    "DOWNGRADE_SQL",
    "DOWNGRADE_SQL_STATEMENTS",
    "UPGRADE_SQL",
    "UPGRADE_SQL_STATEMENTS",
]
