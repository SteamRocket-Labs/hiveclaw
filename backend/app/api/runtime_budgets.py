"""Runtime budget policy and run visibility API."""

from __future__ import annotations

from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import get_current_admin
from app.models.user import User
from app.services.runtime_budget_service import RuntimeBudgetService

router = APIRouter(prefix="/runtime-budgets", tags=["runtime-budgets"])


class RuntimeBudgetPolicyOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    name: str
    enabled: bool
    priority: int = 0
    scope_type: str
    source: str | None = None
    profile: str | None = None
    agent_id: uuid.UUID | None = None
    trigger_id: uuid.UUID | None = None
    enforcement_mode: str
    fail_mode: str = "fail_closed"
    max_tokens: int | None = None
    max_cache_miss_tokens: int | None = None
    max_subagents: int | None = None
    max_delegations: int | None = None
    max_background_tasks: int | None = None
    max_continuation_wakes: int | None = None
    max_provider_calls: int | None = None
    default_child_token_reservation: int = 50_000
    default_llm_call_token_reservation: int = 50_000
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RuntimeBudgetPolicyWrite(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    priority: int = 0
    scope_type: str = "tenant_default"
    source: str | None = Field(default=None, max_length=80)
    profile: str | None = Field(default=None, max_length=80)
    agent_id: uuid.UUID | None = None
    trigger_id: uuid.UUID | None = None
    enforcement_mode: str = "enforce"
    fail_mode: str = "fail_closed"
    max_tokens: int | None = Field(default=None, ge=0)
    max_cache_miss_tokens: int | None = Field(default=None, ge=0)
    max_subagents: int | None = Field(default=None, ge=0)
    max_delegations: int | None = Field(default=None, ge=0)
    max_background_tasks: int | None = Field(default=None, ge=0)
    max_continuation_wakes: int | None = Field(default=None, ge=0)
    max_provider_calls: int | None = Field(default=None, ge=0)
    default_child_token_reservation: int = Field(default=50_000, ge=0)
    default_llm_call_token_reservation: int = Field(default=50_000, ge=0)
    policy_json: dict | None = None


class RuntimeBudgetPolicyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    priority: int | None = None
    scope_type: str | None = None
    source: str | None = Field(default=None, max_length=80)
    profile: str | None = Field(default=None, max_length=80)
    agent_id: uuid.UUID | None = None
    trigger_id: uuid.UUID | None = None
    enforcement_mode: str | None = None
    fail_mode: str | None = None
    max_tokens: int | None = Field(default=None, ge=0)
    max_cache_miss_tokens: int | None = Field(default=None, ge=0)
    max_subagents: int | None = Field(default=None, ge=0)
    max_delegations: int | None = Field(default=None, ge=0)
    max_background_tasks: int | None = Field(default=None, ge=0)
    max_continuation_wakes: int | None = Field(default=None, ge=0)
    max_provider_calls: int | None = Field(default=None, ge=0)
    default_child_token_reservation: int | None = Field(default=None, ge=0)
    default_llm_call_token_reservation: int | None = Field(default=None, ge=0)
    policy_json: dict | None = None


class RuntimeBudgetRunOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    root_run_kind: str
    root_run_key: str
    source: str | None = None
    profile: str | None = None
    status: str
    enforcement_mode: str
    terminal_reason: str | None = None
    user_status: str
    user_reason: str
    user_next_action: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class RuntimeBudgetEventOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    event_type: str
    reservation_key: str | None = None
    allowed: bool | None = None
    would_deny: bool
    reason: str | None = None
    user_message: str
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class RuntimeBudgetCancelRequest(BaseModel):
    reason: str = Field(default="admin cancelled runtime budget run", min_length=1, max_length=1000)


class RuntimeBudgetCancelOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    status: str
    terminal_reason: str | None = None
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class RuntimeBudgetApproveOverrunRequest(BaseModel):
    reason: str = Field(default="admin approved runtime overrun", min_length=1, max_length=1000)
    enforcement_mode: str = "observe"
    max_tokens: int | None = Field(default=None, ge=0)
    max_cache_miss_tokens: int | None = Field(default=None, ge=0)
    max_subagents: int | None = Field(default=None, ge=0)
    max_delegations: int | None = Field(default=None, ge=0)
    max_background_tasks: int | None = Field(default=None, ge=0)
    max_continuation_wakes: int | None = Field(default=None, ge=0)
    max_provider_calls: int | None = Field(default=None, ge=0)


class RuntimeBudgetTenantModeRequest(BaseModel):
    enforcement_mode: str = Field(default="observe")
    reason: str = Field(default="tenant runtime budget emergency switch", min_length=1, max_length=1000)


class RuntimeBudgetTenantModeOut(BaseModel):
    tenant_id: uuid.UUID
    enforcement_mode: str
    updated_policies: int


async def get_runtime_budget_service() -> RuntimeBudgetService:
    return RuntimeBudgetService()


def _require_tenant(user: User) -> uuid.UUID:
    if not user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant assigned")
    return user.tenant_id


def _user_status(status_value: str) -> str:
    return {
        "active": "正在运行",
        "completed": "已完成",
        "exhausted": "已暂停",
        "hard_stopped": "已停止",
        "expired": "已停止",
        "cancelled": "已停止",
    }.get(status_value, "需要处理")


def _user_reason(status_value: str, terminal_reason: str | None) -> str:
    reason = terminal_reason or ""
    if "budget" in reason or status_value == "exhausted":
        return "运行额度已达上限"
    if "failure" in reason or "reconciliation" in reason:
        return "连续失败或异常任务过多"
    if status_value == "cancelled":
        return "管理员已暂停"
    if status_value == "expired":
        return "本次运行已超时"
    if status_value == "completed":
        return "运行已完成"
    return "系统保护机制已介入"


def _user_next_action(status_value: str) -> str:
    if status_value in {"exhausted", "hard_stopped"}:
        return "查看已完成结果，或联系管理员批准继续"
    if status_value == "active":
        return "等待当前运行完成"
    if status_value == "completed":
        return "查看运行结果"
    return "联系管理员处理"


def _run_out(run) -> RuntimeBudgetRunOut:
    status_value = str(run.status)
    return RuntimeBudgetRunOut.model_validate(
        {
            **run.__dict__,
            "user_status": _user_status(status_value),
            "user_reason": _user_reason(status_value, getattr(run, "terminal_reason", None)),
            "user_next_action": _user_next_action(status_value),
        }
    )


def _event_message(event) -> str:
    event_type = str(event.event_type)
    if event_type == "denial":
        return "这次运行未继续执行，因为运行额度已达上限。"
    if event_type == "reservation" and getattr(event, "would_deny", False):
        return "系统检测到这次运行接近保护阈值。"
    if event_type == "expired":
        return "本次运行已超时，未开始的后续任务已停止。"
    if event_type == "cancelled":
        return "管理员已暂停本次运行。"
    if event_type == "settlement":
        return "系统已记录本次运行消耗。"
    return "运行状态已更新。"


def _event_out(event) -> RuntimeBudgetEventOut:
    return RuntimeBudgetEventOut.model_validate({**event.__dict__, "user_message": _event_message(event)})


@router.get("/policies", response_model=list[RuntimeBudgetPolicyOut])
async def list_runtime_budget_policies(
    current_user: User = Depends(get_current_admin),
    service: RuntimeBudgetService = Depends(get_runtime_budget_service),
):
    tenant_id = _require_tenant(current_user)
    policies = await service.list_policies(tenant_id=tenant_id)
    return [RuntimeBudgetPolicyOut.model_validate(policy) for policy in policies]


@router.post("/policies", response_model=RuntimeBudgetPolicyOut, status_code=status.HTTP_201_CREATED)
async def create_runtime_budget_policy(
    body: RuntimeBudgetPolicyWrite,
    current_user: User = Depends(get_current_admin),
    service: RuntimeBudgetService = Depends(get_runtime_budget_service),
):
    tenant_id = _require_tenant(current_user)
    policy = await service.create_policy(tenant_id=tenant_id, **body.model_dump())
    return RuntimeBudgetPolicyOut.model_validate(policy)


@router.patch("/policies/{policy_id}", response_model=RuntimeBudgetPolicyOut)
async def update_runtime_budget_policy(
    policy_id: uuid.UUID,
    body: RuntimeBudgetPolicyPatch,
    current_user: User = Depends(get_current_admin),
    service: RuntimeBudgetService = Depends(get_runtime_budget_service),
):
    tenant_id = _require_tenant(current_user)
    policy = await service.update_policy(
        tenant_id=tenant_id,
        policy_id=policy_id,
        updates=body.model_dump(exclude_unset=True),
    )
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="runtime budget policy not found")
    return RuntimeBudgetPolicyOut.model_validate(policy)


