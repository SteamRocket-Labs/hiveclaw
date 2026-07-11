"""B3 — trigger ``delivery`` field: schema + service/runtime passthrough.

``delivery=same_session`` opts a trigger into CC first-gen ``/loop`` "inject into
current session" semantics; ``new_invocation`` (default) keeps every existing
trigger byte-for-byte unchanged. This covers the plumbing that carries the field
from the ``set_trigger`` tool through to the stored trigger config, plus the
runtime injection that supplies the source session so the model never restates
a session id.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


# ── pure normalization helper ───────────────────────────────────────────


def test_apply_trigger_delivery_same_session_with_source():
    from app.services.agent_tool_domains.triggers import _apply_trigger_delivery

    config = {"minutes": 5}
    sid = str(uuid4())
    error = _apply_trigger_delivery("set_trigger", config, {"delivery": "same_session", "source_session_id": sid})

    assert error is None
    assert config["delivery"] == "same_session"
    assert config["source_session_id"] == sid


def test_apply_trigger_delivery_same_session_reads_source_from_config():
    from app.services.agent_tool_domains.triggers import _apply_trigger_delivery

    sid = str(uuid4())
    config = {"minutes": 5, "delivery": "same_session", "source_session_id": sid}
    error = _apply_trigger_delivery("set_trigger", config, {})

    assert error is None
    assert config["source_session_id"] == sid


def test_apply_trigger_delivery_same_session_without_source_is_error():
    from app.services.agent_tool_domains.triggers import _apply_trigger_delivery

    config = {"minutes": 5}
    error = _apply_trigger_delivery("set_trigger", config, {"delivery": "same_session"})

    assert error is not None
    assert "same_session" in error
    assert "delivery" not in config  # rejected → config untouched


def test_apply_trigger_delivery_default_leaves_config_untouched():
    from app.services.agent_tool_domains.triggers import _apply_trigger_delivery

    config = {"minutes": 5}
    error = _apply_trigger_delivery("set_trigger", config, {})

    assert error is None
    assert "delivery" not in config  # default new_invocation stays implicit


def test_apply_trigger_delivery_explicit_new_invocation_clears_source():
    from app.services.agent_tool_domains.triggers import _apply_trigger_delivery

    config = {"minutes": 5, "source_session_id": str(uuid4())}
    error = _apply_trigger_delivery("set_trigger", config, {"delivery": "new_invocation"})

    assert error is None
    assert config["delivery"] == "new_invocation"
    assert "source_session_id" not in config


def test_apply_trigger_delivery_invalid_value_is_error():
    from app.services.agent_tool_domains.triggers import _apply_trigger_delivery

    config = {"minutes": 5}
    error = _apply_trigger_delivery("set_trigger", config, {"delivery": "broadcast"})

    assert error is not None
    assert "broadcast" in error


# ── runtime injection: supply the source session, never trust the model ──


def test_inject_runtime_context_supplies_source_session_for_same_session():
    from app.tools.service import _inject_runtime_context_arguments

    sid = str(uuid4())
    ctx = SimpleNamespace(session_id=sid, budget_run_id=None, permission_profile=None)
    enriched = _inject_runtime_context_arguments(
        "set_trigger", {"name": "loop", "type": "interval", "delivery": "same_session"}, ctx
    )

    assert enriched["source_session_id"] == sid


def test_inject_runtime_context_no_injection_for_default_delivery():
    from app.tools.service import _inject_runtime_context_arguments

    ctx = SimpleNamespace(session_id=str(uuid4()), budget_run_id=None, permission_profile=None)
    enriched = _inject_runtime_context_arguments("set_trigger", {"name": "cron", "type": "cron"}, ctx)

    assert "source_session_id" not in enriched


def test_inject_runtime_context_overwrites_model_supplied_source_session():
    from app.tools.service import _inject_runtime_context_arguments

    explicit = str(uuid4())
    ctx = SimpleNamespace(session_id=str(uuid4()), budget_run_id=None, permission_profile=None)
    enriched = _inject_runtime_context_arguments(
        "set_trigger",
        {"delivery": "same_session", "source_session_id": explicit},
        ctx,
    )

    assert enriched["source_session_id"] == ctx.session_id
    assert enriched["source_session_id"] != explicit


# ── schema surface ───────────────────────────────────────────────────────


def test_set_trigger_schema_documents_delivery():
    from app.tools.handlers.triggers import set_trigger

    meta = set_trigger.tool_meta
    config_desc = meta.parameters["properties"]["config"]["description"]
    assert "delivery" in config_desc
    assert "same_session" in config_desc


# ── full-handler wiring: delivery lands in the persisted trigger config ──


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _FakeScopedSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt):
        if "app.current_tenant_id" in str(stmt):
            return _Result(None)
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_handle_set_trigger_persists_same_session_delivery(monkeypatch):
    import app.services.agent_tool_domains.triggers as triggers_domain
    from app.models.trigger import AgentTrigger

    agent_id = uuid4()
    user_id = uuid4()
    session_id = str(uuid4())
    agent_obj = SimpleNamespace(id=agent_id, max_triggers=None)

    session = _FakeScopedSession([_Result(agent_obj), _Result(0), _Result(None)])
    monkeypatch.setattr(triggers_domain, "tenant_scoped_session", lambda *a, **k: session)

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return None

    monkeypatch.setattr(triggers_domain, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(triggers_domain, "_capture_reply_context", lambda: None)

    async def _noop_audit(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", _noop_audit)

    result = await triggers_domain._handle_set_trigger(
        agent_id,
        {
            "name": "loop_check_deploy",
            "type": "interval",
            "config": {"minutes": 5},
            "reason": "Check the deploy status.",
            "delivery": "same_session",
            "source_session_id": session_id,
        },
        user_id=user_id,
        session_id=session_id,
    )

    assert "✅" in result
    stored = next(item for item in session.added if isinstance(item, AgentTrigger))
    assert stored.config["delivery"] == "same_session"
    assert stored.config["source_session_id"] == session_id
    assert stored.config["created_by"] == str(user_id)
    assert stored.config["root_session_id"] == session_id
    assert stored.type == "interval"


@pytest.mark.asyncio
async def test_handle_set_trigger_default_delivery_is_unmarked(monkeypatch):
    import app.services.agent_tool_domains.triggers as triggers_domain
    from app.models.trigger import AgentTrigger

    agent_id = uuid4()
    agent_obj = SimpleNamespace(id=agent_id, max_triggers=None)

    session = _FakeScopedSession([_Result(agent_obj), _Result(0), _Result(None)])
    monkeypatch.setattr(triggers_domain, "tenant_scoped_session", lambda *a, **k: session)

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return None

    monkeypatch.setattr(triggers_domain, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(triggers_domain, "_capture_reply_context", lambda: None)

    async def _noop_audit(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", _noop_audit)

    await triggers_domain._handle_set_trigger(
        agent_id,
        {
            "name": "daily_cron",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": "Daily report.",
        },
        user_id=uuid4(),
    )

    stored = next(item for item in session.added if isinstance(item, AgentTrigger))
    assert "delivery" not in stored.config  # default path is byte-for-byte unchanged


@pytest.mark.asyncio
async def test_handle_set_trigger_fails_closed_without_runtime_requester(monkeypatch):
    import app.services.agent_tool_domains.triggers as triggers_domain

    async def _must_not_resolve(*_args, **_kwargs):
        raise AssertionError("unauthenticated trigger creation must stop before persistence")

    monkeypatch.setattr(triggers_domain, "resolve_tenant_for_agent", _must_not_resolve)

    result = await triggers_domain._handle_set_trigger(
        uuid4(),
        {
            "name": "unowned",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": "must not be created",
        },
    )

    assert "auth_or_permission" in result
