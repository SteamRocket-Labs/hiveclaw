from __future__ import annotations

from types import SimpleNamespace
import uuid
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

import app.api.admin as admin_api
from app.core.security import get_current_user
from app.database import get_db
from tests.integration.conftest import (  # noqa: F401
    migrated_pg_url,
    owner_engine,
    owner_sessionmaker,
    pg_container,
)


class _FakeDB:
    def __init__(self):
        self.sync_session = SimpleNamespace(info={})
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return SimpleNamespace()


def _client(role: str = "platform_admin") -> tuple[TestClient, _FakeDB]:
    app = FastAPI()
    app.include_router(admin_api.router)
    fake_db = _FakeDB()

    async def override_user():
        return SimpleNamespace(id=uuid4(), role=role, tenant_id=uuid4(), username="admin")

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app), fake_db


def test_admin_runtime_reconciliation_requires_platform_admin() -> None:
    client, _fake_db = _client(role="org_admin")

    response = client.get("/admin/runtime-reconciliation", params={"tenant_id": str(uuid4())})

    assert response.status_code == 403


def test_admin_runtime_reconciliation_rejects_non_evidentiary_reason() -> None:
    client, _fake_db = _client()

    response = client.post(
        f"/admin/runtime-reconciliation/{uuid4()}/action",
        params={"tenant_id": str(uuid4())},
        json={
            "action": "mark_resolved",
            "reason": "ok",
            "confirmed": True,
            "evidence_digest": "a" * 64,
            "frame_decisions": [],
        },
    )

    assert response.status_code == 422


def test_admin_runtime_reconciliation_requires_explicit_evidence_confirmation() -> None:
    client, _fake_db = _client()

    response = client.post(
        f"/admin/runtime-reconciliation/{uuid4()}/action",
        params={"tenant_id": str(uuid4())},
        json={
            "action": "mark_resolved",
            "reason": "operator verified evidence",
            "confirmed": False,
            "evidence_digest": "a" * 64,
            "frame_decisions": [],
        },
    )

    assert response.status_code == 422


def test_admin_runtime_reconciliation_routes_delegate_to_service(monkeypatch) -> None:
    from app import database

    tenant_id = uuid4()
    task_id = uuid4()
    captured = {}

    async def fake_list(db, *, tenant_id, status, limit, agent_id=None):
        captured["list"] = {"tenant_id": tenant_id, "status": status, "limit": limit, "agent_id": agent_id}
        return [{"task_id": str(task_id), "status": "needs_reconciliation"}]

    async def fake_get(db, *, tenant_id, task_id):
        captured["get"] = {"tenant_id": tenant_id, "task_id": task_id}
        return {"task_id": str(task_id), "status": "needs_reconciliation"}

    async def fake_apply(
        db,
        *,
        tenant_id,
        task_id,
        action,
        reason,
        actor_user_id,
        confirmed,
        evidence_digest,
        frame_decisions,
        operation_id,
    ):
        captured["apply"] = {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "action": action,
            "reason": reason,
            "actor_user_id": actor_user_id,
            "confirmed": confirmed,
            "evidence_digest": evidence_digest,
            "frame_decisions": frame_decisions,
            "operation_id": operation_id,
        }
        return {"task_id": str(task_id), "status": "reconciled", "action": action}

    monkeypatch.setattr(admin_api, "list_runtime_reconciliation_tasks", fake_list)
    monkeypatch.setattr(admin_api, "get_runtime_reconciliation_task", fake_get)
    monkeypatch.setattr(admin_api, "apply_runtime_reconciliation_action", fake_apply)

    client, fake_db = _client()
    list_resp = client.get("/admin/runtime-reconciliation", params={"tenant_id": str(tenant_id), "limit": "25"})
    get_resp = client.get(f"/admin/runtime-reconciliation/{task_id}", params={"tenant_id": str(tenant_id)})
    action_resp = client.post(
        f"/admin/runtime-reconciliation/{task_id}/action",
        params={"tenant_id": str(tenant_id)},
        json={
            "action": "mark_resolved",
            "reason": "operator verified no duplicate side effect",
            "confirmed": True,
            "evidence_digest": "a" * 64,
            "frame_decisions": [
                {
                    "runtime_task_id": str(task_id),
                    "tool_call_id": "call-1",
                    "tool_name": "send_email",
                    "decision": "mark_resolved",
                }
            ],
            "operation_id": "operation-1",
        },
    )

    assert list_resp.status_code == 200
    assert list_resp.json()[0]["task_id"] == str(task_id)
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "needs_reconciliation"
    assert action_resp.status_code == 200
    assert action_resp.json()["status"] == "reconciled"
    assert captured["list"]["tenant_id"] == tenant_id
    assert captured["list"]["limit"] == 25
    assert captured["apply"]["action"] == "mark_resolved"
    assert captured["apply"]["confirmed"] is True
    assert captured["apply"]["evidence_digest"] == "a" * 64
    assert captured["apply"]["frame_decisions"][0]["decision"] == "mark_resolved"
    assert captured["apply"]["operation_id"] == "operation-1"
    assert fake_db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)
    assert any(f"SET LOCAL app.current_tenant_id = '{tenant_id}'" in stmt for stmt in fake_db.statements)