@router.get("/runs", response_model=list[RuntimeBudgetRunOut])
async def list_runtime_budget_runs(
    status_filter: str | None = Query(default=None, alias="status"),
    agent_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_admin),
    service: RuntimeBudgetService = Depends(get_runtime_budget_service),
):
    tenant_id = _require_tenant(current_user)
    runs = await service.list_runs(tenant_id=tenant_id, status=status_filter, agent_id=agent_id, limit=limit)
    return [_run_out(run) for run in runs]


@router.get("/runs/{budget_run_id}", response_model=RuntimeBudgetRunOut)
async def get_runtime_budget_run(
    budget_run_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    service: RuntimeBudgetService = Depends(get_runtime_budget_service),
):
    tenant_id = _require_tenant(current_user)
    run = await service.get_run(tenant_id=tenant_id, budget_run_id=budget_run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="runtime budget run not found")
    return _run_out(run)


@router.get("/runs/{budget_run_id}/events", response_model=list[RuntimeBudgetEventOut])
async def list_runtime_budget_events(
    budget_run_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_admin),
    service: RuntimeBudgetService = Depends(get_runtime_budget_service),
):
    tenant_id = _require_tenant(current_user)
    events = await service.list_events(tenant_id=tenant_id, budget_run_id=budget_run_id, limit=limit)
    return [_event_out(event) for event in events]


