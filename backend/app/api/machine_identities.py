"""Explicit machine credential management; this is not a public task protocol."""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.security.utils import get_authorization_scheme_param
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import machine_identity_service as service

router = APIRouter()


class MachineRequestFrame(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_user_id: uuid.UUID
    expected_tenant_id: uuid.UUID


class MachineIdentityIssueIn(MachineRequestFrame):
    name: str = Field(min_length=1, max_length=200)
    expires_at: AwareDatetime
    scopes: list[Literal['agent.describe', 'agent.execute', 'task.read', 'task.cancel']] = Field(
        default_factory=lambda: ['agent.describe'], min_length=1)
    issue_request_id: uuid.UUID

    @field_validator('name')
    @classmethod
    def identity_name(cls, value):
        if not value.strip():
            raise ValueError('Machine identity name is required')
        return value.strip()


@router.get('/{agent_id}/machine-identities')
async def list_machine_identities(agent_id: uuid.UUID, current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)):
    return await service.list_machine_identities(db, agent_id=agent_id, actor=current_user)


@router.post('/{agent_id}/machine-identities', status_code=201)
async def issue_machine_identity(agent_id: uuid.UUID, body: MachineIdentityIssueIn, response: Response,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await service.issue_machine_identity(db, agent_id=agent_id, actor=current_user, **body.model_dump())
    response.headers['Cache-Control'] = 'no-store'
    if result['idempotent_replay']:
        response.status_code = 200
    return result


@router.post('/{agent_id}/machine-identities/{principal_id}/revoke')
async def revoke_machine_identity(agent_id: uuid.UUID, principal_id: uuid.UUID, body: MachineRequestFrame,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.revoke_machine_identity(db, agent_id=agent_id, principal_id=principal_id,
        actor=current_user, **body.model_dump())


@router.get('/{agent_id}/machine-description')
async def get_machine_description(agent_id: uuid.UUID, request: Request, response: Response,
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)):
    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != 'bearer' or not token:
        raise HTTPException(401, detail='Machine credential required', headers={'WWW-Authenticate': 'Bearer'})
    context, agent = await service.authenticate_machine(db, token=token, agent_id=agent_id, scope='agent.describe')
    from app.services.a2a_machine_tasks import MACHINE_TASK_METHOD_SCOPES

    payload = {'schema': 'hive.machine_agent_description.v1',
        'agent': {'id': str(agent.id), 'name': agent.name, 'description': agent.role_description or agent.bio or ''},
        'principal_id': str(context.principal_id), 'scopes': list(context.scopes),
        'interoperability': {'a2a_tasks': {'status': 'available', 'protocol_version': '1.0', 'transport': 'JSONRPC',
            'rpc_url': str(request.url_for('machine_task_rpc', agent_id=agent.id)),
            'methods': {method: scope for method, scope in MACHINE_TASK_METHOD_SCOPES.items() if scope in context.scopes},
            'inputs': {'text': True, 'files': False}, 'outputs': {'text': True, 'captured_files': True},
            'history': 'current_run', 'streaming': False, 'push_notifications': False}}}
    await db.commit()
    response.headers['Cache-Control'] = 'no-store'
    return payload
