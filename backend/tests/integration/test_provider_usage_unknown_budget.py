"""Unknown provider billing cannot release quota or be reaped as a free call."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.database import pin_rls_tenant_context
from app.models.runtime_task import RuntimeTask
from app.services.llm_client import LLMMessage, LLMResponse
from app.services.runtime_budget_llm import RuntimeBudgetedLLMClient
from app.services.runtime_budget_service import (
    RuntimeBudgetDenied, RuntimeBudgetReservation, RuntimeBudgetRunCreate,
    RuntimeBudgetService, RuntimeBudgetSettlement,
)
from tests.integration import conftest as integration
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run
from tests.services.test_runtime_budget_llm import _FakeLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(scope="module")
def pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture(scope="module")
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


@pytest.mark.parametrize("outcome", ["exception", "missing_usage"])
async def test_provider_usage_hold_survives_terminal_task_reaping_and_accepts_exact_settlement(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, outcome,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await service.create_run(RuntimeBudgetRunCreate(tenant_id=seed["tenant_id"], root_run_kind="web_chat_turn",
        root_run_key=str(seed["run_id"]), root_runtime_task_id=seed["run_id"], max_tokens=500,
        max_cache_miss_tokens=500, max_provider_calls=2, fail_mode="fail_closed"))

    class Provider(_FakeLLMClient):
        async def complete(self, *args, **kwargs):
            self.complete_calls += 1
            if outcome == "exception":
                raise TimeoutError("Synthetic provider acknowledgement lost")
            return LLMResponse(content="Preserve this returned answer", usage={})

    provider = Provider()
    client = RuntimeBudgetedLLMClient(provider, budget_run_id=run.id, runtime_task_id=seed["run_id"],
        default_llm_call_token_reservation=500, service=service)
    if outcome == "exception":
        with pytest.raises(TimeoutError, match="Synthetic provider acknowledgement lost"):
            await client.complete([LLMMessage(role="user", content="Produce the report")])
    else:
        assert (await client.complete([LLMMessage(role="user", content="Produce the report")])).content == "Preserve this returned answer"
    current = await service.get_run(tenant_id=seed["tenant_id"], budget_run_id=run.id)
    assert current.reserved_tokens == 500 and current.reserved_provider_calls == 1
    assert current.used_tokens == 0 and current.used_provider_calls == 0
    events = await service.list_events(tenant_id=seed["tenant_id"], budget_run_id=run.id)
    reservations = [event for event in events if event.event_type == "reservation"]
    assert len(reservations) == 1 and reservations[0].metadata_json["provider_usage_required"] is True
    assert not any(event.event_type == "settlement" for event in events)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        task = await db.get(RuntimeTask, seed["run_id"])
        task.status, task.completed_at = "failed", datetime.now(UTC) - timedelta(hours=2)
        await db.commit()
    legacy = await service.create_run(RuntimeBudgetRunCreate(tenant_id=seed["tenant_id"], root_run_kind="test",
        root_run_key=str(uuid4()), max_tokens=500, max_provider_calls=2))
    await service.reserve(RuntimeBudgetReservation(budget_run_id=legacy.id, reservation_key="legacy-provider-call",
        runtime_task_id=seed["run_id"], tokens=125, provider_calls=1, reason="provider_call_start",
        metadata={"method": "stream", "model": provider.model}))
    other = await service.create_run(RuntimeBudgetRunCreate(tenant_id=seed["tenant_id"], root_run_kind="test",
        root_run_key=str(uuid4()), max_tokens=100))
    await service.reserve(RuntimeBudgetReservation(budget_run_id=other.id, reservation_key="unused-task-booking",
        runtime_task_id=seed["run_id"], tokens=25))
    assert await service.reconcile_orphaned_reservations(limit=1, nested_reservation_grace=timedelta(0)) == 1
    assert (await service.get_run(tenant_id=seed["tenant_id"], budget_run_id=other.id)).reserved_tokens == 0
    assert (await service.get_run(tenant_id=seed["tenant_id"], budget_run_id=legacy.id)).reserved_tokens == 125
    current = await service.get_run(tenant_id=seed["tenant_id"], budget_run_id=run.id)
    assert current.reserved_tokens == 500 and current.reserved_provider_calls == 1
    with pytest.raises(RuntimeBudgetDenied):
        await client.complete([LLMMessage(role="user", content="A later call cannot spend the held quota")])
    assert provider.complete_calls == 1

    await service.settle(RuntimeBudgetSettlement(budget_run_id=run.id, reservation_key=reservations[0].reservation_key,
        runtime_task_id=seed["run_id"], actual_tokens=35, actual_cache_miss_tokens=30, actual_provider_calls=1,
        reason="provider_usage_reconciled", metadata={"usage": {"input_tokens": 30, "output_tokens": 5}}))
    current = await service.get_run(tenant_id=seed["tenant_id"], budget_run_id=run.id)
    assert current.reserved_tokens == 0 and current.reserved_provider_calls == 0
    assert current.used_tokens == 35 and current.used_provider_calls == 1
    await service.settle(RuntimeBudgetSettlement(budget_run_id=legacy.id, reservation_key="legacy-provider-call",
        runtime_task_id=seed["run_id"], actual_tokens=12, actual_cache_miss_tokens=10, actual_provider_calls=1,
        reason="provider_usage_reconciled", metadata={"usage": {"input_tokens": 10, "output_tokens": 2}}))
    current = await service.get_run(tenant_id=seed["tenant_id"], budget_run_id=legacy.id)
    assert current.reserved_tokens == 0 and current.used_tokens == 12
