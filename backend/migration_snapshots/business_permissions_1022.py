"""Frozen exact-child authority for native business tool permission responses."""

from migration_snapshots.business_task_child_1022 import (
    _BUSINESS_TASK_CHILD_RUN_BINDING,
    build_session_tenant_binding_function_sql as _previous_sql,
)


def build_session_tenant_binding_function_sql():
    sql = _previous_sql()
    start = sql.index("TG_TABLE_NAME='session_control_inputs'")
    end = sql.index('          ELSIF', start)
    branch = sql[start:end]
    parent = """            AND run.parent_session_id=NEW.session_id::text
            AND run.parent_agent_id=bound_agent_id"""
    binding = """            AND (
              (run.parent_session_id=NEW.session_id::text AND run.parent_agent_id=bound_agent_id)
              """ + _BUSINESS_TASK_CHILD_RUN_BINDING.replace(
        "OR (run.task_type=", "OR (NEW.kind='permission_response' AND run.task_type=", 1) + "\n            )"
    if branch.count(parent) != 1:
        raise RuntimeError('business_permission_authority_anchor_drift')
    return sql[:start] + branch.replace(parent, binding, 1) + sql[end:]
