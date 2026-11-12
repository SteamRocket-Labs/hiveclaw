"""Source refusals retain their page/delivery contract and identify the leaf."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from loguru import logger

from app.database import tenant_scoped_session
from app.models.user import User
from tests.integration.test_artifact_publication_transactions import publish_report, stage_report
from tests.services.test_session_tool_artifact_settlement import _seed

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("boundary", ["artifact_delivery", "read_runtime_result"])
async def test_source_refusal_logs_leaf_without_changing_response(
    owner_sessionmaker, monkeypatch, tmp_path, boundary,
):
    from app.config import get_settings
    from app.services import content_input_receipts, runtime_result_sources
    from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.tools.handlers import context_resources
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    tenant, user, agent, session = await _seed(owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    root = tmp_path / str(agent)
    async with owner_sessionmaker() as db:
        leaf = await stage_report(db, tenant=tenant, user=user, agent=agent, session=session,
            root=root, path="workspace/private-source.md")
        dependency = {"source_kind": "workspace_artifact", "source_id": str(leaf.id),
            "source_version": leaf.content_sha256}
        middle = await stage_report(db, tenant=tenant, user=user, agent=agent, session=session,
            root=root, path="workspace/intermediate.md", dependencies=[dependency])
        middle_ref = {"source_kind": "workspace_artifact", "source_id": str(middle.id),
            "source_version": middle.content_sha256}
        await stage_report(db, tenant=tenant, user=user, agent=agent, session=session,
            root=root, path="workspace/report.md", dependencies=[middle_ref])
        await db.commit()
    # A real byte mismatch, with the receipt and authority left intact.
    (root / "workspace/private-source.md").write_text("PRIVATE_CHANGED_BODY")
    warnings = []
    sink = logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")
    try:
        if boundary == "artifact_delivery":
            async with tenant_scoped_session(tenant, session_factory=owner_sessionmaker) as db:
                parts, errors = await publish_report(db, SimpleNamespace(tenant=tenant, agent=agent,
                    users=[user], sessions=[session], paths=["workspace/report.md"], root=root), 0)
            assert parts == []
            assert errors == [{"path": "workspace/report.md", "status": "denied",
                "reason_code": "content_source_changed", "retryable": False,
                "recovery_action": "reconcile_artifact_sources"}]
        else:
            async with owner_sessionmaker() as db:
                outbox_id = await enqueue_completion_notification(db, CompletionNotification(
                    tenant_id=tenant, source_kind="subagent", source_run_id=str(uuid4()),
                    parent_session_id=session, parent_agent_id=agent, parent_user_id=user,
                    terminal_status="completed", task_type="subagent", summary="SUMMARY_SENTINEL",
                    delivery_mode="parent_continuation", artifacts=[], metadata={}))
                await db.commit()
                result_ref = (await db.get(RuntimeNotificationOutbox, outbox_id)).result_ref

            async def check_sources(db, _request, _result):
                # Isolate the boundary dispatch; the nested receipt/byte refusal
                # below runs the real dependency validator in its scoped DB.
                reader = await db.get(User, user)
                await content_input_receipts.validate_browser_input_dependencies(db, tenant, reader, [middle_ref])

            monkeypatch.setattr(context_resources, "async_session", owner_sessionmaker)
            monkeypatch.setattr(runtime_result_sources, "validate_runtime_result_sources", check_sources)
            page = json.loads(await context_resources.read_runtime_result(ToolExecutionRequest(
                tool_name="read_runtime_result", arguments={"result_ref": result_ref, "limit": 12000},
                context=ToolExecutionContext(agent_id=agent, user_id=user, tenant_id=str(tenant),
                    session_id=str(session), workspace=root))))
            assert page == {"schema": "hive.runtime_result_page.v1", "status": "source_access_denied",
                "reason_code": "content_source_changed", "result_ref": result_ref, "retryable": False,
                "recovery_action": "reconcile_result_sources"}
    finally:
        logger.remove(sink)
    assert warnings == [f"Source refusal operation={boundary} code=content_source_changed "
        f"message=The canonical source bytes no longer match the receipt. "
        f"source_kind=workspace_artifact source_id={leaf.id}"]
    assert all(marker not in warnings[0] for marker in ("PRIVATE_CHANGED_BODY", "SUMMARY_SENTINEL",
        "private-source.md", "intermediate.md", "report.md", "Tool Artifact", str(tmp_path)))


async def test_source_refusal_log_omits_non_uuid_components():
    from app.services import content_input_receipts as receipts

    identity = uuid4()
    dependency = {"source_kind": "future_source", "source_id": f"{identity}\nworkspace/PRIVATE_NAME",
        "source_version": "v1"}
    warnings = []
    sink = logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")
    try:
        with pytest.raises(receipts.ContentInputReceiptError) as caught:
            await receipts.validate_browser_input_dependencies(SimpleNamespace(get_transaction=lambda: None),
                uuid4(), SimpleNamespace(id=uuid4()), [dependency])
        assert caught.value.code == "content_source_unavailable" and caught.value.status_code == 503
        assert caught.value.dependency == dependency
        receipts.log_source_refusal(caught.value, operation="read_runtime_result")
    finally:
        logger.remove(sink)
    assert len(warnings) == 1 and f"source_id={identity}" in warnings[0]
    assert "PRIVATE_NAME" not in warnings[0] and "workspace/" not in warnings[0] and "\n" not in warnings[0]
