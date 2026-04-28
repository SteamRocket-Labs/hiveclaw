"""P1-W2-8 — capability mapping fail-closed mode + drift counter.

Two behaviours pinned:
  1. Unmapped tool calls always increment a per-tool counter so operators
     can see drift before deciding to flip strict mode on.
  2. Under STRICT_CAPABILITY_MAPPING, an unmapped tool is denied; explicit
     lenient mode still logs + allows for local compatibility.
"""

from __future__ import annotations

import uuid

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
