"""Configured primary authority at the live resolution entries.

A primary_model_id that is missing, disabled, or outside the agent tenant must
stop that execution before any provider call. An explicit fallback is only a
model when no primary is configured.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

_MISSING = object()


class _Rows:
    def __init__(self, one, many=None):
        self._one = one
        self._many = list(many if many is not None else ([] if one is None else [one]))

    def scalar_one_or_none(self):
        return self._one

    def scalars(self):
        rows = self._many
        return SimpleNamespace(first=lambda: rows[0] if rows else None, all=lambda: list(rows))


class CatalogSession:
    """Apply id, tenant, and enabled predicates that are actually in WHERE."""

    def __init__(self, **buckets):
        self.buckets = {key: list(value) for key, value in buckets.items()}
        self.added = []
        self.tenant_active = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def execute(self, statement):
        text = " ".join(str(statement).lower().split())
        if "app.current_tenant_id" in text:
            return _Rows(None)
        try:
            compiled = statement.compile()
            sql = " ".join(str(compiled).lower().split())
            params = dict(compiled.params)
        except Exception:
            return _Rows(None)
        where = _where_clause(sql)
        table = _from_table(sql)
        if table == "tenants":
            return _Rows(self.tenant_active)
        rows = _matching_rows(self.buckets.get(table, []), where, params, table)
        if table == "chat_messages":
            rows = []
        return _Rows(rows[0] if rows else None, rows)


def _where_clause(sql: str) -> str:
    if " where " not in sql:
        return ""
    where = sql.split(" where ", 1)[1]
    for token in (" order by ", " group by ", " limit ", " for update "):
        if token in where:
            where = where.split(token, 1)[0]
    return where


def _from_table(sql: str) -> str:
    match = re.search(r"\bfrom\s+([a-z_]+)", sql)
    return match.group(1) if match else ""


def _same(left, right) -> bool:
    if left is _MISSING or right is _MISSING:
        return False
    if left is None or right is None:
        return left is None and right is None
    return str(left) == str(right)


def _column_ops(where: str, column: str, params: dict):
    equals = []
    differs = []
    for match in re.finditer(rf"(?:^|[.\s]){column}\s*(=|!=|<>)\s*:(\w+)", where):
        value = params.get(match.group(2), _MISSING)
        if match.group(1) == "=":
            equals.append(value)
        else:
            differs.append(value)
    is_null = re.search(rf"(?:^|[.\s]){column}\s+is\s+null\b", where) is not None
    return equals, differs, is_null


def _matching_rows(rows, where: str, params: dict, table: str):
    enabled_required = re.search(r"\benabled\s+is\s+(true|1)\b", where) is not None
    id_column = "ref_id" if table == "participants" else "id"
    matched = []
    for row in rows:
        if not _row_matches(row, where, params, id_column=id_column, enabled_required=enabled_required):
            continue
        matched.append(row)
    return matched


def _row_matches(row, where: str, params: dict, *, id_column: str, enabled_required: bool) -> bool:
    equals, differs, is_null = _column_ops(where, id_column, params)
    row_id = getattr(row, id_column, None)
    if is_null and row_id is not None:
        return False
    if equals and not any(_same(row_id, value) for value in equals):
        return False
    if any(_same(row_id, value) for value in differs):
        return False
    tenant_equals, _, tenant_null = _column_ops(where, "tenant_id", params)
    row_tenant = getattr(row, "tenant_id", None)
    if tenant_null and row_tenant is not None:
        return False
    if tenant_equals and not any(_same(row_tenant, value) for value in tenant_equals):
        return False
    if table_type_constrained(where, params, row):
        return False
    if enabled_required and getattr(row, "enabled", True) is not True:
        return False
    return True


def table_type_constrained(where: str, params: dict, row) -> bool:
    equals, _, _ = _column_ops(where, "type", params)
    if not equals:
        return False
    return not any(_same(getattr(row, "type", None), value) for value in equals)


def _model(tenant_id, *, name, enabled=True, model_id=None):
    return SimpleNamespace(
        id=model_id or uuid4(),
        tenant_id=tenant_id,
        provider="synthetic",
        model=name,
        label=name,
        enabled=enabled,
        api_key="synthetic",
        base_url=None,
        supports_vision=False,
    )


def _agent(tenant_id, **over):
    data = {
        "agent_type": "native",
        "id": uuid4(),
        "name": "Configured Agent",
        "role_description": "role",
        "tenant_id": tenant_id,
        "status": "active",
        "primary_model_id": None,
        "fallback_model_id": None,
        "owner_user_id": uuid4(),
        "creator_id": uuid4(),
        "deleted_at": None,
        "deactivated_at": None,
        "max_tool_rounds": None,
    }
    data.update(over)
    return SimpleNamespace(**data)


def _user(tenant_id, user_id):
    return SimpleNamespace(id=user_id, tenant_id=tenant_id, is_active=True, username="owner")


def _participant(agent_id):
    return SimpleNamespace(id=uuid4(), type="agent", ref_id=agent_id, tenant_id=None)


def _session_for(agent, models, **extra):
    user = _user(agent.tenant_id, agent.owner_user_id)
    return CatalogSession(
        agents=[agent, *extra.pop("agents", [])],
        llm_models=list(models),
        users=[user, *extra.pop("users", [])],
        participants=[_participant(agent.id), *extra.pop("participants", [])],
        tasks=list(extra.pop("tasks", [])),
        **extra,
    )


def _configured_pair(kind: str):
    tenant_id = uuid4()
    other_tenant = uuid4()
    primary = _model(tenant_id, name="primary-model", enabled=kind != "disabled")
    if kind == "cross_tenant":
        primary.tenant_id = other_tenant
    fallback = _model(tenant_id, name="fallback-model", enabled=True)
    agent = _agent(tenant_id, primary_model_id=primary.id, fallback_model_id=fallback.id)
    present = [] if kind == "missing" else [primary]
    return agent, present + [fallback], primary, fallback


def _stub_heartbeat(monkeypatch, session):
    from app.services import heartbeat

    async def _noop(*_args, **_kwargs):
        return None

    async def _sweep(*_args, **_kwargs):
        return SimpleNamespace(recovered_stale=[], retried=[], alerted=False, committed=[], exhausted=[], deferred=[])

    monkeypatch.setattr(heartbeat, "tenant_scoped_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(heartbeat, "_run_memory_lifecycle_maintenance", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(heartbeat, "_run_t2_job_sweep", _sweep)
    monkeypatch.setattr(heartbeat, "_run_consolidation_debt_refresh", lambda *_a, **_k: _noop())
    monkeypatch.setattr(heartbeat, "_run_t2_retention", lambda *_a, **_k: _noop())
    monkeypatch.setattr(heartbeat, "_run_chat_artifact_snapshot_retention", _noop)
    monkeypatch.setattr(heartbeat, "_run_convergence_dirtiness_refresh", lambda *_a, **_k: _noop())
    monkeypatch.setattr("app.core.execution_context.set_agent_bot_identity", lambda *_a, **_k: None)

    async def _debt(*_args, **_kwargs):
        return SimpleNamespace(stalled=False)

    async def _retention(*_args, **_kwargs):
        return SimpleNamespace(archived=[], kept_referenced=0, kept_pipeline=0)

    async def _dirt(*_args, **_kwargs):
        return SimpleNamespace(dirty_files=[])

    monkeypatch.setattr(heartbeat, "_run_consolidation_debt_refresh", _debt)
    monkeypatch.setattr(heartbeat, "_run_t2_retention", _retention)
    monkeypatch.setattr(heartbeat, "_run_convergence_dirtiness_refresh", _dirt)


async def _web_terminal(agent, primary, fallback):
    from app.services.web_chat_run_orchestrator import _WebChatRunState, _handle_pre_invocation_terminal

    finalized = []

    async def finalize_without_assistant(**kwargs):
        finalized.append(kwargs)

    async def maybe_enter_plan_mode(**_kwargs):
        return None

    state = _WebChatRunState(
        run_uuid=uuid4(),
        ports=SimpleNamespace(
            context=SimpleNamespace(maybe_enter_plan_mode=maybe_enter_plan_mode),
            terminal=SimpleNamespace(finalize_without_assistant=finalize_without_assistant),
            artifacts=SimpleNamespace(
                file_change_paths=lambda _ctx: [],
                file_change_states=lambda _ctx: [],
                file_change_lineage=lambda _ctx: [],
            ),
        ),
        cancel_event=asyncio.Event(),
        run_key="m0h",
        broadcast_token=None,
        agent=agent,
        llm_model=primary,
        fallback_model=fallback,
        session_id="sess",
        prompt="hello",
    )
    stopped = await _handle_pre_invocation_terminal(state)
    return stopped, finalized


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "missing", "cross_tenant"])
async def test_web_configured_primary_stops_before_provider(kind):
    from app.services.web_chat_runtime import _resolve_runtime_models_for_task

    agent, models, _primary, fallback = _configured_pair(kind)
    session = _session_for(agent, models)
    resolved, resolved_fallback = await _resolve_runtime_models_for_task(session, agent=agent, metadata={})
    stopped, finalized = await _web_terminal(agent, resolved, resolved_fallback)

    assert stopped is True and resolved is None and resolved_fallback is None, {
        "kind": kind,
        "stopped": stopped,
        "model": getattr(resolved, "model", None),
        "enabled": getattr(resolved, "enabled", None),
        "fallback": getattr(resolved_fallback, "model", None),
    }
    assert finalized[0]["metadata_json"]["error_code"] == "llm_model_missing"
    assert finalized[0]["metadata_json"]["requires_user_decision"] is True
    assert finalized[0]["failure"]["delivery_state"] == "unavailable"
    assert fallback.model != getattr(resolved, "model", None)


@pytest.mark.asyncio
async def test_web_keeps_enabled_primary_and_drops_disabled_fallback():
    from app.services.web_chat_runtime import _resolve_runtime_models_for_task

    tenant_id = uuid4()
    primary = _model(tenant_id, name="primary-model", enabled=True)
    fallback = _model(tenant_id, name="fallback-model", enabled=False)
    agent = _agent(tenant_id, primary_model_id=primary.id, fallback_model_id=fallback.id)
    resolved, resolved_fallback = await _resolve_runtime_models_for_task(
        _session_for(agent, [primary, fallback]),
        agent=agent,
        metadata={},
    )
    stopped, _finalized = await _web_terminal(agent, resolved, resolved_fallback)

    assert resolved is primary
    assert resolved_fallback is None
    assert stopped is False


@pytest.mark.asyncio
async def test_web_fallback_only_promotes_explicit_fallback():
    from app.services.web_chat_runtime import _resolve_runtime_models_for_task

    tenant_id = uuid4()
    fallback = _model(tenant_id, name="fallback-only", enabled=True)
    agent = _agent(tenant_id, primary_model_id=None, fallback_model_id=fallback.id)
    resolved, resolved_fallback = await _resolve_runtime_models_for_task(
        _session_for(agent, [fallback]),
        agent=agent,
        metadata={},
    )
    stopped, _finalized = await _web_terminal(agent, resolved, resolved_fallback)

    assert resolved is fallback
    assert resolved_fallback is None
    assert stopped is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "missing", "cross_tenant"])
async def test_channel_configured_primary_does_not_call_provider(monkeypatch, kind):
    from app.services.channel_agent_runtime import call_agent_llm

    agent, models, _primary, _fallback = _configured_pair(kind)
    calls = []

    async def fake_call_llm(model, _messages, _name, _role, **kwargs):
        calls.append({"model": model, "fallback": kwargs.get("fallback_model")})
        return "provider"

    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)
    reply = await call_agent_llm(_session_for(agent, models), agent.id, "hello", session_id=None)

    assert calls == []
    assert "主模型" in reply


@pytest.mark.asyncio
async def test_channel_fallback_only_calls_provider_with_that_model(monkeypatch):
    from app.services.channel_agent_runtime import call_agent_llm

    tenant_id = uuid4()
    fallback = _model(tenant_id, name="fallback-only", enabled=True)
    agent = _agent(tenant_id, primary_model_id=None, fallback_model_id=fallback.id)
    calls = []

    async def fake_call_llm(model, _messages, _name, _role, **kwargs):
        calls.append({"model": model, "fallback": kwargs.get("fallback_model")})
        return "ok"

    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)
    reply = await call_agent_llm(_session_for(agent, [fallback]), agent.id, "hello", session_id=None)

    assert reply == "ok"
    assert calls == [{"model": fallback, "fallback": None}]


@pytest.mark.asyncio
async def test_channel_disabled_fallback_does_not_reach_provider(monkeypatch):
    from app.services.channel_agent_runtime import call_agent_llm

    tenant_id = uuid4()
    primary = _model(tenant_id, name="primary-model", enabled=True)
    fallback = _model(tenant_id, name="fallback-model", enabled=False)
    agent = _agent(tenant_id, primary_model_id=primary.id, fallback_model_id=fallback.id)
    calls = []

    async def fake_call_llm(model, _messages, _name, _role, **kwargs):
        calls.append({"model": model, "fallback": kwargs.get("fallback_model")})
        return "ok"

    monkeypatch.setattr("app.api.websocket.call_llm", fake_call_llm)
    reply = await call_agent_llm(_session_for(agent, [primary, fallback]), agent.id, "hello", session_id=None)

    assert reply == "ok"
    assert calls == [{"model": primary, "fallback": None}]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "missing", "cross_tenant"])
async def test_heartbeat_configured_primary_skips_before_core(monkeypatch, kind):
    from app.services import heartbeat

    agent, models, _primary, _fallback = _configured_pair(kind)
    session = _session_for(agent, models)
    captured = []
    updates = []

    async def fake_core(**kwargs):
        captured.append(kwargs.get("model"))

    async def fake_update(task_id, **fields):
        updates.append(fields)
        return True

    _stub_heartbeat(monkeypatch, session)
    monkeypatch.setattr(heartbeat, "_run_heartbeat_core_and_persist", fake_core)
    monkeypatch.setattr(heartbeat, "update_runtime_task_record", fake_update)

    await heartbeat._execute_heartbeat(agent.id, tenant_id=agent.tenant_id, lease_acquired=True, runtime_task_id="hb-1")

    assert captured == [], [getattr(model, "model", None) for model in captured]
    assert updates[-1]["status"] == "skipped"
    assert updates[-1]["metadata_json"]["skip_reason"] == "primary_model_unavailable"
    assert updates[-1]["metadata_json"]["primary_model_id"] == str(agent.primary_model_id)
    from app.services.autonomy_overview import _attention_from_last_attempt

    code, _text, action = _attention_from_last_attempt(
        SimpleNamespace(
            status=updates[-1]["status"],
            metadata_json=updates[-1]["metadata_json"],
            result_summary=updates[-1].get("result_summary"),
        )
    )
    assert code == "missing_model"
    assert action == "configure_model"


@pytest.mark.asyncio
async def test_heartbeat_fallback_only_still_runs_core(monkeypatch):
    from app.services import heartbeat

    tenant_id = uuid4()
    fallback = _model(tenant_id, name="fallback-only", enabled=True)
    agent = _agent(tenant_id, primary_model_id=None, fallback_model_id=fallback.id)
    captured = []

    async def fake_core(**kwargs):
        captured.append(kwargs.get("model"))

    _stub_heartbeat(monkeypatch, _session_for(agent, [fallback]))
    monkeypatch.setattr(heartbeat, "_run_heartbeat_core_and_persist", fake_core)
    monkeypatch.setattr(heartbeat, "update_runtime_task_record", lambda *_a, **_k: _async_true())

    await heartbeat._execute_heartbeat(agent.id, tenant_id=tenant_id, lease_acquired=True, runtime_task_id="hb-2")

    assert captured == [fallback]


async def _async_true():
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "missing", "cross_tenant"])
async def test_evolution_configured_primary_holds_curator(monkeypatch, kind):
    from app.services import evolution_daemon

    agent, models, _primary, _fallback = _configured_pair(kind)
    reviews = []

    async def fake_review(*_args, **_kwargs):
        reviews.append("review")
        return {"status": "ran"}

    async def fake_distill(*_args, **_kwargs):
        reviews.append("distill")
        return None

    async def fake_curator(*_args, **_kwargs):
        reviews.append("curator")
        return {"status": "ran"}

    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_a, **_k: _session_for(agent, models))
    monkeypatch.setattr("app.tools.workspace.ensure_workspace", lambda *_a, **_k: _async_path())
    monkeypatch.setattr(
        "app.runtime.invoker._resolve_runtime_config",
        lambda *_a, **_k: _async_value(SimpleNamespace(tenant_resolution_error=None)),
    )
    monkeypatch.setattr(
        "app.services.provisional_trial.sweep_expired_provisional_trials",
        lambda _workspace: {"status": "ok"},
    )
    monkeypatch.setattr("app.services.provisional_trial.review_pending_provisional_trials_with_model", fake_review)
    monkeypatch.setattr(evolution_daemon, "_maybe_run_skill_distillation", fake_distill)
    monkeypatch.setattr(evolution_daemon, "_maybe_run_skill_curator", fake_curator)
    monkeypatch.setattr(
        "app.memory.enhancement.sync_t3_to_memory_enhancement",
        lambda *_a, **_k: _async_value(SimpleNamespace(synced=0, skipped=0, reason="test")),
    )

    report = await evolution_daemon.run_heartbeat_evolution_maintenance(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        outcome_type=None,
        current_session_id=None,
    )

    assert reviews == []
    assert report["skill_curator"] == {"status": "held", "reason": "model_unavailable"}


async def _async_path():
    return "/tmp/m0h-workspace"


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_evolution_fallback_only_resolves_that_model(monkeypatch):
    from app.services.evolution_daemon import _resolve_agent_model

    tenant_id = uuid4()
    fallback = _model(tenant_id, name="fallback-only", enabled=True)
    agent = _agent(tenant_id, primary_model_id=None, fallback_model_id=fallback.id)
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_a, **_k: _session_for(agent, [fallback]))

    resolved = await _resolve_agent_model(agent.id, tenant_id)

    assert resolved is fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "missing", "cross_tenant"])
async def test_subagent_dispatch_does_not_spawn_configured_primary(monkeypatch, kind):
    from app.agents.subagent import SubagentResult
    from app.services import subagent_run_service as service

    agent, models, _primary, _fallback = _configured_pair(kind)
    user_id = uuid4()
    run_id = uuid4().hex
    spawned = []
    updates = []
    record = {
        "task_id": run_id,
        "task_type": "subagent",
        "status": "pending",
        "parent_agent_id": str(agent.id),
        "root_user_id": str(user_id),
        "prompt": "inspect the notes",
        "metadata": {"resume_after_restart": True, "resumable_subagent": True, "subagent_type": "general-purpose"},
    }

    async def fake_get(_task_id):
        return record

    async def fake_update(_task_id, **fields):
        updates.append(fields)
        return True

    async def fake_messages(**_kwargs):
        return [{"role": "user", "content": "inspect the notes"}]

    async def fake_hydrate(runtime, **_kwargs):
        return runtime

    async def fake_spawn(runtime, _spec, _prompt, **_kwargs):
        spawned.append(runtime.model)
        return SimpleNamespace(
            result=SubagentResult(name="general-purpose", type="general-purpose", status="completed", content="ran")
        )

    monkeypatch.setattr(service, "get_runtime_task_record", fake_get)
    monkeypatch.setattr(service, "update_runtime_task_record", fake_update)
    monkeypatch.setattr(service, "_load_subagent_resume_messages", fake_messages)
    monkeypatch.setattr(service, "_hydrate_worker_runtime_context", fake_hydrate)

    def fake_completer(*_args, **_kwargs):
        async def _complete(_result):
            return None

        return _complete

    monkeypatch.setattr(service, "spawn_subagent", fake_spawn)
    monkeypatch.setattr(service, "make_run_completer", fake_completer)
    monkeypatch.setattr(service, "resolve_tenant_for_agent", lambda *_a, **_k: _async_value(agent.tenant_id))
    monkeypatch.setattr(service, "tenant_scoped_session", lambda *_a, **_k: _session_for(agent, models))

    await service.dispatch_persisted_subagent_run(run_id)

    assert spawned == []
    assert updates[0]["status"] == "failed"
    assert updates[0]["metadata_json"]["error_code"] == "primary_model_unavailable"


@pytest.mark.asyncio
async def test_spawn_tool_configured_primary_returns_without_provider(monkeypatch):
    from app.agents.subagent import SubagentResult
    from app.tools.handlers.subagent import spawn_subagent_tool
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    agent, models, _primary, _fallback = _configured_pair("disabled")
    spawned = []

    async def fake_spawn(ctx, _spec, _task, **_kwargs):
        spawned.append(ctx.model)
        return SimpleNamespace(
            result=SubagentResult(
                name="general-purpose",
                type="general-purpose",
                status="completed",
                content="ran",
                knowledge_provenance=None,
            )
        )

    monkeypatch.setattr(
        "app.tools.handlers.subagent.resolve_tenant_for_agent", lambda *_a, **_k: _async_value(agent.tenant_id)
    )
    monkeypatch.setattr(
        "app.tools.handlers.subagent.tenant_scoped_session", lambda *_a, **_k: _session_for(agent, models)
    )
    monkeypatch.setattr("app.tools.handlers.subagent.spawn_subagent", fake_spawn)
    monkeypatch.setattr("app.tools.handlers.subagent.memory_store_for_tenant", lambda *_a, **_k: None)
    monkeypatch.setattr("app.tools.handlers.subagent.memory_store_for_agent", lambda *_a, **_k: None)

    request = ToolExecutionRequest(
        tool_name="spawn_subagent",
        arguments={"task": "read the notes"},
        context=ToolExecutionContext(
            agent_id=agent.id,
            user_id=agent.owner_user_id,
            tenant_id=str(agent.tenant_id),
            workspace="/tmp",
            session_id=None,
        ),
    )
    payload = json.loads(await spawn_subagent_tool(request))

    assert spawned == []
    assert payload["ok"] is False
    assert payload["error"] == "No model or agent available for spawning a subagent"
    assert payload["error_code"] == "primary_model_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "missing", "cross_tenant"])
async def test_workflow_configured_primary_is_lookup_error(monkeypatch, kind):
    from app.services.workflow_launch import resolve_agent_runtime

    agent, models, _primary, _fallback = _configured_pair(kind)
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_a, **_k: _session_for(agent, models))

    with pytest.raises(LookupError, match="primary model is unavailable"):
        await resolve_agent_runtime(agent.id, tenant_id=agent.tenant_id)


@pytest.mark.asyncio
async def test_workflow_fallback_only_resolves(monkeypatch):
    from app.services.workflow_launch import resolve_agent_runtime

    tenant_id = uuid4()
    fallback = _model(tenant_id, name="fallback-only", enabled=True)
    agent = _agent(tenant_id, primary_model_id=None, fallback_model_id=fallback.id)
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_a, **_k: _session_for(agent, [fallback]))

    resolved_agent, resolved_model = await resolve_agent_runtime(agent.id, tenant_id=tenant_id)

    assert resolved_agent is agent
    assert resolved_model is fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "missing"])
async def test_resumable_delegation_configured_primary_needs_reconciliation(monkeypatch, kind):
    from app.agents.orchestrator import _build_delegation_request_from_runtime_record

    agent, models, _primary, _fallback = _configured_pair(kind)
    updates = []

    async def fake_update(_task_id, **fields):
        updates.append(fields)
        return True

    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_a, **_k: _session_for(agent, models))
    monkeypatch.setattr("app.agents.orchestrator.update_runtime_task_record", fake_update)
    record = {
        "task_id": uuid4().hex,
        "tenant_id": str(agent.tenant_id),
        "metadata": {
            "target_agent_id": str(agent.id),
            "owner_id": str(agent.owner_user_id),
            "tenant_id": str(agent.tenant_id),
            "conversation_messages": [{"role": "user", "content": "continue"}],
        },
    }

    request = await _build_delegation_request_from_runtime_record(record)

    assert request is None
    assert updates[-1]["status"] == "needs_reconciliation"
    assert updates[-1]["root_item_reason_code"] == "dispatch_request_unavailable"


@pytest.mark.asyncio
async def test_a2a_delegate_configured_primary_does_not_select_fallback(monkeypatch):
    from app.services.agent_tool_domains.messaging import _resolve_target_agent_runtime

    source_tenant = uuid4()
    source = _agent(source_tenant, name="Source")
    target, models, _primary, fallback = _configured_pair("disabled")
    target.tenant_id = source_tenant
    for model in models:
        if model.tenant_id != source_tenant or model is fallback or getattr(model, "enabled", True) is False:
            model.tenant_id = source_tenant
    session = CatalogSession(
        agents=[source, target],
        llm_models=models,
        users=[_user(source_tenant, source.owner_user_id)],
        participants=[],
    )

    async def allow_policy(*_args, **_kwargs):
        return SimpleNamespace(allowed=True, message="ok")

    monkeypatch.setattr(
        "app.services.agent_tool_domains.messaging.resolve_tenant_for_agent",
        lambda *_a, **_k: _async_value(source_tenant),
    )
    monkeypatch.setattr("app.services.agent_tool_domains.messaging.tenant_scoped_session", lambda *_a, **_k: session)
    monkeypatch.setattr("app.services.agent_tool_domains.messaging.resolve_a2a_collaboration_policy", allow_policy)

    _source, _target, model, error = await _resolve_target_agent_runtime(
        source.id,
        "",
        target_agent_id=target.id,
    )

    assert model is None
    assert error
    assert fallback.model not in error


@pytest.mark.asyncio
async def test_a2a_consult_configured_primary_does_not_invoke(monkeypatch):
    from app.services.agent_tool_domains.messaging import _send_message_to_agent_outcome

    tenant_id = uuid4()
    source = _agent(tenant_id, name="Source")
    target, models, _primary, _fallback = _configured_pair("missing")
    target.tenant_id = tenant_id
    target.name = "Target"
    for model in models:
        model.tenant_id = tenant_id
    invoked = []

    async def allow_policy(*_args, **_kwargs):
        return SimpleNamespace(allowed=True, message="ok", reason="allowed", group_id=None, group_name=None)

    async def fake_participant(*_args, **_kwargs):
        return uuid4()

    async def fake_session(*_args, **_kwargs):
        return SimpleNamespace(id=uuid4(), agent_id=source.id)

    async def fake_append(*_args, **_kwargs):
        invoked.append("append")

    async def fake_invoke(**kwargs):
        invoked.append(kwargs.get("target_model"))
        return SimpleNamespace(content="ran", failed=False, terminal_reason="turn_stop", parts=())

    monkeypatch.setattr(
        "app.services.agent_tool_domains.messaging.resolve_tenant_for_agent",
        lambda *_a, **_k: _async_value(tenant_id),
    )
    monkeypatch.setattr(
        "app.services.agent_tool_domains.messaging.tenant_scoped_session",
        lambda *_a, **_k: CatalogSession(agents=[source, target], llm_models=models, users=[], participants=[]),
    )
    monkeypatch.setattr("app.services.agent_tool_domains.messaging.resolve_a2a_collaboration_policy", allow_policy)
    monkeypatch.setattr("app.services.agent_tool_domains.messaging._invoke_agent_message_runtime", fake_invoke)
    monkeypatch.setattr("app.services.agent_pair_session.get_or_create_agent_participant_id", fake_participant)
    monkeypatch.setattr("app.services.agent_pair_session.find_or_create_agent_pair_session", fake_session)
    monkeypatch.setattr("app.services.chat_transcript.append_session_event", fake_append)

    outcome = await _send_message_to_agent_outcome(
        source.id,
        {"target_agent_id": str(target.id), "message": "hello"},
    )

    assert invoked == []
    assert outcome.ok is False
    assert outcome.error_code == "primary_model_unavailable"


@pytest.mark.asyncio
async def test_trigger_disabled_primary_and_pin_do_not_select_model():
    from app.services.trigger_preflight import select_trigger_model

    tenant_id = uuid4()
    primary = _model(tenant_id, name="primary-model", enabled=False)
    pinned = _model(tenant_id, name="pinned-model", enabled=False)
    agent = _agent(tenant_id, primary_model_id=primary.id)
    session = _session_for(agent, [primary, pinned])

    selected, _metadata, error = await select_trigger_model(session, agent, [])
    pin_selected, _pin_metadata, pin_error = await select_trigger_model(
        session,
        agent,
        [SimpleNamespace(config={"model_id": str(pinned.id)})],
    )

    assert selected is None and error == "model_not_found"
    assert pin_selected is None and pin_error == "model_pin_not_found"


@pytest.mark.asyncio
async def test_plan_mode_disabled_primary_does_not_invoke(monkeypatch):
    from app.services import plan_mode_system_run as mod

    agent, models, _primary, _fallback = _configured_pair("disabled")
    invoked = []
    plan = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        session_id="sess-plan",
        runtime_task_id=None,
        requested_by_user_id=agent.owner_user_id,
        intent_type="in_session_execution",
        original_request="draft the plan",
    )

    async def fake_invoke(_request):
        invoked.append(_request.model)
        return SimpleNamespace(content="ran")

    monkeypatch.setattr(mod, "resolve_tenant_for_agent", lambda *_a, **_k: _async_value(agent.tenant_id))
    monkeypatch.setattr(mod, "tenant_scoped_session", lambda *_a, **_k: _session_for(agent, models))
    monkeypatch.setattr(mod, "provision_agent_plan_file_slot", lambda *_a, **_k: None)
    monkeypatch.setattr("app.runtime.invoker.invoke_agent", fake_invoke)

    returned = await mod.launch_system_plan_run(plan)

    assert invoked == []
    assert returned is plan
    assert returned.status == "planning_failed"
    assert any("primary model is unavailable" in item for item in returned.metadata_json["planning_errors"])


@pytest.mark.asyncio
async def test_task_executor_disabled_primary_does_not_invoke(monkeypatch):
    from app.services import task_executor

    tenant_id = uuid4()
    primary = _model(tenant_id, name="primary-model", enabled=False)
    fallback = _model(tenant_id, name="fallback-model", enabled=True)
    agent = _agent(tenant_id, primary_model_id=primary.id, fallback_model_id=fallback.id)
    task = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=tenant_id,
        title="file the note",
        description="",
        type="todo",
        status="pending",
        created_by=agent.owner_user_id,
        plan_authorization=None,
        last_execution_status=None,
    )
    invoked = []

    async def fake_invoke(request):
        invoked.append({"model": request.model, "fallback": request.fallback_model})
        return SimpleNamespace(content="ran", terminal_reason=SimpleNamespace(value="turn_abort"))

    async def allow_plan(*_args, **_kwargs):
        return True, None

    monkeypatch.setattr(task_executor, "resolve_tenant_for_agent", lambda *_a, **_k: _async_value(tenant_id))
    monkeypatch.setattr(
        task_executor,
        "tenant_scoped_session",
        lambda *_a, **_k: _session_for(agent, [primary, fallback], tasks=[task]),
    )
    monkeypatch.setattr(task_executor, "_task_plan_gate_allows", allow_plan)
    monkeypatch.setattr(task_executor, "_append_task_session_event", lambda *_a, **_k: _async_true())
    monkeypatch.setattr(task_executor, "invoke_agent", fake_invoke)

    outcome = await task_executor.execute_task(task.id, agent.id, requester_user_id=agent.owner_user_id)

    assert invoked == []
    assert outcome.error_code == "model_not_found"


@pytest.mark.asyncio
async def test_task_executor_disabled_fallback_is_not_passed(monkeypatch):
    from app.services import task_executor

    tenant_id = uuid4()
    primary = _model(tenant_id, name="primary-model", enabled=True)
    fallback = _model(tenant_id, name="fallback-model", enabled=False)
    agent = _agent(tenant_id, primary_model_id=primary.id, fallback_model_id=fallback.id, role_description="role")
    task = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=tenant_id,
        title="file the note",
        description="",
        type="todo",
        status="pending",
        created_by=agent.owner_user_id,
        plan_authorization=None,
        last_execution_status=None,
    )
    invoked = []

    async def fake_invoke(request):
        invoked.append({"model": request.model, "fallback": request.fallback_model})
        from app.kernel.contracts import TerminalReason

        return SimpleNamespace(
            content="ran",
            terminal_reason=TerminalReason.TURN_STOP,
            failure_code=None,
            response_complete_payload={"ok": True},
        )

    async def allow_plan(*_args, **_kwargs):
        return True, None

    monkeypatch.setattr(task_executor, "resolve_tenant_for_agent", lambda *_a, **_k: _async_value(tenant_id))
    monkeypatch.setattr(
        task_executor,
        "tenant_scoped_session",
        lambda *_a, **_k: _session_for(agent, [primary, fallback], tasks=[task]),
    )
    monkeypatch.setattr(task_executor, "_task_plan_gate_allows", allow_plan)
    monkeypatch.setattr(task_executor, "_append_task_session_event", lambda *_a, **_k: _async_true())
    monkeypatch.setattr(task_executor, "invoke_agent", fake_invoke)
    monkeypatch.setattr("app.services.activity_logger.log_activity", lambda *_a, **_k: _async_true())

    outcome = await task_executor.execute_task(task.id, agent.id, requester_user_id=agent.owner_user_id)

    assert invoked == [{"model": primary, "fallback": None}]
    assert outcome.error_code is None


class _PersistedPlans:
    """Separate the caller's draft object from the row a later read returns."""

    def __init__(self):
        self.persisted: dict = {}
        self.marked: list = []

    async def create_plan_request(self, **kwargs):
        self.persisted = {"id": uuid4(), "status": "draft", "metadata_json": {}, "plan_hash": None, **kwargs}
        return SimpleNamespace(**self.persisted)

    async def get_plan(self, _plan_id):
        data = self.persisted
        return SimpleNamespace(
            id=data["id"],
            status=data["status"],
            metadata_json=dict(data["metadata_json"]),
            plan_hash=data["plan_hash"],
        )

    async def mark_planning_failed_if_unauthored(self, plan_id, errors):
        self.marked.append((plan_id, list(errors)))
        if self.persisted["status"] in ("draft", "planning"):
            self.persisted["status"] = "planning_failed"
            self.persisted["plan_hash"] = None
            metadata = dict(self.persisted["metadata_json"])
            metadata["planning_errors"] = list(errors)
            self.persisted["metadata_json"] = metadata


