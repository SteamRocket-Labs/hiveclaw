"""Resolver-side governance hook pins: sandbox verdict translation (D1/D2) and
the platform force switches (CC managed-policy parity)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.tools.governance_resolver import ToolGovernanceResolver, run_sandboxed_governance_hook
from app.tools.hook_governance import GovernanceHookSpec


def _command_spec(**overrides) -> GovernanceHookSpec:
    base = dict(
        key="acme/check",
        layer="company",
        kind="command",
        matcher="run_command",
        decision=None,
        reason="tenant compliance script",
        arg_rules=(),
        command="python check.py",
        timeout_seconds=5,
        enabled=True,
    )
    base.update(overrides)
    return GovernanceHookSpec(**base)


def _payload() -> dict:
    return {
        "event": "PreToolUse",
        "hook_key": "acme/check",
        "tool_name": "run_command",
        "tool_args": {"command": "ls"},
        "agent_id": str(uuid.uuid4()),
        "session_id": "session-1",
        "tenant_id": str(uuid.uuid4()),
        "turn_id": None,
        "tool_call_id": None,
    }


def _exec_result(**overrides):
    base = dict(stdout="", stderr="", exit_code=0, timed_out=False, error=None, evidence={})
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Patch the code-execution provider and the workspace resolver."""
    calls: dict = {"argv": None, "kwargs": None, "result": _exec_result()}

    async def fake_execute(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return calls["result"]

    async def fake_workspace(_agent_id, _tenant_id=None):
        return tmp_path

    monkeypatch.setattr("app.services.code_execution.service.execute_agent_command", fake_execute)
    monkeypatch.setattr("app.tools.workspace.ensure_workspace", fake_workspace)
    return calls


@pytest.mark.asyncio
async def test_json_verdict_translates(sandbox):
    sandbox["result"] = _exec_result(stdout=json.dumps({"decision": "ask", "reason": "needs review"}))
    verdict = await run_sandboxed_governance_hook(_command_spec(), _payload())
    assert verdict.decision == "ask"
    assert verdict.reason == "needs review"
    assert verdict.source == "command"
    # D2: the command ran through the sandbox provider with stdin redirection.
    assert sandbox["argv"][0] == "bash"
    assert "python check.py <" in sandbox["argv"][2]
    assert sandbox["kwargs"]["network_policy"] == "deny"
    assert sandbox["kwargs"]["env"] == {}


@pytest.mark.asyncio
async def test_exit_two_is_deny_with_output_reason(sandbox):
    sandbox["result"] = _exec_result(stdout="policy violated", exit_code=2)
    verdict = await run_sandboxed_governance_hook(_command_spec(), _payload())
    assert verdict.decision == "deny"
    assert "policy violated" in verdict.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result_kwargs",
    [
        {"error": "provider unavailable"},
        {"timed_out": True},
        {"exit_code": 1},
        {"stdout": "{not json", "exit_code": 0},
        {"stdout": json.dumps({"decision": "warp"}), "exit_code": 0},
    ],
)
async def test_runtime_failures_fail_closed(sandbox, result_kwargs):
    """D1: crash / timeout / unknown exit / malformed protocol all deny."""
    sandbox["result"] = _exec_result(**result_kwargs)
    verdict = await run_sandboxed_governance_hook(_command_spec(), _payload())
    assert verdict.decision == "deny"
    assert verdict.source == "failure"


@pytest.mark.asyncio
async def test_clean_exit_without_json_is_no_opinion(sandbox):
    sandbox["result"] = _exec_result(stdout="checked 3 rules, all fine")
    verdict = await run_sandboxed_governance_hook(_command_spec(), _payload())
    assert verdict.decision == "no_opinion"


@pytest.mark.asyncio
async def test_loader_disabled_by_platform_switch(monkeypatch):
    monkeypatch.setenv("HIVE_DISABLE_ALL_GOVERNANCE_HOOKS", "1")
    resolver = ToolGovernanceResolver()
    deps = resolver.build_dependencies()
    specs = await deps.load_governance_hooks(str(uuid.uuid4()), uuid.uuid4(), "write_file")
    assert specs == []


@pytest.mark.asyncio
async def test_loader_filters_to_managed_when_forced(monkeypatch):
    monkeypatch.setenv("HIVE_ALLOW_MANAGED_HOOKS_ONLY", "1")
    resolver = ToolGovernanceResolver()

    rows = [
        SimpleNamespace(
            qualified_name="managed/rule",
            event="PreToolUse",
            handler="declarative:policy",
            mode="deny",
            status="approved",
            matcher_json={"layer": "managed", "matcher": "*", "decision": "deny", "reason": "m"},
        ),
        SimpleNamespace(
            qualified_name="company/rule",
            event="PreToolUse",
            handler="declarative:policy",
            mode="ask",
            status="approved",
            matcher_json={"layer": "company", "matcher": "*", "decision": "ask", "reason": "c"},
        ),
    ]

    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: rows)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, _stmt):
            return _Result()

    monkeypatch.setattr(
        "app.tools.governance_resolver.tenant_scoped_session", lambda _tenant: _Session()
    )
    deps = resolver.build_dependencies()
    specs = await deps.load_governance_hooks(str(uuid.uuid4()), uuid.uuid4(), "write_file")
    assert [spec.key for spec in specs] == ["managed/rule"]
