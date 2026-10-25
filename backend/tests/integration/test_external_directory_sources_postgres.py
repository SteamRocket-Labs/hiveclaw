"""Directory observations retain the exact unbound channel audience, never a User."""

from dataclasses import replace
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.config_revision import ConfigRevision
from app.models.external_principal import ExternalPrincipal
from app.models.invocation_span import InvocationSpan
from app.services.agent_directory_sources import observe_directory, validate_directory
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.external_principal_service import ExternalChannelSourceReader
from tests.integration.test_external_context_origins_postgres import _external_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("change", ["revoke", "wrong_session", "wrong_principal"])
async def test_external_directory_observation_uses_its_installed_sender_and_work(
    owner_sessionmaker,
    app_user_sessionmaker,
    change,
):
    tenant, agent_id, session_id, run_id, principal_id = await _external_run(owner_sessionmaker)
    peer_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        parent = await db.get(Agent, agent_id)
        db.add(
            Agent(
                id=peer_id,
                tenant_id=tenant,
                name="Observed colleague",
                role_description="Prepare authorized reports",
                creator_id=parent.creator_id,
                owner_user_id=parent.owner_user_id,
            )
        )
        await db.commit()
    reader = ExternalChannelSourceReader(
        agent_id=agent_id, session_id=session_id, runtime_task_id=run_id, external_principal_id=principal_id
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        content, dependency = await observe_directory(db, tenant_id=tenant, agent_id=agent_id, runtime_request=reader)
        assert "Observed colleague" in content
        row = await db.get(InvocationSpan, uuid.UUID(dependency["source_id"]))
        proof = row.metadata_json["agent_directory_observation"]
        assert proof["schema"] == "hive.agent_directory_observation.external.v1"
        assert row.user_id is None and proof["reader_user_id"] is None
        assert proof["reader_external_principal_id"] == str(principal_id)
        assert proof["session_id"] == str(session_id) and proof["run_id"] == str(run_id)
        assert await db.scalar(select(ConfigRevision.id).where(ConfigRevision.entity_id == peer_id)) is None
        await validate_directory(db, tenant, dependency, runtime_request=reader)
        with pytest.raises(ContentInputReceiptError) as anonymous:
            await validate_directory(db, tenant, dependency)
        assert anonymous.value.status_code == 403
        await db.commit()
    async with owner_sessionmaker() as db:
        if change == "revoke":
            (await db.get(ExternalPrincipal, principal_id)).status = "revoked"
        await db.commit()
    denied_reader = (
        replace(reader, session_id=uuid.uuid4())
        if change == "wrong_session"
        else replace(reader, external_principal_id=uuid.uuid4())
        if change == "wrong_principal"
        else reader
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        with pytest.raises(ContentInputReceiptError) as denied:
            await validate_directory(db, tenant, dependency, runtime_request=denied_reader)
        assert denied.value.status_code == 403