@router.post("/runs/{budget_run_id}/cancel", response_model=RuntimeBudgetCancelOut)
async def cancel_runtime_budget_run(
    budget_run_id: uuid.UUID,
    body: RuntimeBudgetCancelRequest,
    current_user: User = Depends(get_current_admin),
    service: RuntimeBudgetService = Depends(get_runtime_budget_service),
):
    tenant_id = _require_tenant(current_user)
    run = await service.cancel_run(
        tenant_id=tenant_id,
        budget_run_id=budget_run_id,
        reason=body.reason,
        actor_user_id=current_user.id,
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="runtime budget run not found")
    return RuntimeBudgetCancelOut.model_validate(run)


@router.post("/runs/{budget_run_id}/approve-overrun", response_model=RuntimeBudgetRunOut)
async def approve_runtime_budget_overrun(
    budget_run_id: uuid.UUID,
    body: RuntimeBudgetApproveOverrunRequest,
    current_user: User = Depends(get_current_admin),
    service: RuntimeBudgetService = Depends(get_runtime_budget_service),
):
    tenant_id = _require_tenant(current_user)
    run = await service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=budget_run_id,
        reason=body.reason,
        actor_user_id=current_user.id,
        enforcement_mode=body.enforcement_mode,
        max_tokens=body.max_tokens,
        max_cache_miss_tokens=body.max_cache_miss_tokens,
        max_subagents=body.max_subagents,
        max_delegations=body.max_delegations,
        max_background_tasks=body.max_background_tasks,
        max_continuation_wakes=body.max_continuation_wakes,
        max_provider_calls=body.max_provider_calls,
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="runtime budget run not found")
    return _run_out(run)


@router.post("/tenant/enforcement-mode", response_model=RuntimeBudgetTenantModeOut)
async def set_tenant_runtime_budget_enforcement_mode(
    body: RuntimeBudgetTenantModeRequest,
    current_user: User = Depends(get_current_admin),
    service: RuntimeBudgetService = Depends(get_runtime_budget_service),
):
    tenant_id = _require_tenant(current_user)
    updated = await service.set_tenant_enforcement_mode(
        tenant_id=tenant_id,
        enforcement_mode=body.enforcement_mode,
        reason=body.reason,
        actor_user_id=current_user.id,
    )
    return RuntimeBudgetTenantModeOut(
        tenant_id=tenant_id,
        enforcement_mode=body.enforcement_mode,
        updated_policies=updated,
    )
