"""Recover exact old inputs from independent compiler facts, without rewriting history."""

from copy import deepcopy

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.kernel.engine import _context_message, _dicts_to_llm_messages, _dynamic_suffix_notice, _mid_run_items_to_user_messages
from app.models.session_v2 import SessionModelResult
from app.runtime.prompt_builder import build_dynamic_prompt_suffix
from app.services.llm_client import LLMMessage
from app.services.prompt_cache import apply_cache_hints
from app.services.session_model_inputs import _hash, _origin_dependencies, build_model_input_bindings, message_snapshot
from app.services.session_model_round import prepare_model_request
from app.services.session_semantic_history import load_session_semantic_history
from app.services.web_chat_runtime import conversation_from_history_messages
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input
from tests.services.test_session_semantic_history import _seal_round

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _catalog_notice(*, source, unbound=(), catalog="§ Skills\npublic-probe: Public bundled probe", suffix="CURRENT"):
    ledger = []
    text = build_dynamic_prompt_suffix(skill_catalog=catalog, system_prompt_suffix=suffix, context_section_ledger=ledger)
    return _dynamic_suffix_notice(text, sections=ledger, sources=[source] if source else [], unbound=unbound)


@pytest.mark.parametrize("alteration", [None, "prefix", "catalog", "private_skill", "unknown_gap", "foreign_session"])
async def test_old_cache_and_catalog_inventory_recovers_only_exact_independent_sources(
    owner_sessionmaker, app_user_sessionmaker, alteration,
):
    scope = await _bound_input(owner_sessionmaker)
    tenant, agent, session, run, bound = scope
    source = {"source_kind": "agent_platform_skill_file", "source_id": f"{agent}\nskills/public-probe/SKILL.md\noriginal-scope",
        "source_version": "a" * 64}
    original_leaf = {"source_kind": "agent_context_resource", "source_id": f"{agent}\noriginal-private-context",
        "source_version": "b" * 64}
    old_notice = _catalog_notice(source=original_leaf, unbound=("skill_file_unbound:skills/public-probe/SKILL.md",), suffix="OLD")
    # Reproduce the old cache clone which lost ContextMessageOrigin, before persistence.
    old_prefix = apply_cache_hints([LLMMessage(role="system", content="Exact original frozen prefix")], "minimax")[0]
    assert isinstance(old_prefix.content, list)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        request = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=1,
            messages=[old_prefix, *_mid_run_items_to_user_messages(bound), old_notice], tools=[],
            provider="minimax", model="synthetic-no-provider")
        await _seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_index=1, provider_request_id=request, content="Use the original facts.")
        old = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        old_snapshot, old_hash = deepcopy(old.model_request_snapshot_json), old.model_request_hash
        assert old_snapshot["input_bindings"]["dependency_coverage"]["complete"] is False
        old_ref = f"session-model-result:{old.id}"
        await db.commit()

    witness_scope = scope if alteration != "foreign_session" else await _bound_input(owner_sessionmaker)
    if alteration == "private_skill":
        source = {**source, "source_kind": "agent_skill_file"}
    fresh_prefix = _context_message(role="system", kind="frozen_prompt", unbound=(),
        content="Changed prefix" if alteration == "prefix" else "Exact original frozen prefix")
    fresh_notice = _catalog_notice(source=source, unbound=("unknown_private_input",) if alteration == "unknown_gap" else (),
        catalog="§ Skills\nCHANGED" if alteration == "catalog" else "§ Skills\npublic-probe: Public bundled probe")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, witness_scope[0])
        from app.services.session_model_inputs import capture_context_message_origins

        # Capture through the same trusted compiler seam as a real next model request.
        import uuid
        await capture_context_message_origins(db, tenant_id=witness_scope[0], agent_id=witness_scope[1],
            session_id=witness_scope[2], run_id=witness_scope[3], result_id=uuid.uuid4(),
            messages=[fresh_prefix, fresh_notice])
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        deps, complete = await _origin_dependencies(db, tenant_id=tenant, agent_id=agent, ref=old_ref)
        assert complete is (alteration is None), (alteration, deps, complete)
        if alteration is None:
            assert source in deps
            assert original_leaf in deps
            assert any(item["source_kind"] == "session_command" for item in deps)
            history = await load_session_semantic_history(db, tenant_id=tenant, agent_id=agent, session_id=session)
            messages = _dicts_to_llm_messages(conversation_from_history_messages([m for m in history.messages if m.role == "assistant"]))
            current = await build_model_input_bindings(db, tenant_id=tenant, agent_id=agent, session_id=session,
                messages=messages, wire_messages=[message_snapshot(message) for message in messages])
            assert current["dependency_coverage"]["complete"] is True
            recovery = current["historical_source_recovery"]
            assert recovery["models"][0]["model_request_hash"] == old_hash
            assert recovery["models"][0]["model_result_id"] == str(old.id)
            assert recovery["contexts"][0]["witness_ref"] == fresh_notice.source_ref
            assert recovery["contexts"][0]["recovered_paths"] == ["skills/public-probe/SKILL.md"]
        retained = await db.get(SessionModelResult, old.id, populate_existing=True)
        assert retained.model_request_snapshot_json == old_snapshot
        assert retained.model_request_hash == old_hash == _hash(old_snapshot)


