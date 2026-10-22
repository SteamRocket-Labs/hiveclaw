"""Frozen exact-child authority for delegated native permission responses."""

from migration_snapshots.business_permissions_1022 import build_session_tenant_binding_function_sql as _previous_sql
from migration_snapshots.business_task_child_1022 import _BUSINESS_TASK_CHILD_RUN_BINDING
from migration_snapshots.delegation_session_rounds_1017 import _CHILD


def build_session_tenant_binding_function_sql():
    sql = _previous_sql()
    start = sql.index("TG_TABLE_NAME='session_control_inputs'")
    end = sql.index('          ELSIF', start)
    branch = sql[start:end]
    previous = """            AND (
              (run.parent_session_id=NEW.session_id::text AND run.parent_agent_id=bound_agent_id)
              """ + _BUSINESS_TASK_CHILD_RUN_BINDING.replace(
        "OR (run.task_type=", "OR (NEW.kind='permission_response' AND run.task_type=", 1) + "\n            )"
    binding = _CHILD.replace("OR (run.task_type='delegation'", _BUSINESS_TASK_CHILD_RUN_BINDING.replace(
        "OR (run.task_type=", "OR (NEW.kind='permission_response' AND run.task_type=", 1)
        + "OR (NEW.kind='permission_response' AND run.task_type='delegation'", 1)
    if branch.count(previous) != 1:
        raise RuntimeError('delegation_permission_authority_anchor_drift')
    return sql[:start] + branch.replace(previous, binding, 1) + sql[end:]