def test_admin_runtime_reconciliation_returns_409_for_specialized_business_task(monkeypatch) -> None:
    async def reject_specialized(*_args, **_kwargs):
        raise admin_api.RuntimeReconciliationConflict(
            "business_task reconciliation must use its specialized endpoint: /agents/agent-1/tasks/task-1/reconcile"
        )

    monkeypatch.setattr(admin_api, "apply_runtime_reconciliation_action", reject_specialized)
    client, _fake_db = _client()
    response = client.post(
        f"/admin/runtime-reconciliation/{uuid4()}/action",
        params={"tenant_id": str(uuid4())},
        json={
            "action": "mark_resolved",
            "reason": "use the specialized task reconciler",
            "confirmed": True,
            "evidence_digest": "a" * 64,
            "frame_decisions": [
                {
                    "runtime_task_id": str(uuid4()),
                    "tool_call_id": "call-1",
                    "tool_name": "send_email",
                    "decision": "mark_resolved",
                }
            ],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"].endswith("/agents/agent-1/tasks/task-1/reconcile")


def test_admin_delivery_reconciliation_requires_platform_admin() -> None:
    client, _fake_db = _client(role="org_admin")

    response = client.get(
        "/admin/runtime-notification-deliveries",
        params={"tenant_id": str(uuid4()), "status": "dead_letter"},
    )

    assert response.status_code == 403


def test_admin_delivery_reconciliation_list_and_retry_are_delivery_only(monkeypatch) -> None:
    from app import database

    tenant_id = uuid4()
    delivery_id = uuid4()
    captured = {}

    async def fake_list(db, *, tenant_id, status, limit):
        captured["list"] = {"db": db, "tenant_id": tenant_id, "status": status, "limit": limit}
        return [
            {
                "delivery_id": str(delivery_id),
                "status": "dead_letter",
                "source_kind": "subagent",
                "source_run_id": str(uuid4()),
                "execution_terminal_status": "completed",
                "delivery_only": True,
                "retryable": True,
            }
        ]

    async def fake_retry(db, *, tenant_id, delivery_id, reason, actor_user_id):
        captured["retry"] = {
            "db": db,
            "tenant_id": tenant_id,
            "delivery_id": delivery_id,
            "reason": reason,
            "actor_user_id": actor_user_id,
        }
        return {
            "delivery_id": str(delivery_id),
            "status": "pending",
            "execution_terminal_status": "completed",
            "delivery_only": True,
            "retryable": False,
        }

    monkeypatch.setattr(admin_api, "list_runtime_notification_delivery_reconciliations", fake_list)
    monkeypatch.setattr(admin_api, "retry_runtime_notification_delivery", fake_retry)
    client, fake_db = _client()

    listed = client.get(
        "/admin/runtime-notification-deliveries",
        params={"tenant_id": str(tenant_id), "status": "dead_letter", "limit": 25},
    )
    retried = client.post(
        f"/admin/runtime-notification-deliveries/{delivery_id}/retry",
        params={"tenant_id": str(tenant_id)},
        json={
            "reason": "operator repaired the exact delivery target authority",
            "confirmed": True,
        },
    )

    assert listed.status_code == 200
    assert listed.json()[0]["delivery_only"] is True
    assert listed.json()[0]["execution_terminal_status"] == "completed"
    assert retried.status_code == 200
    assert retried.json()["status"] == "pending"
    assert retried.json()["delivery_only"] is True
    assert captured["list"]["tenant_id"] == tenant_id
    assert captured["retry"]["delivery_id"] == delivery_id
    assert captured["retry"]["reason"].startswith("operator repaired")
    assert fake_db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)


def test_admin_delivery_retry_requires_explicit_confirmation() -> None:
    client, _fake_db = _client()

    response = client.post(
        f"/admin/runtime-notification-deliveries/{uuid4()}/retry",
        params={"tenant_id": str(uuid4())},
        json={"reason": "authority was repaired and verified", "confirmed": False},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/admin/runtime-notification-deliveries/{delivery_id}/retry",
        "/admin/workflow-completion-deliveries/{delivery_id}/retry",
    ],
)
def test_admin_delivery_retry_rejects_blank_or_short_trimmed_reason(path: str, monkeypatch) -> None:
    async def must_not_retry(*_args, **_kwargs):
        raise AssertionError("invalid evidence reason must be rejected before service execution")

    monkeypatch.setattr(admin_api, "retry_runtime_notification_delivery", must_not_retry)

    class MustNotRetryWorkflow:
        async def retry_dead_letter(self, **_kwargs):
            raise AssertionError("invalid evidence reason must be rejected before service execution")

    monkeypatch.setattr(admin_api, "WorkflowCompletionOutboxService", MustNotRetryWorkflow)
    client, _fake_db = _client()
    for reason in ("        ", "  short  "):
        response = client.post(
            path.format(delivery_id=uuid4()),
            params={"tenant_id": str(uuid4())},
            json={"reason": reason, "confirmed": True},
        )
        assert response.status_code == 422


