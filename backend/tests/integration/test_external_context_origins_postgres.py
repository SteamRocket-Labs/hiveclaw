"""External channel context has provenance without acquiring member authority."""

import uuid

import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from app.kernel.engine import _context_message
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.channel_config import ChannelConfig
from app.models.external_principal import ExternalPrincipal
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.services.external_principal_service import load_external_runtime_actor
from app.services.session_model_inputs import (
    capture_context_message_origins,
    load_context_message_origin,
)
from app.services.session_model_round import prepare_model_request
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _external_run(owner_sessionmaker, *, mismatched_principal=False):
    tenant, _owner, agent, session, _run = await _seed_session(owner_sessionmaker)
    principal, run = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        (await db.get(Agent, agent)).timezone = "UTC"
        config_id = uuid.uuid4()
        db.add(
            ChannelConfig(
                id=config_id,
                tenant_id=tenant,
                agent_id=agent,
                channel_type="slack",
                app_id="synthetic-app",
                app_secret="synthetic-secret",
                is_configured=True,
            )
        )
        await db.flush()
        db.add(
            ExternalPrincipal(
                id=principal,
                tenant_id=tenant,
                provider="slack",
                installation_ref=str(config_id),
                channel_config_id=config_id,
                subject_id=f"sender-{principal.hex}",
            )
        )
        await db.flush()
        row = await db.get(ChatSession, session)
        row.user_id = None
        row.external_principal_id = principal
        row.channel = row.source_channel = "slack"
        task_principal = principal
        if mismatched_principal:
            task_principal = uuid.uuid4()
            db.add(
                ExternalPrincipal(
                    id=task_principal,
                    tenant_id=tenant,
                    provider="slack",
                    installation_ref=str(config_id),
                    channel_config_id=config_id,
                    subject_id=f"sender-{task_principal.hex}",
                )
            )
            await db.flush()
        db.add(
            RuntimeTask(
                id=run,
                task_type="web_chat_turn",
                status="running",
                tenant_id=tenant,
                parent_agent_id=agent,
                child_agent_id=agent,
                parent_session_id=str(session),
                child_session_id=str(session),
                root_external_principal_id=task_principal,
                root_session_id=str(session),
                root_runtime_task_id=run,
                metadata_json={"turn_id": f"turn-{run.hex}", "channel": "slack"},
            )
        )
        await db.commit()
    return tenant, agent, session, run, principal


@pytest.mark.parametrize("unbound", [(), ("synthetic_source_unavailable",)])
@pytest.mark.parametrize("change", ["revoke", "change_session_principal"])
async def test_external_context_capture_preserves_sources_and_live_identity(
    owner_sessionmaker,
    app_user_sessionmaker,
    unbound,
    change,
):
    tenant, agent, session, run, principal = await _external_run(owner_sessionmaker)
    message = _context_message(
        role="system", kind="frozen_prompt", content="Platform compiler context", unbound=unbound
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        assert (
            await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one() == (False, False)
        request = await prepare_model_request(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            turn_id=f"turn-{run.hex}",
            round_index=1,
            messages=[message],
            tools=[],
            provider="openai",
            model="synthetic-no-provider",
        )
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        bindings = model.model_request_snapshot_json["input_bindings"]
        assert bindings["dependency_coverage"]["complete"] is (not unbound), bindings
        assert message.source_ref.startswith("session-kernel-context:")
        source_id = uuid.UUID(message.source_ref.split(":", 1)[1])
        proof = await load_context_message_origin(
            db, tenant_id=tenant, agent_id=agent, session_id=session, source_id=source_id
        )
        assert proof["requester_external_principal_id"] == str(principal)
        assert "machine_credential_id" not in proof
        assert proof["dependency_coverage"] == {"complete": not unbound, "unbound": list(unbound)}
        assert (await db.get(InvocationSpan, source_id)).user_id is None
        actor = await load_external_runtime_actor(db, tenant_id=tenant, principal_id=principal, expected_user_id=None)
        assert actor.id is None and actor.role == "external" and not actor.authority_bound
        assert (await db.get(RuntimeTask, run)).root_user_id is None
        await db.commit()
    async with owner_sessionmaker() as db:
        if change == "revoke":
            (await db.get(ExternalPrincipal, principal)).status = "revoked"
        else:
            other = uuid.uuid4()
            db.add(
                ExternalPrincipal(
                    id=other,
                    tenant_id=tenant,
                    provider="slack",
                    installation_ref="synthetic-installation",
                    subject_id=f"sender-{other.hex}",
                )
            )
            await db.flush()
            (await db.get(ChatSession, session)).external_principal_id = other
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        assert (
            await load_context_message_origin(
                db, tenant_id=tenant, agent_id=agent, session_id=session, source_id=source_id
            )
            is None
        )


async def test_external_context_capture_does_not_accept_a_different_run_principal(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    tenant, agent, session, run, _principal = await _external_run(owner_sessionmaker, mismatched_principal=True)
    message = _context_message(role="system", kind="frozen_prompt", content="Platform compiler context", unbound=())
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        await capture_context_message_origins(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            result_id=uuid.uuid4(),
            messages=[message],
        )
        assert message.source_ref is None
        assert not (
            await db.scalars(
                select(InvocationSpan).where(
                    InvocationSpan.runtime_task_id == run, InvocationSpan.span_type == "kernel_context"
                )
            )
        ).all()


@pytest.mark.parametrize(
    "change", ["disable_installation", "rehash_identity", "different_reader", "different_principal"]
)
async def test_external_actor_source_requires_its_current_run_and_installation(
    owner_sessionmaker,
    app_user_sessionmaker,
    change,
):
    from dataclasses import replace
    from app.services.content_input_receipts import (
        ContentInputReceiptError,
        _validate_input_dependencies,
        record_session_actor_identity,
    )
    from app.services.external_principal_service import ExternalChannelSourceReader
    from app.services.session_model_inputs import _hash

    tenant, agent, session, run, principal = await _external_run(owner_sessionmaker)
    reader = ExternalChannelSourceReader(
        agent_id=agent, session_id=session, runtime_task_id=run, external_principal_id=principal
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        ref, proof = await record_session_actor_identity(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            user_id=None,
            runtime_task_id=run,
            display_name=None,
            timezone_name="UTC",
        )
        assert proof["schema"] == "hive.session_actor_identity.external.v1"
        assert proof["external_principal_id"] == str(principal)
        assert proof["channel"] == "slack" and proof["user_id"] is None
        assert "machine_credential_id" not in proof
        await _validate_input_dependencies(db, tenant, [ref], runtime_request=reader)
        with pytest.raises(ContentInputReceiptError) as denied:
            await _validate_input_dependencies(db, tenant, [ref])
        assert denied.value.status_code == 403
        await db.commit()
    async with owner_sessionmaker() as db:
        if change == "disable_installation":
            config_id = (await db.get(ExternalPrincipal, principal)).channel_config_id
            (await db.get(ChannelConfig, config_id)).is_configured = False
        elif change == "rehash_identity":
            forged = {**proof, "display_name": "Forged actor"}
            (await db.get(InvocationSpan, uuid.UUID(ref["source_id"]))).metadata_json = {
                "session_actor_identity": forged
            }
            ref = {**ref, "source_version": _hash(forged)}
        elif change == "different_reader":
            reader = replace(reader, session_id=uuid.uuid4())
        else:
            reader = replace(reader, external_principal_id=uuid.uuid4())
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        with pytest.raises(ContentInputReceiptError) as denied:
            await _validate_input_dependencies(db, tenant, [ref], runtime_request=reader)
        assert denied.value.status_code == (503 if change == "rehash_identity" else 403)
