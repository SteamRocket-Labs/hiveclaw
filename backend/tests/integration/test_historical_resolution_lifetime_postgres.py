"""A later source read cannot inherit an expired compiler recovery result."""

import asyncio
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from app.kernel.engine import _context_message, _mid_run_items_to_user_messages
from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionModelResult
from app.services.historical_model_inputs import source_resolution
from app.services.llm_client import LLMMessage
from app.services.prompt_cache import apply_cache_hints
from app.services.session_model_inputs import _origin_dependencies, capture_context_message_origins
from app.services.session_model_round import prepare_model_request
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input
from tests.integration.test_m1g46_historical_input_recovery_postgres import _catalog_notice
from tests.services.test_session_semantic_history import _seal_round

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("entry", ["model", "context"])
async def test_inherited_reader_rechecks_recovery_after_its_original_scope_ends(
    owner_sessionmaker, app_user_sessionmaker, entry
):
    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    public = {
        "source_kind": "agent_platform_skill_file",
        "source_id": f"{agent}\nskills/public-probe/SKILL.md\noriginal-scope",
        "source_version": "a" * 64,
    }
    original = {
        "source_kind": "agent_context_resource",
        "source_id": f"{agent}\noriginal-private-context",
        "source_version": "b" * 64,
    }
    old_notice = _catalog_notice(
        source=original, unbound=("skill_file_unbound:skills/public-probe/SKILL.md",), suffix="OLD"
    )
    old_prefix = apply_cache_hints([LLMMessage(role="system", content="Exact original frozen prefix")], "minimax")[0]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        request = await prepare_model_request(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            turn_id=f"turn-{run.hex}",
            round_index=1,
            messages=[old_prefix, *_mid_run_items_to_user_messages(bound), old_notice],
            tools=[],
            provider="minimax",
            model="synthetic-no-provider",
        )
        await _seal_round(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            turn_id=f"turn-{run.hex}",
            round_index=1,
            provider_request_id=request,
            content="Use the independently recorded compiler facts.",
        )
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        frozen, digest, model_id = deepcopy(model.model_request_snapshot_json), model.model_request_hash, model.id
        assert frozen["input_bindings"]["dependency_coverage"]["complete"] is False
        context_ref = old_notice.source_ref
        assert context_ref.startswith("session-kernel-context:")
        prefix = _context_message(
            role="system", kind="frozen_prompt", content="Exact original frozen prefix", unbound=()
        )
        witness = _catalog_notice(source=public)
        await capture_context_message_origins(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            result_id=uuid4(),
            messages=[prefix, witness],
        )
        witness_id = UUID(witness.source_ref.split(":", 1)[1])
        await db.commit()

    ref = f"session-model-result:{model_id}" if entry == "model" else context_ref
    continue_read = asyncio.Event()

    async def inherited_read():
        await continue_read.wait()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            return await _origin_dependencies(db, tenant_id=tenant, agent_id=agent, ref=ref)

    with source_resolution():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            role = (
                await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))
            ).one()
            assert role == (False, False)
            dependencies, complete = await _origin_dependencies(db, tenant_id=tenant, agent_id=agent, ref=ref)
            assert complete and public in dependencies and original in dependencies
            held = asyncio.create_task(inherited_read())

    try:
        async with owner_sessionmaker() as writer:
            (await writer.get(InvocationSpan, witness_id)).status = "failed"
            await writer.commit()
        async with app_user_sessionmaker() as fresh:
            await pin_rls_tenant_context(fresh, tenant)
            _dependencies, current_complete = await _origin_dependencies(
                fresh, tenant_id=tenant, agent_id=agent, ref=ref
            )
            assert current_complete is False
        continue_read.set()
        _dependencies, inherited_complete = await asyncio.wait_for(held, 10)
        assert inherited_complete is current_complete
    finally:
        continue_read.set()
        await asyncio.gather(held, return_exceptions=True)
        async with owner_sessionmaker() as writer:
            (await writer.get(InvocationSpan, witness_id)).status = "captured"
            await writer.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        dependencies, complete = await _origin_dependencies(db, tenant_id=tenant, agent_id=agent, ref=ref)
        assert complete and public in dependencies and original in dependencies
        retained = await db.get(SessionModelResult, model_id, populate_existing=True)
        assert retained.model_request_snapshot_json == frozen and retained.model_request_hash == digest
