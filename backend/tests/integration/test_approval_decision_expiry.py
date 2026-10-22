"""The real approval entry honors its persisted deadline before any decision."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.user import User
from app.services.approval_service import approval_service
from tests.services.test_channel_ingress_inbox import _seed_agent

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('mode', ['live', 'expired', 'unauthorized_expired'])
async def test_web_decision_checks_the_stored_expiry_after_current_authority(
    owner_sessionmaker, app_user_sessionmaker, mode,
):
    from app.api.enterprise import router

    tenant_id, owner_id, agent_id = await _seed_agent(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        result = await approval_service.request_approval(db, await db.get(Agent, agent_id), action_type='publish',
            details={'requested_by': str(owner_id), 'purpose': 'Deadline-bound employee approval'})
        approval_id = UUID(result['approval_id'])
        await db.commit()
    actor_id = owner_id
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        approval.expires_at = datetime.now(UTC) + timedelta(minutes=30) if mode == 'live' else datetime.now(UTC) - timedelta(seconds=1)
        assert approval.created_at > datetime.now(UTC) - timedelta(minutes=1)
        if mode == 'unauthorized_expired':
            actor_id = uuid4()
            db.add(User(id=actor_id, tenant_id=tenant_id, username=uuid4().hex,
                email=f'{uuid4().hex}@synthetic.invalid', password_hash='synthetic', display_name='Other member', role='member'))
        await db.commit()

    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            yield db
            await db.commit()

    async def actor():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            return await db.get(User, actor_id, populate_existing=True)

    web = FastAPI()
    web.include_router(router, prefix='/api')
    web.dependency_overrides[get_db] = database
    web.dependency_overrides[get_current_user] = actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url='http://approval-expiry') as api:
        response = await api.post(f'/api/enterprise/approvals/{approval_id}/resolve', json={'action': 'approve'})
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        audits = list(await db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id,
            AuditLog.action == 'approval_approved')))
        if mode == 'live':
            assert response.status_code == 200 and response.json()['status'] == 'approved', response.text
            assert approval.status == 'approved' and approval.resolved_by == owner_id and len(audits) == 1
        elif mode == 'expired':
            assert response.status_code == 200 and response.json()['status'] == 'rejected', response.text
            assert approval.status == 'rejected' and approval.execution_status == 'rejected'
            assert approval.resolved_by is None and approval.execution_task_id is None and not audits
        else:
            assert response.status_code == 400, response.text
            assert approval.status == 'pending' and approval.resolved_by is None and not audits
