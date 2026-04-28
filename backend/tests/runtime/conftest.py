"""Shared fixtures for runtime test module.

P0-1b autouse fixture: invoker._resolve_runtime_config now sets a
tenant_resolution_error sentinel on every fallback path (no agent_id /
agent not found / DB exception) and kernel.engine aborts the invocation
when this sentinel is present. Most invoke_agent integration tests do not
mock the database, so the call previously ran into a real PostgreSQL
connection failure and silently fell back to tenant_id=None — letting the
test continue. Now it correctly aborts.

This fixture provides a default RuntimeConfig with a real tenant_id for
all runtime/* tests so the tenant-resolution path doesn't accidentally
abort tests that target unrelated invoke_agent behaviour. Tests that
specifically exercise tenant resolution can opt out via monkeypatch
themselves (see test_resolve_runtime_config_*).
"""
from __future__ import annotations

import uuid

import pytest

from app.kernel.contracts import RuntimeConfig


@pytest.fixture(autouse=True)
def _default_runtime_config(monkeypatch, request):
    """Stub DB-touching invoker helpers for invoke_agent integration tests.

    Why each is stubbed:
      * _resolve_runtime_config — real version queries Agent table; fallback
        now sets tenant_resolution_error sentinel (P0-1b) which would abort
        every test that doesn't explicitly mock the DB.
      * _resolve_retrieval_context — runs build_agent_runtime_context which
        calls get_agent_timezone (real DB query). Returning empty string keeps
        the prompt assembly path exercisable without DB.
      * _resolve_current_user_name — queries User table; tests that care
        about display name supply their own monkeypatch.

    Opt out with @pytest.mark.real_runtime_config when a test specifically
    targets these resolvers.
    """
    if request.node.get_closest_marker("real_runtime_config"):
        return

    async def _stub_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid.uuid4(), max_tool_rounds=200)

    async def _stub_current_user_name(_user_id):
        return None

    async def _stub_agent_runtime_context(*_args, **_kwargs):
        # The real builder calls get_agent_timezone which queries the Agent
        # table — return empty so _resolve_retrieval_context keeps running
        # (so tests that mock fetch_relevant_knowledge / build_memory_context
        # individually still see their stubs flow through).
        return ""

    monkeypatch.setattr("app.runtime.invoker._resolve_runtime_config", _stub_runtime_config)
    monkeypatch.setattr("app.runtime.invoker._resolve_current_user_name", _stub_current_user_name)
    monkeypatch.setattr("app.runtime.invoker.build_agent_runtime_context", _stub_agent_runtime_context)