@pytest.mark.asyncio
async def test_channel_plan_disabled_primary_is_failed_and_not_confirmable(monkeypatch):
    from app.services import plan_mode_core, plan_mode_system_run as plans
    from app.services.channel_agent_runtime import call_agent_llm

    agent, models, _primary, _fallback = _configured_pair("disabled")
    service = _PersistedPlans()
    invoked = []
    decision = plan_mode_core.PlanModeEntryDecision(
        mode="explicit",
        intent_type="autonomous_wake",
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        title="owner requested plan",
        reason="explicit",
    )

    async def invoke(_request):
        invoked.append(_request.model)
        return SimpleNamespace(content="planned")

    monkeypatch.setattr(plan_mode_core, "classify_plan_mode_entry", lambda _text: decision)
    monkeypatch.setattr("app.services.plan_mode_service.get_plan_mode_service", lambda: service)
    monkeypatch.setattr(plans, "resolve_tenant_for_agent", lambda *_a, **_k: _async_value(agent.tenant_id))
    monkeypatch.setattr(plans, "tenant_scoped_session", lambda *_a, **_k: _session_for(agent, models))
    monkeypatch.setattr(plans, "provision_agent_plan_file_slot", lambda *_a, **_k: None)
    monkeypatch.setattr("app.runtime.invoker.invoke_agent", invoke)

    reply = await call_agent_llm(_session_for(agent, models), agent.id, "进入计划模式", session_id=None)
    stored = await service.get_plan(service.persisted["id"])

    assert invoked == []
    assert service.marked
    assert stored.status == "planning_failed"
    assert any("primary model is unavailable" in item for item in stored.metadata_json["planning_errors"])
    assert "待确认计划" not in reply
    assert "不能确认" in reply
    assert str(stored.id) in reply


