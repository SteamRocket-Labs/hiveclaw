"""Inspect source failures through the real admin read path, without disclosure or effects."""

import json
import uuid
from copy import deepcopy

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app.database import get_db, pin_rls_tenant_context
from app.kernel.engine import _context_message, _mid_run_items_to_user_messages
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.prompt_cache import apply_cache_hints
from app.services.session_model_inputs import message_snapshot
from app.services.session_model_round import prepare_model_request
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input
from tests.services.test_session_semantic_history import _seal_round

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _input_diagnostic_app(owner_sessionmaker, app_user_sessionmaker, *, extension=False, changed_catalog=False):
    from app.api.admin import router
    from app.core.security import get_current_user
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services.session_model_inputs import _hash, capture_context_message_origins
    from app.services.skill_source_receipts import _bundled_platform_skill_hashes
    from tests.integration.test_m1g46_historical_input_recovery_postgres import _catalog_notice

    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    private = "PRIVATE-PROMPT-" + uuid.uuid4().hex
    package_path = next(path for path in _bundled_platform_skill_hashes() if path.endswith("/SKILL.md"))
    prefix = _context_message(role="system", kind="frozen_prompt", content=private, unbound=())
    leaf = {"source_kind": "agent_context_resource", "source_id": f"{agent}\noriginal-context",
        "source_version": "a" * 64}
    notice = _catalog_notice(source=leaf, unbound=(f"skill_file_unbound:{package_path}",))
    messages = [prefix, *_mid_run_items_to_user_messages(bound), notice]
    cached = apply_cache_hints(messages, "minimax")
    async with owner_sessionmaker() as db:
        user_id = await db.scalar(select(ChatSession.user_id).where(ChatSession.id == session))
        actor = await db.get(User, user_id)
        actor.role = "platform_admin"
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        request = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=1, messages=messages, tools=[],
            provider="minimax", model="synthetic-no-provider",
            wire_request={"messages": [message_snapshot(m) for m in cached], "tools": []})
        await _seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_index=1, provider_request_id=request, content=private + "-RESPONSE")
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        assert model.model_request_snapshot_json["input_bindings"]["messages"][0]["code"] == "source_wire_mismatch"
        if extension:
            snapshot = deepcopy(model.model_request_snapshot_json)
            snapshot["input_bindings"]["dependency_coverage"]["private_extension"] = private
            model.model_request_snapshot_json = snapshot
            model.model_request_hash = _hash(snapshot)
        model_id = model.id
        frozen = (deepcopy(model.model_request_snapshot_json), model.model_request_hash, model.state)
        task = await db.get(RuntimeTask, run)
        task.status = "needs_reconciliation"
        task.metadata_json = {**task.metadata_json, "reconciliation_reason": "synthetic_source_gap"}
        public = {"source_kind": "agent_platform_skill_file",
            "source_id": f"{agent}\n{package_path}\noriginal-scope", "source_version": "b" * 64}
        options = {"catalog": private} if changed_catalog else {}
        witness = _catalog_notice(source=public, **options)
        await capture_context_message_origins(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, result_id=uuid.uuid4(), messages=[witness])
        current_notice = _catalog_notice(source=leaf,
            unbound=("skill_installation_snapshot_unavailable", private))
        current_request = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=2,
            messages=[prefix, *_mid_run_items_to_user_messages(bound), current_notice], tools=[],
            provider="minimax", model="synthetic-no-provider")
        await _seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_index=2, provider_request_id=current_request, content=private)
        await db.commit()

    app = FastAPI()
    app.include_router(router)

    async def current_user():
        return actor

    async def readonly_db():
        async with app_user_sessionmaker() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            try:
                yield db
            finally:
                assert not db.dirty and not db.new and not db.deleted
                await db.rollback()

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = readonly_db
    return app, tenant, run, model_id, private, frozen


@pytest.mark.parametrize("extension", [False, True])
@pytest.mark.parametrize("changed_catalog", [False, True])
async def test_admin_input_source_detail_is_opt_in_scoped_and_read_only(
    owner_sessionmaker, app_user_sessionmaker, extension, changed_catalog,
):
    app, tenant, run, model_id, private, frozen = await _input_diagnostic_app(
        owner_sessionmaker, app_user_sessionmaker, extension=extension, changed_catalog=changed_catalog)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        default = await client.get(f"/admin/runtime-reconciliation/{run}", params={"tenant_id": str(tenant)})
        assert default.status_code == 200 and "input_sources" not in default.json()
        response = await client.get(f"/admin/runtime-reconciliation/{run}", params={
            "tenant_id": str(tenant), "include_input_sources": "true",
        })
        assert response.status_code == 200, response.text
        diagnostics = response.json()["input_sources"]
        assert diagnostics["schema"] == "hive.runtime_input_sources.v1"
        assert diagnostics["models"][0]["request_hash_matches"] is True
        assert diagnostics["models"][0]["incomplete_messages"][0]["code"] == "source_wire_mismatch"
        assert diagnostics["compiler_witnesses"]["cache_presentation_matches"] >= 1
        contexts = diagnostics["context_sources"]
        assert len(contexts) == 2
        context = contexts[0]
        assert context["available"] is True and context["dependencies_valid"] is True
        assert context["missing_skill_file_count"] == 1 and context["other_missing_count"] == 0
        assert context["missing_packaged_path_count"] == 1
        assert context["catalog"]["available"] is True
        assert context["witnesses"][0]["catalog_matches"] is (not changed_catalog)
        assert context["witnesses"][0]["missing_skill_paths_covered"] == 1
        assert contexts[1]["known_unbound_codes"] == ["skill_installation_snapshot_unavailable"]
        assert contexts[1]["other_missing_count"] == 2
        assert private not in json.dumps(diagnostics)
        foreign = await client.get(f"/admin/runtime-reconciliation/{run}", params={
            "tenant_id": str(uuid.uuid4()), "include_input_sources": "true",
        })
        assert foreign.status_code == 404
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        role = (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        assert role == (False, False)
        model = await db.get(SessionModelResult, model_id)
        assert (model.model_request_snapshot_json, model.model_request_hash, model.state) == frozen
        assert await db.scalar(select(func.count()).select_from(SessionToolInvocation).where(
            SessionToolInvocation.run_id == run)) == 0