def test_admin_delivery_retry_strips_reason_before_service(monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def fake_retry(db, *, tenant_id, delivery_id, reason, actor_user_id):
        del db, tenant_id, delivery_id, actor_user_id
        captured["reason"] = reason
        return {"status": "pending"}

    monkeypatch.setattr(admin_api, "retry_runtime_notification_delivery", fake_retry)
    client, _fake_db = _client()
    response = client.post(
        f"/admin/runtime-notification-deliveries/{uuid4()}/retry",
        params={"tenant_id": str(uuid4())},
        json={"reason": "  verified repaired authority  ", "confirmed": True},
    )
    assert response.status_code == 200
    assert captured["reason"] == "verified repaired authority"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_admin_generic_retry_route_real_pg_rejects_inactive_target_owner(
    owner_sessionmaker,  # noqa: F811
) -> None:
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Admin route authority", slug=f"admin-route-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            User(
                id=user_id,
                username=f"admin-route-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@admin-route.test",
                password_hash="x",
                display_name="Inactive Target Owner",
                tenant_id=tenant_id,
                role="platform_admin",
                is_active=False,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="route-agent", creator_id=user_id))
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Route authority",
            )
        )
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="subagent",
                status="completed",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=user_id,
                result_summary="completed",
                metadata_json={},
            )
        )
        outbox_id = await enqueue_completion_notification(
            db,
            CompletionNotification(
                tenant_id=tenant_id,
                source_kind="subagent",
                source_run_id=str(task_id),
                parent_session_id=session_id,
                parent_agent_id=agent_id,
                parent_user_id=user_id,
                terminal_status="completed",
                task_type="subagent",
                summary="completed",
            ),
        )
        row = await db.get(RuntimeNotificationOutbox, outbox_id, with_for_update=True)
        assert row is not None
        row.status = "dead_letter"

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(HTTPException) as exc_info:
            await admin_api.retry_runtime_notification_delivery_route(
                delivery_id=outbox_id,
                payload=admin_api.RuntimeNotificationDeliveryRetryRequest(
                    reason="verified repaired authority",
                    confirmed=True,
                ),
                tenant_id=tenant_id,
                current_user=SimpleNamespace(id=user_id),
                db=db,
            )
        assert exc_info.value.status_code == 409
        assert "target authority" in str(exc_info.value.detail)


