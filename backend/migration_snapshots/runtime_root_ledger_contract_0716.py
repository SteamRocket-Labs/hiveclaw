"""Frozen child-session authority delta for the Group 3 root ledger revision."""

from __future__ import annotations

from migration_snapshots.session_v2_projection_epoch_contract_0716 import (
    build_session_event_contract_function_sql as _build_parent_event_contract,
)


_PARENT_ONLY_RUN_AUTHORITY = """                AND legacy_run.parent_session_id=NEW.session_id::text
                AND legacy_run.parent_agent_id=NEW.agent_id
"""

_PARENT_OR_CHILD_RUN_AUTHORITY = """                AND (
                  (
                    legacy_run.parent_session_id=NEW.session_id::text
                    AND legacy_run.parent_agent_id=NEW.agent_id
                  )
                  OR (
                    legacy_run.child_session_id=NEW.session_id::text
                    AND COALESCE(legacy_run.child_agent_id, legacy_run.parent_agent_id)=NEW.agent_id
                  )
                )
"""


def build_session_event_contract_function_sql() -> str:
    """Allow a queued RuntimeTask to author its bound child-session evidence."""

    sql = _build_parent_event_contract()
    if sql.count(_PARENT_ONLY_RUN_AUTHORITY) != 1:
        raise RuntimeError("runtime_root_ledger_child_session_authority_anchor_drift")
    return sql.replace(
        _PARENT_ONLY_RUN_AUTHORITY,
        _PARENT_OR_CHILD_RUN_AUTHORITY,
        1,
    )
