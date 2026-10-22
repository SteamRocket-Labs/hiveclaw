"""Personal search checks current source authority after asynchronous retrieval."""

import json
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select

from app.api import agent_knowledge
from app.core.security import create_access_token
from app.database import get_db, tenant_scoped_session
from app.models.agent import AgentPermission
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, KnowledgeSegment
from app.services.personal_knowledge_service import PersonalKnowledgeService
from tests.integration.test_personal_knowledge_cross_owner import _seed_two_owners

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('change', [None, 'grant_revoked', 'document_archived', 'credential_reclassified'])
@pytest.mark.parametrize('retrieval', ['text', 'vector'])
async def test_personal_search_rechecks_sources_after_vector_lookup(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, change, retrieval,
):
    seed = await _seed_two_owners(owner_sessionmaker)
    grant_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(AgentPermission(tenant_id=seed['tenant'], agent_id=seed['agentA'],
            scope_type='user', scope_id=seed['ownerB'], access_level='use'))
        db.add(KnowledgeGrant(id=grant_id, tenant_id=seed['tenant'], scope_id=seed['ownerA'],
            resource_type='document', resource_id=seed['docA'], document_id=seed['docA'],
            grantee_type='user', grantee_id=seed['ownerB'], permission='read', sensitivity_ceiling='PL4_credential'))
        segment_id = await db.scalar(select(KnowledgeSegment.id).where(KnowledgeSegment.document_id == seed['docA']))
        await db.commit()
    observed = {'calls': 0}

    class Vector:
        async def search_personal_segments(self, **arguments):
            assert arguments['tenant_id'] == seed['tenant'] and arguments['owner_user_id'] == seed['ownerA']
            observed['calls'] += 1
            if change:
                from datetime import datetime, timezone

                async with owner_sessionmaker() as db:
                    if change == 'grant_revoked':
                        (await db.get(KnowledgeGrant, grant_id)).revoked_at = datetime.now(timezone.utc)
                    elif change == 'document_archived':
                        (await db.get(KnowledgeDocument, seed['docA'])).status = 'archived'
                    else:
                        (await db.get(KnowledgeDocument, seed['docA'])).sensitivity = 'PL4_credential'
                    await db.commit()
            return [{'segment_id': str(segment_id), 'score': 1.0}] if retrieval == 'vector' else []

    monkeypatch.setattr(PersonalKnowledgeService, '_vector_index_provider', lambda _self: Vector())
    app = FastAPI()
    app.include_router(agent_knowledge.router)

    async def database():
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            yield db

    app.dependency_overrides[get_db] = database
    token = create_access_token(str(seed['ownerB']), 'member', str(seed['tenant']))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://personal-search') as api:
        response = await api.get(f"/agents/{seed['agentA']}/knowledge/personal/search",
            params={'q': 'hello' if retrieval == 'text' else 'semantic-only-query'},
            headers={'Authorization': 'Bearer ' + token})
    observed.update(change=change, retrieval=retrieval, status_code=response.status_code, response=response.json())
    (tmp_path / 'personal-search-current-sources.json').write_text(json.dumps(observed, indent=2))
    assert response.status_code == 200 and observed['calls'] == 1, observed
    results = response.json()['results']
    if change == 'credential_reclassified':
        assert len(results) == 1 and results[0]['sensitivity'] == 'PL4_credential', observed
        assert results[0]['snippet'] == '' and results[0]['source_version'] is None, observed
        assert 'hello owner A' not in response.text and 'ownerA-doc' not in response.text, observed
    elif change:
        assert results == [] and 'hello owner A' not in response.text, observed
    else:
        assert len(results) == 1 and results[0]['snippet'] == 'hello owner A', observed