def _spawn_request(agent, arguments):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="spawn_subagent",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=agent.id,
            user_id=agent.owner_user_id,
            tenant_id=str(agent.tenant_id),
            workspace="/tmp",
            session_id=None,
        ),
    )


def _patch_spawn(monkeypatch, agent, models, spawned):
    from app.agents.subagent import SubagentResult

    async def fake_spawn(ctx, spec, _task, **_kwargs):
        spawned.append({"model": ctx.model, "fallback": ctx.fallback_model, "spec_model": spec.model})
        return SimpleNamespace(
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="checked")
        )

    monkeypatch.setattr(
        "app.tools.handlers.subagent.resolve_tenant_for_agent", lambda *_a, **_k: _async_value(agent.tenant_id)
    )
    monkeypatch.setattr(
        "app.tools.handlers.subagent.tenant_scoped_session", lambda *_a, **_k: _session_for(agent, models)
    )
    monkeypatch.setattr("app.tools.handlers.subagent.spawn_subagent", fake_spawn)
    monkeypatch.setattr("app.tools.handlers.subagent.memory_store_for_tenant", lambda *_a, **_k: None)
    monkeypatch.setattr("app.tools.handlers.subagent.memory_store_for_agent", lambda *_a, **_k: None)


@pytest.mark.asyncio
async def test_explicit_child_model_spawns_when_parent_primary_disabled(monkeypatch):
    from app.tools.handlers.subagent import spawn_subagent_tool

    agent, models, _primary, fallback = _configured_pair("disabled")
    chosen = _model(agent.tenant_id, name="explicit-child", enabled=True)
    spawned = []
    _patch_spawn(monkeypatch, agent, [*models, chosen], spawned)

    payload = json.loads(
        await spawn_subagent_tool(_spawn_request(agent, {"task": "read notes", "model": str(chosen.id)}))
    )

    assert payload["ok"] is True
    assert spawned == [{"model": chosen, "fallback": None, "spec_model": str(chosen.id)}]
    assert spawned[0]["model"] is not fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "cross_tenant"])
