"""Frozen exact-child Session V2 model/tool/input authority extension."""

from migration_snapshots.session_v2_admission_revision_contract_0716 import (
    build_session_tenant_binding_function_sql as _parent_sql,
)


_PARENT = """            AND run.parent_session_id=NEW.session_id::text
            AND run.parent_agent_id=bound_agent_id"""
_CHILD = """            AND (
              (run.parent_session_id=NEW.session_id::text AND run.parent_agent_id=bound_agent_id)
              OR (run.task_type='delegation' AND replace(lower(run.child_session_id),'-','')=replace(NEW.session_id::text,'-','')
                AND run.child_agent_id=bound_agent_id AND EXISTS (
                  SELECT 1 FROM public.chat_sessions AS child
                  WHERE child.id=NEW.session_id AND child.tenant_id=NEW.tenant_id
                    AND child.agent_id=run.child_agent_id AND child.session_kind='delegation_run'
                    AND child.runtime_task_id=run.id AND child.peer_agent_id=run.parent_agent_id
                    AND child.user_id=run.root_user_id
                    AND replace(child.parent_session_id::text,'-','')=replace(lower(run.parent_session_id),'-','')
                ))
            )"""


def build_session_tenant_binding_function_sql():
    sql = _parent_sql()
    for table in ("session_turn_inputs", "session_model_results", "session_tool_invocations",
        "session_round_obligations", "session_next_round_plans", "session_run_outcomes"):
        start = sql.index(f"TG_TABLE_NAME='{table}'")
        end = sql.index("          ELSIF", start)
        branch = sql[start:end]
        if branch.count(_PARENT) != 1:
            raise RuntimeError("delegation_session_round_authority_anchor_drift")
        sql = sql[:start] + branch.replace(_PARENT, _CHILD, 1) + sql[end:]
    return sql
