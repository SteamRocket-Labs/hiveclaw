from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security import get_current_admin


class _FakeRuntimeBudgetService:
    def __init__(self):
        self.tenant_id = uuid4()
        self.run_id = uuid4()
        self.policy_id = uuid4()
        self.cancelled: dict | None = None
        self.created_policy: dict | None = None
        self.updated_policy: dict | None = None
        self.approved_overrun: dict | None = None
        self.rejected_overrun: dict | None = None
        self.tenant_mode: dict | None = None

    async def list_policies(self, *, tenant_id):
        return [
            SimpleNamespace(
                id=self.policy_id,
                tenant_id=tenant_id,
                name="Scheduled default",
                enabled=True,
                priority=0,
                scope_type="tenant_default",
                source="scheduled",
                profile="scheduled",
                agent_id=None,
                trigger_id=None,
                enforcement_mode="enforce",
                fail_mode="fail_closed",
                max_tokens=1_000_000,
                max_cache_miss_tokens=250_000,
                max_subagents=32,
                max_team_sessions=0,
                max_delegations=32,
                max_background_tasks=32,
                max_continuation_wakes=64,
                max_provider_calls=128,
                default_child_token_reservation=50_000,
                default_llm_call_token_reservation=50_000,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        ]

    async def create_policy(self, *, tenant_id, **payload):
        self.created_policy = {"tenant_id": tenant_id, **payload}
        return SimpleNamespace(
            id=self.policy_id,
            tenant_id=tenant_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            **payload,
        )

    async def update_policy(self, *, tenant_id, policy_id, updates):
        self.updated_policy = {"tenant_id": tenant_id, "policy_id": policy_id, "updates": updates}
        base = (await self.list_policies(tenant_id=tenant_id))[0]
        for key, value in updates.items():
            setattr(base, key, value)
        return base

    async def list_runs(self, *, tenant_id, status=None, agent_id=None, limit=50):
        return [
            SimpleNamespace(
                id=self.run_id,
                tenant_id=tenant_id,
                root_run_kind="trigger_fire",
                root_run_key="trigger:daily_scan",
                source="scheduled",
                profile="scheduled",
                status=status or "hard_stopped",
                enforcement_mode="enforce",
                terminal_reason="runtime_budget_exhausted:subagents",
                created_at=datetime.now(UTC),
                expires_at=None,
                completed_at=None,
            )
        ]

    async def get_run(self, *, tenant_id, budget_run_id):
        return (await self.list_runs(tenant_id=tenant_id))[0]

    async def list_events(self, *, tenant_id, budget_run_id, limit=100):
        return [
            SimpleNamespace(
                id=uuid4(),
                tenant_id=tenant_id,
                budget_run_id=budget_run_id,
                event_type="denial",
                reservation_key="child-2",
                allowed=False,
                would_deny=True,
                reason="runtime budget exhausted",
                amounts_json={"subagents": 1},
                metadata_json={},
                created_at=datetime.now(UTC),
            )
        ]

    async def cancel_run(self, *, tenant_id, budget_run_id, reason, actor_user_id):
        self.cancelled = {
            "tenant_id": tenant_id,
            "budget_run_id": budget_run_id,
            "reason": reason,
            "actor_user_id": actor_user_id,
        }
        return SimpleNamespace(
            id=budget_run_id,
            tenant_id=tenant_id,
            status="cancelled",
            terminal_reason=reason,
            completed_at=datetime.now(UTC),
        )

    async def approve_overrun(
        self,
        *,
        tenant_id,
        budget_run_id,
        reason,
        actor_user_id,
        enforcement_mode,
        max_tokens=None,
        max_cache_miss_tokens=None,
        max_subagents=None,
        max_team_sessions=None,
        max_delegations=None,
        max_background_tasks=None,
        max_continuation_wakes=None,
        max_provider_calls=None,
    ):
        self.approved_overrun = {
            "tenant_id": tenant_id,
            "budget_run_id": budget_run_id,
            "reason": reason,
            "actor_user_id": actor_user_id,
            "enforcement_mode": enforcement_mode,
            "max_subagents": max_subagents,
            "max_team_sessions": max_team_sessions,
        }
        return SimpleNamespace(
            id=budget_run_id,
            tenant_id=tenant_id,
            root_run_kind="trigger_fire",
            root_run_key="trigger:daily_scan",
            source="scheduled",
            profile="scheduled",
            status="active",
            enforcement_mode=enforcement_mode,
            terminal_reason=None,
            created_at=datetime.now(UTC),
            expires_at=None,
            completed_at=None,
        )

    async def reject_overrun(self, *, tenant_id, budget_run_id, reason, actor_user_id):
        self.rejected_overrun = {
            "tenant_id": tenant_id,
            "budget_run_id": budget_run_id,
            "reason": reason,
            "actor_user_id": actor_user_id,
        }
        return SimpleNamespace(
            id=budget_run_id,
            tenant_id=tenant_id,
            root_run_kind="trigger_fire",
            root_run_key="trigger:daily_scan",
            source="scheduled",
            profile="scheduled",
            status="stopped",
            enforcement_mode="enforce",
            terminal_reason="runtime_budget_approval_rejected",
            created_at=datetime.now(UTC),
            expires_at=None,
            completed_at=datetime.now(UTC),
        )

    async def set_tenant_enforcement_mode(self, *, tenant_id, enforcement_mode, reason, actor_user_id):
        self.tenant_mode = {
            "tenant_id": tenant_id,
            "enforcement_mode": enforcement_mode,
            "reason": reason,
            "actor_user_id": actor_user_id,
        }
        return 3


def _client(fake_service: _FakeRuntimeBudgetService):
    import app.api.runtime_budgets as runtime_budgets_api

    app = FastAPI()
    app.include_router(runtime_budgets_api.router)
    user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=fake_service.tenant_id, username="admin")

    async def override_admin():
        return user

    async def override_service():
        return fake_service

    app.dependency_overrides[get_current_admin] = override_admin
    app.dependency_overrides[runtime_budgets_api.get_runtime_budget_service] = override_service
    return TestClient(app), user