async def test_unusable_explicit_child_model_does_not_spawn(monkeypatch, kind):
    from app.tools.handlers.subagent import spawn_subagent_tool

    agent, models, _primary, _fallback = _configured_pair("disabled")
    chosen = _model(agent.tenant_id, name="blocked-child", enabled=kind != "disabled")
    if kind == "cross_tenant":
        chosen.tenant_id = uuid4()
    spawned = []
    _patch_spawn(monkeypatch, agent, [*models, chosen], spawned)

    payload = json.loads(
        await spawn_subagent_tool(_spawn_request(agent, {"task": "read notes", "model": str(chosen.id)}))
    )

    assert spawned == []
    assert payload["ok"] is False
    assert payload["error_code"] == "child_model_unavailable"
    assert "fallback-model" not in payload["error"]


@pytest.mark.asyncio
async def test_inherit_child_model_keeps_parent_primary_failure(monkeypatch):
    from app.tools.handlers.subagent import spawn_subagent_tool

    agent, models, _primary, _fallback = _configured_pair("disabled")
    spawned = []
    _patch_spawn(monkeypatch, agent, models, spawned)

    payload = json.loads(await spawn_subagent_tool(_spawn_request(agent, {"task": "read notes", "model": "inherit"})))

    assert spawned == []
    assert payload["ok"] is False
    assert payload["error_code"] == "primary_model_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("definition_model", ["selected", "inherit"])
