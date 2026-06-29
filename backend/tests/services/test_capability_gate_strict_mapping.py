"""P1-W2-8 — capability mapping fail-closed mode + drift counter.

Two behaviours pinned:
  1. Unmapped tool calls always increment a per-tool counter so operators
     can see drift before deciding to flip strict mode on.
  2. Under STRICT_CAPABILITY_MAPPING, an unmapped tool is denied; explicit
     lenient mode still logs + allows for local compatibility.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import capability_gate


@pytest.fixture(autouse=True)
def _reset_unmapped_counter():
    capability_gate.reset_unmapped_tool_counts()
    yield
    capability_gate.reset_unmapped_tool_counts()


# ── Counter ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unmapped_tool_increments_counter(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "STRICT_CAPABILITY_MAPPING", False, raising=False)

    result = await capability_gate.check_capability(
        db=None,  # not touched on the unmapped path
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_name="never_seen_tool",
    )

    assert result.allowed is True  # lenient
    counts = capability_gate.get_unmapped_tool_counts()
    assert counts["never_seen_tool"] == 1


@pytest.mark.asyncio
async def test_repeated_unmapped_calls_accumulate(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "STRICT_CAPABILITY_MAPPING", False, raising=False)

    for _ in range(3):
        await capability_gate.check_capability(
            db=None,
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            tool_name="leaky_tool",
        )

    assert capability_gate.get_unmapped_tool_counts()["leaky_tool"] == 3


@pytest.mark.asyncio
async def test_mapped_tool_does_not_touch_counter(monkeypatch) -> None:
    """Sanity: a known tool must not bump the unmapped counter even if
    the policy lookup later allows or denies."""

    async def fake_execute(_stmt):
        class _Result:
            def scalar_one_or_none(self):
                return None

        return _Result()

    fake_db = type("FakeDB", (), {"execute": staticmethod(fake_execute)})()

    await capability_gate.check_capability(
        db=fake_db,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_name="read_file",  # in CAPABILITY_MAP
    )

    assert capability_gate.get_unmapped_tool_counts() == {}


@pytest.mark.asyncio
async def test_mapped_tool_without_policy_or_tool_enablement_source_escalates() -> None:
    """Mapped capabilities without policy or tool-toggle state fall back to session permission."""

    async def fake_execute(_stmt):
        class _Result:
            def scalar_one_or_none(self):
                return None

        return _Result()

    fake_db = type("FakeDB", (), {"execute": staticmethod(fake_execute)})()

    result = await capability_gate.check_capability(
        db=fake_db,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_name="write_file",
    )

    assert result.allowed is False
    assert result.denied is False
    assert result.escalate_to_l3 is True
    assert result.capability == "workspace.file.write"
    assert result.policy_found is False
    assert "no capability policy" in result.reason


@pytest.mark.asyncio
async def test_enabled_workspace_tool_config_allows_mapped_missing_policy() -> None:
    """The enterprise Tools page is the product-facing policy source.

    If a tenant has enabled a tool there, runtime must not reject the call just
    because no separate CapabilityPolicy row exists.
    """
    tool_id = uuid.uuid4()
    result = await capability_gate.check_capability(
        db=_QueuedScalarDB(
            [
                None,  # no agent-specific CapabilityPolicy
                None,  # no tenant-default CapabilityPolicy
                SimpleNamespace(id=tool_id, enabled=True, tenant_id=None),  # global Tool row
                SimpleNamespace(enabled=True),  # TenantToolConfig row from the backend toggle
            ]
        ),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_name="send_email",
    )

    assert result.allowed is True
    assert result.denied is False
    assert result.escalate_to_l3 is False
    assert result.capability == "channel.email.send"
    assert result.policy_found is False


@pytest.mark.asyncio
async def test_disabled_workspace_tool_config_denies_mapped_missing_policy() -> None:
    tool_id = uuid.uuid4()
    result = await capability_gate.check_capability(
        db=_QueuedScalarDB(
            [
                None,
                None,
                SimpleNamespace(id=tool_id, enabled=True, tenant_id=None),
                SimpleNamespace(enabled=False),
            ]
        ),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_name="send_email",
    )

    assert result.allowed is False
    assert result.denied is True
    assert result.escalate_to_l3 is False
    assert result.capability == "channel.email.send"
    assert result.policy_found is False
    assert "disabled" in result.reason


@pytest.mark.asyncio
async def test_explicit_capability_policy_deny_overrides_enabled_tool_config() -> None:
    result = await capability_gate.check_capability(
        db=_QueuedScalarDB([SimpleNamespace(allowed=False, requires_approval=False)]),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_name="send_email",
    )

    assert result.allowed is False
    assert result.denied is True
    assert result.capability == "channel.email.send"
    assert result.policy_found is True


# ── Strict mode ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strict_mode_denies_unmapped_tool(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "STRICT_CAPABILITY_MAPPING", True, raising=False)

    result = await capability_gate.check_capability(
        db=None,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_name="rogue_tool",
    )

    assert result.allowed is False
    assert result.denied is True
    assert "missing from CAPABILITY_MAP" in result.reason
    # Counter still bumped — operators want visibility under both modes.
    assert capability_gate.get_unmapped_tool_counts()["rogue_tool"] == 1


@pytest.mark.asyncio
async def test_lenient_mode_allows_unmapped_tool(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "STRICT_CAPABILITY_MAPPING", False, raising=False)

    result = await capability_gate.check_capability(
        db=None,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_name="rogue_tool",
    )

    assert result.allowed is True
    assert result.denied is False


def test_settings_default_is_strict() -> None:
    """Production default is fail-closed; lenient mode must be explicit."""
    from app.config import get_settings

    assert get_settings().STRICT_CAPABILITY_MAPPING is True


# ── Company HR system-agent default lane ───────────────────────


class _QueuedScalarDB:
    def __init__(self, values) -> None:
        self.values = list(values)

    async def execute(self, _stmt):
        value = self.values.pop(0)

        class _Result:
            def scalar_one_or_none(self):
                return value

        return _Result()


@pytest.mark.asyncio
async def test_company_hr_agent_create_employee_missing_policy_is_allowed_by_platform_default() -> None:
    """The company HR system agent is the platform's built-in hiring lane.

    A company/workspace with no explicit CapabilityPolicy rows must still be able to
    use create_digital_employee; otherwise every new company loses HR hiring.
    """
    from types import SimpleNamespace

    tenant_id = uuid.uuid4()
    result = await capability_gate.check_capability(
        db=_QueuedScalarDB(
            [
                None,  # no agent-specific CapabilityPolicy
                None,  # no company-default CapabilityPolicy
                SimpleNamespace(name="__system_hr__", agent_class="internal_system", tenant_id=tenant_id),
            ]
        ),
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        tool_name="create_digital_employee",
    )

    assert result.allowed is True
    assert result.denied is False
    assert result.escalate_to_l3 is False
    assert result.capability == "agent.employee.create"
    assert result.policy_found is False


@pytest.mark.asyncio
async def test_non_hr_agent_create_employee_missing_policy_still_escalates() -> None:
    from types import SimpleNamespace

    tenant_id = uuid.uuid4()
    result = await capability_gate.check_capability(
        db=_QueuedScalarDB(
            [
                None,
                None,
                SimpleNamespace(name="regular-agent", agent_class="internal_tenant", tenant_id=tenant_id),
            ]
        ),
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        tool_name="create_digital_employee",
    )

    assert result.allowed is False
    assert result.denied is False
    assert result.escalate_to_l3 is True
    assert result.capability == "agent.employee.create"
    assert result.policy_found is False


@pytest.mark.asyncio
async def test_create_employee_missing_policy_does_not_allow_without_company_hr_agent_row() -> None:
    tenant_id = uuid.uuid4()
    result = await capability_gate.check_capability(
        db=_QueuedScalarDB(
            [
                None,  # no agent-specific CapabilityPolicy
                None,  # no company-default CapabilityPolicy
                None,  # agent is not the current company's __system_hr__
            ]
        ),
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        tool_name="create_digital_employee",
    )

    assert result.allowed is False
    assert result.denied is False
    assert result.escalate_to_l3 is True
    assert result.capability == "agent.employee.create"
    assert result.policy_found is False


@pytest.mark.asyncio
async def test_hr_agent_explicit_create_employee_deny_still_wins() -> None:
    from types import SimpleNamespace

    result = await capability_gate.check_capability(
        db=_QueuedScalarDB([SimpleNamespace(allowed=False, requires_approval=False)]),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_name="create_digital_employee",
    )

    assert result.allowed is False
    assert result.denied is True
    assert result.escalate_to_l3 is False
    assert result.capability == "agent.employee.create"
    assert result.policy_found is True


# ── P1-W2-9 — startup reconciliation ──────────────────────────


class _FakeCollected:
    def __init__(self, safe: set[str], sensitive: set[str]) -> None:
        self.safe = safe
        self.sensitive = sensitive


def test_audit_returns_unmapped_and_stale_lists(monkeypatch) -> None:
    from app.services import capability_gate as cg

    # Pretend the registry has read_file (mapped + exempt), a brand new
    # tool no one mapped yet, and one exempt-only discovery tool.
    fake = _FakeCollected(safe={"read_file", "tool_search"}, sensitive={"shiny_new_tool"})
    monkeypatch.setattr(cg, "collect_tools", lambda: fake, raising=False)
    # Also stub the import inside audit_capability_mapping so it picks
    # the local fake rather than the real collector.
    import app.tools.collector as real_collector

    monkeypatch.setattr(real_collector, "collect_tools", lambda: fake)

    # Inject one stale entry into CAPABILITY_MAP for this test only.
    monkeypatch.setitem(cg.CAPABILITY_MAP, "_test_stale_tool", "test.cap")

    result = cg.audit_capability_mapping()

    assert "shiny_new_tool" in result["unmapped"]
    # Exempt tools are not flagged as unmapped.
    assert "tool_search" not in result["unmapped"]
    # Mapped tools are not flagged as unmapped.
    assert "read_file" not in result["unmapped"]
    # Stale entry surfaces.
    assert "_test_stale_tool" in result["stale"]


def test_audit_clean_when_everything_mapped_or_exempt(monkeypatch) -> None:
    from app.services import capability_gate as cg

    fake = _FakeCollected(safe={"read_file"}, sensitive={"send_email"})
    import app.tools.collector as real_collector

    monkeypatch.setattr(real_collector, "collect_tools", lambda: fake)

    result = cg.audit_capability_mapping()

    assert result["unmapped"] == []


def test_audit_handles_collector_failure_gracefully(monkeypatch) -> None:
    """A broken collector must not crash startup."""
    from app.services import capability_gate as cg

    def boom():
        raise RuntimeError("collector broken")

    import app.tools.collector as real_collector

    monkeypatch.setattr(real_collector, "collect_tools", boom)

    result = cg.audit_capability_mapping()

    assert result == {"unmapped": [], "stale": []}


# ── T1 (§8.1 #6) — promoted core tools must stay mapped ───────────


def test_t1_core_promoted_tools_have_capability_mappings() -> None:
    """The six T1 tools (source capabilities + work ledger) are promoted to
    CORE_TOOL_NAMES; under STRICT_CAPABILITY_MAPPING an unmapped tool is
    denied at call time, so their mappings are pinned here against drift."""
    for tool_name in (
        "spawn_subagent",
        "propose_dynamic_workflow",
        "preview_workflow",
        "start_workflow",
        "track_todo",
        "record_finding",
        "read_ledger",
    ):
        assert tool_name in capability_gate.CAPABILITY_MAP, tool_name
