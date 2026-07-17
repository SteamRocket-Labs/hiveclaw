"""Frozen Peer A2A child-Session authority delta for the 0717 repair."""

from __future__ import annotations

from migration_snapshots.runtime_root_ledger_contract_0716 import (
    build_session_event_contract_function_sql as _build_parent_event_contract,
)


_LEGACY_CHILD_AUTHORITY = """                    legacy_run.child_session_id=NEW.session_id::text
                    AND COALESCE(legacy_run.child_agent_id, legacy_run.parent_agent_id)=NEW.agent_id
"""

_NORMALIZED_LEGACY_CHILD_AUTHORITY = """                    replace(lower(legacy_run.child_session_id),'-','')
                      = replace(lower(NEW.session_id::text),'-','')
                    AND COALESCE(legacy_run.child_agent_id, legacy_run.parent_agent_id)=NEW.agent_id
"""

_LEGACY_PARENT_AUTHORITY = """                    legacy_run.parent_session_id=NEW.session_id::text
                    AND legacy_run.parent_agent_id=NEW.agent_id
"""

_NORMALIZED_LEGACY_PARENT_AUTHORITY = """                    replace(lower(legacy_run.parent_session_id),'-','')
                      = replace(lower(NEW.session_id::text),'-','')
                    AND legacy_run.parent_agent_id=NEW.agent_id
"""

_V2_PARENT_ONLY_RUN_AUTHORITY = """              AND run.parent_session_id=NEW.session_id::text
              AND run.parent_agent_id=NEW.agent_id
"""

_V2_PARENT_OR_CHILD_RUN_AUTHORITY = """              AND (
                (
                  replace(lower(run.parent_session_id),'-','')
                    = replace(lower(NEW.session_id::text),'-','')
                  AND run.parent_agent_id=NEW.agent_id
                )
                OR (
                  replace(lower(run.child_session_id),'-','')
                    = replace(lower(NEW.session_id::text),'-','')
                  AND COALESCE(run.child_agent_id, run.parent_agent_id)=NEW.agent_id
                )
              )
"""


def build_session_event_contract_function_sql() -> str:
    """Authorize only the exact RuntimeTask-bound parent or child Session."""

    sql = _build_parent_event_contract()
    if sql.count(_LEGACY_PARENT_AUTHORITY) != 1:
        raise RuntimeError("peer_a2a_legacy_parent_authority_anchor_drift")
    if sql.count(_LEGACY_CHILD_AUTHORITY) != 1:
        raise RuntimeError("peer_a2a_legacy_child_authority_anchor_drift")
    if sql.count(_V2_PARENT_ONLY_RUN_AUTHORITY) != 1:
        raise RuntimeError("peer_a2a_v2_child_authority_anchor_drift")
    return (
        sql.replace(
            _LEGACY_PARENT_AUTHORITY,
            _NORMALIZED_LEGACY_PARENT_AUTHORITY,
            1,
        )
        .replace(
            _LEGACY_CHILD_AUTHORITY,
            _NORMALIZED_LEGACY_CHILD_AUTHORITY,
            1,
        )
        .replace(
            _V2_PARENT_ONLY_RUN_AUTHORITY,
            _V2_PARENT_OR_CHILD_RUN_AUTHORITY,
            1,
        )
    )