def test_runtime_budget_api_lists_policies_and_user_facing_runs():
    fake_service = _FakeRuntimeBudgetService()
    client, _user = _client(fake_service)

    policies = client.get("/runtime-budgets/policies")
    runs = client.get("/runtime-budgets/runs")

    assert policies.status_code == 200
    assert policies.json()[0]["name"] == "Scheduled default"
    assert runs.status_code == 200
    assert runs.json()[0]["user_status"] == "已停止"
    assert runs.json()[0]["user_reason"] == "运行额度已达上限"
    assert "budget_run_id" not in runs.json()[0]


def test_runtime_budget_api_cancel_scopes_to_current_tenant():
    fake_service = _FakeRuntimeBudgetService()
    client, user = _client(fake_service)

    response = client.post(
        f"/runtime-budgets/runs/{fake_service.run_id}/cancel",
        json={"reason": "operator stop"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert fake_service.cancelled == {
        "tenant_id": fake_service.tenant_id,
        "budget_run_id": fake_service.run_id,
        "reason": "operator stop",
        "actor_user_id": user.id,
    }


def test_runtime_budget_api_creates_and_updates_policy_in_current_tenant():
    fake_service = _FakeRuntimeBudgetService()
    client, _user = _client(fake_service)

    created = client.post(
        "/runtime-budgets/policies",
        json={
            "name": "Scheduled hard guard",
            "scope_type": "source_profile",
            "source": "scheduled",
            "profile": "scheduled",
            "max_subagents": 24,
            "enforcement_mode": "observe",
        },
    )
    updated = client.patch(
        f"/runtime-budgets/policies/{fake_service.policy_id}",
        json={"enforcement_mode": "enforce", "max_subagents": 16},
    )

    assert created.status_code == 201
    assert created.json()["tenant_id"] == str(fake_service.tenant_id)
    assert fake_service.created_policy is not None
    assert fake_service.created_policy["tenant_id"] == fake_service.tenant_id
    assert fake_service.created_policy["max_subagents"] == 24
    assert updated.status_code == 200
    assert updated.json()["enforcement_mode"] == "enforce"
    assert fake_service.updated_policy == {
        "tenant_id": fake_service.tenant_id,
        "policy_id": fake_service.policy_id,
        "updates": {"enforcement_mode": "enforce", "max_subagents": 16},
    }


def test_runtime_budget_api_approves_overrun_and_switches_tenant_mode():
    fake_service = _FakeRuntimeBudgetService()
    client, user = _client(fake_service)

    approved = client.post(
        f"/runtime-budgets/runs/{fake_service.run_id}/approve-overrun",
        json={"reason": "human reviewed", "enforcement_mode": "observe", "max_subagents": 48, "max_team_sessions": 4},
    )
    switched = client.post(
        "/runtime-budgets/tenant/enforcement-mode",
        json={"enforcement_mode": "observe", "reason": "emergency observe"},
    )

    assert approved.status_code == 200
    assert approved.json()["status"] == "active"
    assert fake_service.approved_overrun == {
        "tenant_id": fake_service.tenant_id,
        "budget_run_id": fake_service.run_id,
        "reason": "human reviewed",
        "actor_user_id": user.id,
        "enforcement_mode": "observe",
        "max_subagents": 48,
        "max_team_sessions": 4,
    }
    assert switched.status_code == 200
    assert switched.json()["updated_policies"] == 3
    assert fake_service.tenant_mode == {
        "tenant_id": fake_service.tenant_id,
        "enforcement_mode": "observe",
        "reason": "emergency observe",
        "actor_user_id": user.id,
    }


def test_runtime_budget_api_rejects_waiting_work_with_actor_and_reason():
    fake_service = _FakeRuntimeBudgetService()
    client, user = _client(fake_service)

    response = client.post(
        f"/runtime-budgets/runs/{fake_service.run_id}/reject-overrun",
        json={"reason": "unsafe to continue"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert response.json()["user_status"] == "已停止"
    assert fake_service.rejected_overrun == {
        "tenant_id": fake_service.tenant_id,
        "budget_run_id": fake_service.run_id,
        "reason": "unsafe to continue",
        "actor_user_id": user.id,
    }


def test_runtime_budget_waiting_status_has_user_semantics():
    from app.api.runtime_budgets import _user_next_action, _user_reason, _user_status

    assert _user_status("waiting_budget_approval") == "等待批准"
    assert (
        _user_reason("waiting_budget_approval", "runtime_budget_approval_required:subagents")
        == "运行额度已达上限，正在等待管理员批准"
    )
    assert _user_next_action("waiting_budget_approval") == "你可以继续其他工作；管理员批准后本任务会自动恢复"
