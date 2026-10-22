"""Current ownership checks preserve authenticated platform-admin company views."""

from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx
import pytest

from app.api import agents
from app.core.security import create_access_token
from app.database import get_db, tenant_scoped_session
from app.models.machine_credential import MachineCredential
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit_logger
from tests.integration import test_m3h_machine_identity_postgres as machine

machine_api = machine.machine_api
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('home', ['global', 'foreign'])
async def test_locked_ownership_keeps_selected_admin_view_and_canonical_home(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, home,
):
    seed, _api = machine_api
    issuer_id = uuid4()
    home_id = uuid4() if home == 'foreign' else None
    async with owner_sessionmaker() as db:
        if home_id is not None:
            db.add(Tenant(id=home_id, name='Administrator home', slug=f'admin-{home_id.hex}'))
        db.add(User(id=issuer_id, tenant_id=home_id, username=f'global-{issuer_id.hex}',
            email=f'{issuer_id.hex}@selected.example.test', password_hash='synthetic-unused-password',
            display_name='Selected company administrator', role='platform_admin', is_active=True))
        await db.commit()
    web = FastAPI()
    web.include_router(agents.router)

    async def database():
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            yield db

    web.dependency_overrides[get_db] = database
    # Keep the real independent operator audit writer on the isolated app role.
    monkeypatch.setattr(audit_logger, 'async_session', app_user_sessionmaker)
    token = create_access_token(str(issuer_id), 'platform_admin', str(home_id) if home_id else None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url='http://selected-issuer') as api:
        issued = await api.post(f"/agents/{seed['agent']}/machine-identities",
            json={**machine._issue(seed), 'expected_user_id': str(issuer_id)},
            headers={'Authorization': 'Bearer ' + token, 'X-Tenant-Id': str(seed['tenant'])})
    assert issued.status_code == 201, issued.text
    async with owner_sessionmaker() as db:
        issuer = await db.get(User, issuer_id)
        credential = await db.get(MachineCredential, UUID(issued.json()['credential_id']))
        assert issuer.tenant_id == home_id and issuer.role == 'platform_admin' and issuer.is_active
        assert credential.tenant_id == seed['tenant'] and credential.agent_id == seed['agent']
        assert credential.issued_by_user_id == issuer_id and credential.owner_user_id == seed['owner']
