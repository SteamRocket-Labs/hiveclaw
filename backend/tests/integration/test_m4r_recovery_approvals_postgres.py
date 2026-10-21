"""Invalid recovery locators cannot consume a real tenant-bound approval."""

from copy import deepcopy
import uuid

import pytest

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g22_artifact_versions_postgres import delivered  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("fault", ["changed", "missing", "null_marker"])
async def test_changed_relocation_is_denied_before_ticket_consumption(delivered, owner_sessionmaker,  # noqa: F811
    app_user_sessionmaker, monkeypatch, fault):
    from app.config import get_settings
    from app.models.agent import Agent
    from app.models.audit import ApprovalRequest
    from app.models.user import User
    from app.models.system_settings import SystemSetting
    from app.scripts import self_hosted_recovery as recovery
    from app.services.approval_service import ApprovalService
    from app.services.approval_ticket import ApprovalTicketError, build_approval_execution_envelope, consume_approval_ticket
    from app.tools.runtime import ToolExecutionContext

    d = delivered
    old = d.root.parent / "former-volume"
    key = "synthetic-relocation-key"
    monkeypatch.setattr(get_settings(), "SECRETS_MASTER_KEY", key)
    mapping = {"schema": "hive.workspace_relocation.v1", "manifest_sha256": "a" * 64,
        "source_roots": [str(old)], "target_roots": [str(d.root.parent)]}
    mapping["mac"] = recovery._relocation_mac(mapping, key)
    journal = {"schema": "hive.self_hosted_restore.v1", "status": "restored",
        "manifest_sha256": "a" * 64, "workspace_relocation": mapping}
    control = d.root.parent / recovery.CONTROL
    recovery.write_json(control, journal)
    async with owner_sessionmaker() as db:
        marker = await db.get(SystemSetting, recovery.RECOVERY_MARKER_KEY)
        if marker is None:
            db.add(SystemSetting(key=recovery.RECOVERY_MARKER_KEY, value={"manifest_sha256": "a" * 64}))
        else:
            marker.value = {"manifest_sha256": "a" * 64}
        (await db.get(User, d.user.id)).role = "org_admin"
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        reviewer = await db.get(User, d.user.id)
        context = ToolExecutionContext(agent_id=d.agent, tenant_id=str(d.tenant), user_id=d.user.id,
            session_id=str(d.session), workspace=old / str(d.agent), runtime_task_id=str(d.run))
        envelope = build_approval_execution_envelope(context=context, tool_call_id="restore-scope", emit_runtime_hooks=True)
        ticket = await ApprovalService().request_approval(db, await db.get(Agent, d.agent), action_type="file.write",
            details={"tool": "write_file", "args": {"path": "workspace/should-not-run.txt", "content": "DENIED"},
                "requested_by": str(d.user.id), "execution_envelope": envelope})
        await db.commit()
        await pin_rls_tenant_context(db, d.tenant)
        await ApprovalService().resolve_approval(db, uuid.UUID(ticket["approval_id"]), reviewer, "approve")
        await db.commit()
    corrupted = deepcopy(journal)
    corrupted["workspace_relocation"]["source_roots"] = ["/another-installation"]
    if fault == "changed":
        recovery.write_json(control, corrupted, replace=True)
    elif fault == "missing":
        control.unlink()
    else:
        async with owner_sessionmaker() as db:
            (await db.get(SystemSetting, recovery.RECOVERY_MARKER_KEY)).value = None
            await db.commit()
    with pytest.raises(ApprovalTicketError, match="workspace recovery metadata"):
        await consume_approval_ticket(approval_id=uuid.UUID(ticket["approval_id"]),
            expected_agent_id=d.agent, expected_user_id=d.user.id)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        row = await db.get(ApprovalRequest, uuid.UUID(ticket["approval_id"]))
        assert row.consumed_at is None and row.execution_status == "queued"
    assert not (d.root / "workspace/should-not-run.txt").exists()
    assert not old.exists()