async def test_definition_model_follows_child_override_rules(monkeypatch, definition_model):
    from app.agents.subagent import SubagentSpec
    from app.agents.subagent_definition import SCOPE_AGENT, ResolvedSubagentDefinition
    from app.tools.handlers.subagent import spawn_subagent_tool

    agent, models, _primary, _fallback = _configured_pair("disabled")
    chosen = _model(agent.tenant_id, name="definition-child", enabled=True)
    spawned = []
    _patch_spawn(monkeypatch, agent, [*models, chosen], spawned)
    model_value = str(chosen.id) if definition_model == "selected" else "inherit"

    def fake_resolve(name, **_kwargs):
        return ResolvedSubagentDefinition(
            spec=SubagentSpec(name=name, type="general-purpose", model=model_value),
            scope=SCOPE_AGENT,
        )

    monkeypatch.setattr("app.tools.handlers.subagent.resolve_runtime_subagent_definition", fake_resolve)
    payload = json.loads(
        await spawn_subagent_tool(_spawn_request(agent, {"task": "read notes", "definition_name": "notes"}))
    )

    if definition_model == "selected":
        assert payload["ok"] is True
        assert spawned == [{"model": chosen, "fallback": None, "spec_model": str(chosen.id)}]
    else:
        assert spawned == []
        assert payload["error_code"] == "primary_model_unavailable"


