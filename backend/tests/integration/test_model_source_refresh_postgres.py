"""Recovered model origins must use the current persisted result."""

from copy import deepcopy

import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from app.kernel.engine import _mid_run_items_to_user_messages
from app.models.session_v2 import SessionModelResult
from app.services.session_model_inputs import _origin_dependencies
from app.services.session_model_round import prepare_model_request
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input
from tests.services.test_session_semantic_history import _seal_round

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("mutation", ["reconciliation", "snapshot"])
async def test_retained_model_origin_observes_current_state_and_snapshot(
    owner_sessionmaker, app_user_sessionmaker, mutation
):
    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        role = (await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        assert role == (False, False)
        request_id = await prepare_model_request(
            db,
            tenant_id=tenant,
            agent_id=agent,
            session_id=session,
            run_id=run,
            turn_id=f"turn-{run.hex}",
            round_index=1,
            messages=_mid_run_items_to_user_messages(bound),
            tools=[],
            provider="openai",
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
            provider_request_id=request_id,
            content="Bound result",
        )
        retained = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id)
        )
        source_ref = f"session-model-result:{retained.id}"
        original_state = retained.state
        original_snapshot = deepcopy(retained.model_request_snapshot_json)
        expected, complete = await _origin_dependencies(db, tenant_id=tenant, agent_id=agent, ref=source_ref)
        assert complete and expected
        assert [row["source_kind"] for row in expected] == ["session_command"]
        await db.commit()

        async with owner_sessionmaker() as writer:
            current = await writer.get(SessionModelResult, retained.id)
            if mutation == "reconciliation":
                current.state = "needs_reconciliation"
            else:
                changed = deepcopy(current.model_request_snapshot_json)
                changed["input_bindings"]["dependency_coverage"]["complete"] = False
                current.model_request_snapshot_json = changed
            await writer.commit()
        try:
            async with app_user_sessionmaker() as fresh:
                await pin_rls_tenant_context(fresh, tenant)
                _sources, current_complete = await _origin_dependencies(
                    fresh, tenant_id=tenant, agent_id=agent, ref=source_ref
                )
                assert current_complete is False
            await pin_rls_tenant_context(db, tenant)
            _sources, retained_complete = await _origin_dependencies(
                db, tenant_id=tenant, agent_id=agent, ref=source_ref
            )
            assert retained_complete is current_complete
        finally:
            async with owner_sessionmaker() as writer:
                current = await writer.get(SessionModelResult, retained.id)
                current.state = original_state
                current.model_request_snapshot_json = original_snapshot
                await writer.commit()

        restored, complete = await _origin_dependencies(db, tenant_id=tenant, agent_id=agent, ref=source_ref)
        assert complete and restored == expected
        assert retained.state == original_state
        assert retained.model_request_snapshot_json == original_snapshot