@pytest.mark.parametrize("changed_text", [False, True])
async def test_historical_assistant_cache_presentation_rechecks_canonical_bytes(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, changed_text,
):
    import app.services.session_model_inputs as inputs

    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    context = _context_message(role="system", kind="frozen_prompt", content="Platform instructions", unbound=())
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        request = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=1,
            messages=[context, *_mid_run_items_to_user_messages(bound)], tools=[], provider="minimax", model="synthetic-no-provider")
        await _seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_index=1, provider_request_id=request, content="Original assistant bytes")
        history = await load_session_semantic_history(db, tenant_id=tenant, agent_id=agent, session_id=session)
        assistant = _dicts_to_llm_messages(conversation_from_history_messages([item for item in history.messages if item.role == "assistant"]))
        if changed_text:
            assistant[0].content = "Changed assistant bytes"
        selected = apply_cache_hints([context, *_mid_run_items_to_user_messages(bound), *assistant], "minimax")
        assert isinstance(selected[-1].content, list)

        async def legacy_inventory(*args, **kwargs):
            manifest = await build_model_input_bindings(*args, **kwargs)
            entry = manifest["messages"][-1]
            entry.update(status="unresolved", code="source_content_mismatch", dependency_status="incomplete")
            entry.pop("source_ref", None)
            entry.pop("dependencies", None)
            manifest["status"] = "incomplete"
            manifest["dependency_coverage"]["complete"] = False
            return manifest

        # The old compiler writes its old manifest once. Recovery must never edit it.
        with monkeypatch.context() as old_compiler:
            old_compiler.setattr(inputs, "build_model_input_bindings", legacy_inventory)
            request = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
                run_id=run, turn_id=f"turn-{run.hex}", round_index=2, messages=selected, tools=[],
                provider="minimax", model="synthetic-no-provider")
        await _seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_index=2, provider_request_id=request, content="Continue the same work")
        old = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot, digest = deepcopy(old.model_request_snapshot_json), old.model_request_hash
        deps, complete = await _origin_dependencies(db, tenant_id=tenant, agent_id=agent, ref=f"session-model-result:{old.id}")
        assert complete is (not changed_text)
        assert any(item["source_kind"] == "session_command" for item in deps)
        assert old.model_request_snapshot_json == snapshot and old.model_request_hash == digest == _hash(snapshot)


@pytest.mark.parametrize("complete_inputs", [False, True])
@pytest.mark.parametrize("alteration", [None, "missing_commit", "wrong_commit", "wrong_owner", "missing_seal"])
async def test_canonical_committed_inputs_survive_legacy_prepare_reconciliation(
    owner_sessionmaker, app_user_sessionmaker, complete_inputs, alteration,
):
    import uuid
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_model_inputs import capture_context_message_origins

    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    prefix = _context_message(role="system", kind="frozen_prompt", content="Original compiler prefix", unbound=())
    selected = prefix if complete_inputs else apply_cache_hints(
        [LLMMessage(role="system", content="Original compiler prefix")], "minimax",
    )[0]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        request = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, turn_id=f"turn-{run.hex}", round_index=1,
            messages=[selected, *_mid_run_items_to_user_messages(bound)], tools=[],
            provider="minimax", model="synthetic-no-provider")
        await _seal_round(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=f"turn-{run.hex}", round_index=1, provider_request_id=request, content="Committed answer")
        old = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        assert old.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] is complete_inputs
        await capture_context_message_origins(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, result_id=uuid.uuid4(), messages=[prefix])
        # Historical stale prepares changed the aggregate, not its immutable commit.
        old.state = "needs_reconciliation"
        old.reconciliation_owner = "session_model_round:ambiguous_prepare"
        if alteration == "missing_commit":
            old.round_committed_event_id = None
        elif alteration == "wrong_commit":
            old.round_committed_event_id = await db.scalar(select(ChatTranscriptEvent.id).where(
                ChatTranscriptEvent.tenant_id == tenant, ChatTranscriptEvent.result_id == old.id,
                ChatTranscriptEvent.item_kind == "result_commit", ChatTranscriptEvent.lifecycle == "prepared",
            ))
        elif alteration == "wrong_owner":
            old.reconciliation_owner = "session_model_round:ambiguous_failure"
        elif alteration == "missing_seal":
            old.seal_json = None
        frozen = (deepcopy(old.model_request_snapshot_json), old.model_request_hash,
            old.state, deepcopy(old.seal_json), old.round_committed_event_id)
        await db.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        dependencies, complete = await _origin_dependencies(db, tenant_id=tenant, agent_id=agent,
            ref=f"session-model-result:{old.id}")
        assert complete is (alteration is None), (dependencies, complete, alteration)
        if alteration is None:
            assert any(item["source_kind"] == "session_command" for item in dependencies)
            history = await load_session_semantic_history(db, tenant_id=tenant, agent_id=agent, session_id=session)
            assistant = _dicts_to_llm_messages(conversation_from_history_messages(
                [item for item in history.messages if item.role == "assistant"]))
            current_request = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
                run_id=run, turn_id=f"turn-{run.hex}", round_index=2,
                messages=[prefix, *_mid_run_items_to_user_messages(bound), *assistant], tools=[],
                provider="minimax", model="synthetic-no-provider")
            current = await db.scalar(select(SessionModelResult).where(
                SessionModelResult.provider_request_id == current_request))
            bindings = current.model_request_snapshot_json["input_bindings"]
            assert bindings["dependency_coverage"]["complete"] is True
            recovery = next(item for item in bindings["historical_source_recovery"]["models"]
                if item["model_result_id"] == str(old.id))
            assert recovery["canonical_commit_ref"] == f"session-event:{frozen[4]}"
        retained = await db.get(SessionModelResult, old.id, populate_existing=True)
        assert (retained.model_request_snapshot_json, retained.model_request_hash, retained.state,
            retained.seal_json, retained.round_committed_event_id) == frozen