async def _dispatch_recorded_child(monkeypatch, child_kind: str):
    from app.agents.subagent import SubagentResult
    from app.services import subagent_run_service as service

    agent, models, _primary, fallback = _configured_pair("disabled")
    if child_kind == "cross_tenant":
        child = _model(uuid4(), name="persisted-child", enabled=True)
    else:
        child = _model(agent.tenant_id, name="persisted-child", enabled=child_kind == "enabled")
    spawned = []
    updates = []
    record = {
        "task_id": uuid4().hex,
        "task_type": "subagent",
        "status": "pending",
        "parent_agent_id": str(agent.id),
        "root_user_id": str(uuid4()),
        "prompt": "inspect the notes",
        "metadata": {
            "resume_after_restart": True,
            "resumable_subagent": True,
            "subagent_type": "general-purpose",
            "subagent_spec": {"name": "notes", "type": "general-purpose", "model": str(child.id)},
        },
    }

    async def fake_update(_task_id, **fields):
        updates.append(fields)
        return True

    async def fake_spawn(runtime, spec, _prompt, **_kwargs):
        spawned.append({"model": runtime.model, "fallback": runtime.fallback_model, "spec_model": spec.model})
        return SimpleNamespace(result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ran"))

    monkeypatch.setattr(service, "get_runtime_task_record", lambda _task_id: _async_value(record))
    monkeypatch.setattr(service, "update_runtime_task_record", fake_update)
    monkeypatch.setattr(
        service,
        "_load_subagent_resume_messages",
        lambda **_kwargs: _async_value([{"role": "user", "content": "inspect"}]),
    )
    monkeypatch.setattr(service, "_hydrate_worker_runtime_context", lambda runtime, **_kwargs: _async_value(runtime))
    monkeypatch.setattr(service, "spawn_subagent", fake_spawn)
    monkeypatch.setattr(service, "make_run_completer", lambda *_a, **_k: lambda _result: _async_true())
    monkeypatch.setattr(service, "resolve_tenant_for_agent", lambda *_a, **_k: _async_value(agent.tenant_id))
    monkeypatch.setattr(service, "tenant_scoped_session", lambda *_a, **_k: _session_for(agent, [*models, child]))
    await service.dispatch_persisted_subagent_run(record["task_id"])
    return spawned, updates, child, fallback


@pytest.mark.asyncio
async def test_persisted_child_override_dispatches_when_parent_primary_disabled(monkeypatch):
    spawned, updates, child, fallback = await _dispatch_recorded_child(monkeypatch, "enabled")

    assert spawned == [{"model": child, "fallback": None, "spec_model": str(child.id)}]
    assert spawned[0]["model"] is not fallback
    assert all(item.get("metadata_json", {}).get("error_code") != "primary_model_unavailable" for item in updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("child_kind", ["disabled", "cross_tenant"])
async def test_persisted_unusable_child_override_does_not_dispatch(monkeypatch, child_kind):
    spawned, updates, _child, _fallback = await _dispatch_recorded_child(monkeypatch, child_kind)

    assert spawned == []
    assert updates[0]["status"] == "failed"
    assert updates[0]["metadata_json"]["error_code"] == "child_model_unavailable"
