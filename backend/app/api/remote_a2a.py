"""Authenticated remote endpoint management; discovery is not runtime activation."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import remote_a2a_connections as service

router = APIRouter(prefix="/{agent_id}/remote-a2a", tags=["remote-a2a"])


class RemoteConnectionFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_user_id: uuid.UUID
    expected_tenant_id: uuid.UUID
    expected_version: int | None = Field(default=None, ge=1)


class RemoteConnectionIn(RemoteConnectionFrame):
    request_id: uuid.UUID
    card_url: str = Field(min_length=1, max_length=2048)
    rpc_url: str = Field(min_length=1, max_length=2048)
    bearer_token: SecretStr | None = None
    allowed_networks: list[str] = Field(default_factory=list)
    max_response_bytes: int = Field(default=16 * 1024 * 1024, gt=0, le=2_147_483_647)
    timeout_seconds: int = Field(default=20, gt=0, le=2_147_483_647)


@router.get("")
async def get_connection(
    agent_id: uuid.UUID, response: Response, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.get_connection(db, agent_id=agent_id, actor=actor)


@router.put("")
async def configure_connection(
    agent_id: uuid.UUID,
    body: RemoteConnectionIn,
    response: Response,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.configure_connection(
        db,
        agent_id=agent_id,
        actor=actor,
        **body.model_dump(exclude={"bearer_token"}),
        bearer_token=body.bearer_token.get_secret_value() if body.bearer_token is not None else None,
    )


@router.post("/discover")
async def discover_connection(
    agent_id: uuid.UUID,
    body: RemoteConnectionFrame,
    response: Response,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.discover_connection(db, agent_id=agent_id, actor=actor, **body.model_dump())


@router.post("/disable")
async def disable_connection(
    agent_id: uuid.UUID,
    body: RemoteConnectionFrame,
    response: Response,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.disable_connection(db, agent_id=agent_id, actor=actor, **body.model_dump())


class RemoteTaskIn(RemoteConnectionFrame):
    request_id: uuid.UUID
    source_session_id: uuid.UUID
    params: dict
    uncontrolled_effects_acknowledged: StrictBool = False
    unknown_cost_acknowledged: StrictBool = False
    required_controls: list[str] = Field(default_factory=list)


class RemoteRuntimeSelectionIn(RemoteConnectionFrame):
    request_id: uuid.UUID
    expected_runtime_revision: int | None = Field(default=None, ge=1)
    selected_runtime: Literal["remote_a2a", "previous"]
    uncontrolled_effects_acknowledged: StrictBool = False
    unknown_cost_acknowledged: StrictBool = False
    required_controls: list[str] = Field(default_factory=list)


@router.post("/runtime")
async def select_runtime(
    agent_id: uuid.UUID, body: RemoteRuntimeSelectionIn, response: Response,
    actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    from app.services.remote_a2a_runtime import select_runtime as select_employee_runtime

    response.headers["Cache-Control"] = "private, no-store"
    return await select_employee_runtime(db, agent_id=agent_id, actor=actor, **body.model_dump())


class RemoteTaskOperationIn(RemoteConnectionFrame):
    operation_id: uuid.UUID
    action: Literal["refresh", "cancel", "send_message", "abandon", "retry"]
    params: dict = Field(default_factory=dict)


class RemoteArtifactRecoveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_user_id: uuid.UUID
    expected_tenant_id: uuid.UUID


@router.post("/tasks", status_code=201)
async def start_task(
    agent_id: uuid.UUID,
    body: RemoteTaskIn,
    response: Response,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import remote_a2a_tasks

    response.headers["Cache-Control"] = "private, no-store"
    return await remote_a2a_tasks.start_task(db, agent_id=agent_id, actor=actor, **body.model_dump())


@router.get("/tasks/{task_id}")
async def get_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    response: Response,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import remote_a2a_tasks

    response.headers["Cache-Control"] = "private, no-store"
    task = await remote_a2a_tasks.get_task(db, agent_id=agent_id, task_id=task_id, actor=actor)
    return await remote_a2a_tasks.read_task_view(db, task)


@router.get("/tasks")
async def list_tasks(
    agent_id: uuid.UUID,
    source_session_id: uuid.UUID,
    response: Response,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import remote_a2a_tasks

    response.headers["Cache-Control"] = "private, no-store"
    return await remote_a2a_tasks.list_tasks(db, agent_id=agent_id, source_session_id=source_session_id, actor=actor)


@router.post("/tasks/{task_id}/operations")
async def operate_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    body: RemoteTaskOperationIn,
    response: Response,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import remote_a2a_tasks

    response.headers["Cache-Control"] = "private, no-store"
    return await remote_a2a_tasks.operate_task(db, agent_id=agent_id, task_id=task_id, actor=actor, **body.model_dump())


@router.post("/tasks/{task_id}/artifacts")
async def recover_artifacts(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    body: RemoteArtifactRecoveryIn,
    response: Response,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.remote_a2a_artifacts import recover_task_artifacts
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.workspace_resource_authority import WorkspaceAuthorityError

    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await recover_task_artifacts(db, agent_id=agent_id, task_id=task_id, actor=actor, **body.model_dump())
    except (ContentInputReceiptError, WorkspaceAuthorityError, OSError) as exc:
        await db.rollback()
        raise HTTPException(getattr(exc, "status_code", 503), detail={
            "code": getattr(exc, "code", "remote_a2a_storage_unavailable"),
            "recovery_action": "retry_saved_artifact_delivery", "remote_request_replayed": False,
        }) from exc