def test_admin_allows_resumable_system_plan_session_projection_delivery_retry(monkeypatch) -> None:
    tenant_id = uuid4()
    delivery_id = uuid4()
    runtime_task_id = uuid4()

    async def fake_list(db, *, tenant_id, status, limit):
        del db, status, limit
        return [
            {
                "delivery_id": str(delivery_id),
                "tenant_id": str(tenant_id),
                "source_kind": "system_plan_run",
                "source_run_id": str(runtime_task_id),
                "task_type": "system_plan_run",
                "status": "dead_letter",
                "execution_terminal_status": "resumable",
                "delivery_only": True,
                "does_not_rerun_execution": True,
                "retryable": True,
                "attempt_count": 8,
            }
        ]

    async def fake_retry(db, *, tenant_id, delivery_id, reason, actor_user_id):
        del db, reason, actor_user_id
        return {
            "delivery_id": str(delivery_id),
            "tenant_id": str(tenant_id),
            "source_kind": "system_plan_run",
            "source_run_id": str(runtime_task_id),
            "task_type": "system_plan_run",
            "status": "pending",
            "execution_terminal_status": "resumable",
            "delivery_only": True,
            "does_not_rerun_execution": True,
            "retryable": False,
            "attempt_count": 0,
        }

    monkeypatch.setattr(admin_api, "list_runtime_notification_delivery_reconciliations", fake_list)
    monkeypatch.setattr(admin_api, "retry_runtime_notification_delivery", fake_retry)
    client, _fake_db = _client()

    listed = client.get(
        "/admin/runtime-notification-deliveries",
        params={"tenant_id": str(tenant_id), "status": "dead_letter"},
    )
    retried = client.post(
        f"/admin/runtime-notification-deliveries/{delivery_id}/retry",
        params={"tenant_id": str(tenant_id)},
        json={
            "reason": "operator verified the System Plan session projection target",
            "confirmed": True,
        },
    )

    assert listed.status_code == 200
    assert listed.json()[0]["execution_terminal_status"] == "resumable"
    assert listed.json()[0]["retryable"] is True
    assert listed.json()[0]["does_not_rerun_execution"] is True
    assert retried.status_code == 200
    assert retried.json()["status"] == "pending"
    assert retried.json()["retryable"] is False


def test_admin_workflow_completion_delivery_requires_platform_admin() -> None:
    client, _fake_db = _client(role="org_admin")

    response = client.get(
        "/admin/workflow-completion-deliveries",
        params={"tenant_id": str(uuid4()), "status": "dead_letter"},
    )

    assert response.status_code == 403


def test_admin_workflow_completion_delivery_list_and_retry_are_delivery_only(monkeypatch) -> None:
    tenant_id = uuid4()
    delivery_id = uuid4()
    run_id = uuid4()
    actor_ids: list[object] = []

    class StubWorkflowCompletionOutboxService:
        async def list_dead_letters(self, *, tenant_id, limit):
            return [
                {
                    "delivery_id": str(delivery_id),
                    "tenant_id": str(tenant_id),
                    "source_kind": "workflow_completion",
                    "source_run_id": str(run_id),
                    "status": "dead_letter",
                    "execution_terminal_status": "completed",
                    "delivery_only": True,
                    "does_not_rerun_execution": True,
                    "retryable": True,
                    "attempt_count": 8,
                    "last_error": "coordination target unavailable",
                    "authority_snapshot": {
                        "valid": True,
                        "tenant_id": str(tenant_id),
                        "agent_id": "workflow-target-agent",
                    },
                }
            ]

        async def retry_dead_letter(self, *, tenant_id, outbox_id, actor_user_id, reason):
            actor_ids.append(actor_user_id)
            assert outbox_id == delivery_id
            assert reason == "coordination delivery authority repaired"
            return {
                "delivery_id": str(outbox_id),
                "tenant_id": str(tenant_id),
                "source_kind": "workflow_completion",
                "source_run_id": str(run_id),
                "status": "pending",
                "execution_terminal_status": "completed",
                "delivery_only": True,
                "does_not_rerun_execution": True,
                "retryable": False,
            }

    monkeypatch.setattr(admin_api, "WorkflowCompletionOutboxService", StubWorkflowCompletionOutboxService)
    client, _fake_db = _client()

    listed = client.get(
        "/admin/workflow-completion-deliveries",
        params={"tenant_id": str(tenant_id), "status": "dead_letter", "limit": 25},
    )
    retried = client.post(
        f"/admin/workflow-completion-deliveries/{delivery_id}/retry",
        params={"tenant_id": str(tenant_id)},
        json={"reason": "coordination delivery authority repaired", "confirmed": True},
    )

    assert listed.status_code == 200
    assert listed.json()[0]["source_kind"] == "workflow_completion"
    assert listed.json()[0]["source_run_id"] == str(run_id)
    assert listed.json()[0]["delivery_only"] is True
    assert listed.json()[0]["authority_snapshot"]["valid"] is True
    assert retried.status_code == 200
    assert retried.json()["status"] == "pending"
    assert retried.json()["does_not_rerun_execution"] is True
    assert len(actor_ids) == 1


def test_admin_workflow_completion_delivery_retry_requires_explicit_confirmation() -> None:
    client, _fake_db = _client()

    response = client.post(
        f"/admin/workflow-completion-deliveries/{uuid4()}/retry",
        params={"tenant_id": str(uuid4())},
        json={"reason": "coordination delivery authority repaired", "confirmed": False},
    )

    assert response.status_code == 422
