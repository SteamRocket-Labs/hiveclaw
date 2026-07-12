from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4


def _row(table_name: str):
    return SimpleNamespace(
        tenant_id=None,
        __mapper__=SimpleNamespace(local_table=SimpleNamespace(name=table_name)),
    )


def test_before_flush_stamps_missing_tenant_from_trusted_session_scope() -> None:
    from app.database import _RLS_TENANT_INFO_KEY, stamp_new_tenant_owned_rows

    tenant_id = uuid4()
    runtime_task = _row("runtime_tasks")
    workflow_step = _row("workflow_steps")
    session = SimpleNamespace(
        info={_RLS_TENANT_INFO_KEY: str(tenant_id)},
        new=[runtime_task, workflow_step],
    )

    stamp_new_tenant_owned_rows(session)

    assert runtime_task.tenant_id == tenant_id
    assert workflow_step.tenant_id == tenant_id
    assert isinstance(runtime_task.tenant_id, UUID)


def test_before_flush_never_invents_scope_for_empty_or_bypass_sessions() -> None:
    from app.database import _RLS_BYPASS_VALUE, _RLS_TENANT_INFO_KEY, stamp_new_tenant_owned_rows

    for scope in ("", _RLS_BYPASS_VALUE, None):
        row = _row("runtime_tasks")
        session = SimpleNamespace(info={_RLS_TENANT_INFO_KEY: scope}, new=[row])
        stamp_new_tenant_owned_rows(session)
        assert row.tenant_id is None


def test_before_flush_does_not_stamp_platform_shared_or_operator_nullable_rows() -> None:
    from app.database import _RLS_TENANT_INFO_KEY, stamp_new_tenant_owned_rows

    rows = [_row("tools"), _row("users"), _row("audit_logs")]
    session = SimpleNamespace(info={_RLS_TENANT_INFO_KEY: str(uuid4())}, new=rows)

    stamp_new_tenant_owned_rows(session)

    assert all(row.tenant_id is None for row in rows)
