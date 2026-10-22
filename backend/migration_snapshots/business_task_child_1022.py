"""Frozen addition of the exact business execution-session authority frame."""

from migration_snapshots.delegation_session_rounds_1017 import (
    build_session_tenant_binding_function_sql as _previous_authority_sql,
)

_BUSINESS_TASK_CHILD_RUN_BINDING = """OR (run.task_type='business_task' AND run.parent_agent_id=bound_agent_id
                AND run.child_agent_id=run.parent_agent_id
                AND replace(lower(run.child_session_id),'-','')=replace(NEW.session_id::text,'-','')
                AND EXISTS (SELECT 1 FROM public.chat_sessions AS child
                  WHERE child.id=NEW.session_id AND child.tenant_id=NEW.tenant_id
                    AND child.agent_id=run.parent_agent_id AND child.session_kind='user_task'
                    AND child.source_channel='task' AND child.runtime_task_id=run.id
                    AND child.user_id=run.root_user_id AND child.user_id IS NOT NULL))
              """


def build_session_tenant_binding_function_sql():
    previous = _previous_authority_sql()
    anchor = "OR (run.task_type='delegation'"
    assert previous.count(anchor) == 6
    return previous.replace(anchor, _BUSINESS_TASK_CHILD_RUN_BINDING + anchor)
