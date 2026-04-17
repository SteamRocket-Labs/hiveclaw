"""Tests for the admin rebuild_hindsight CLI."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.admin import rebuild_hindsight as rh
from app.memory.backend import reset_memory_backend
from app.memory.hindsight_sync import _CURSOR_FILENAME


TENANT = uuid.UUID("aaaaaaaa-1111-2222-3333-444444444444")
AGENT_A = uuid.UUID("bbbbbbbb-5555-6666-7777-888888888888")
AGENT_B = uuid.UUID("cccccccc-9999-aaaa-bbbb-cccccccccccc")


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    from app.config import get_settings
    reset_memory_backend()
    get_settings.cache_clear()
    yield
    reset_memory_backend()
    get_settings.cache_clear()


def _seed_t3(dir_: Path, agent_id: uuid.UUID, bullets: list[str]) -> None:
    mem = dir_ / str(agent_id) / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    body = "# feedback\n\n" + "\n".join(f"- {b}" for b in bullets) + "\n"
    (mem / "feedback.md").write_text(body, encoding="utf-8")


def test_reset_cursor_removes_file(tmp_path: Path) -> None:
    mem = tmp_path / str(AGENT_A) / "memory"
    mem.mkdir(parents=True)
    cursor = mem / _CURSOR_FILENAME
    cursor.write_text('{"feedback.md": {"mtime": 1, "size": 0}}', encoding="utf-8")
    assert cursor.exists()
    rh._reset_cursor(tmp_path, AGENT_A)
    assert not cursor.exists()


def test_reset_cursor_missing_is_noop(tmp_path: Path) -> None:
    # Must not raise when cursor never existed
    rh._reset_cursor(tmp_path, AGENT_A)


@pytest.mark.asyncio
async def test_rebuild_invokes_sync_per_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Point AGENT_DATA_DIR at tmp_path for test isolation
    from app.config import get_settings
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    _seed_t3(tmp_path, AGENT_A, ["fact a1", "fact a2"])
    _seed_t3(tmp_path, AGENT_B, ["fact b1"])

    synced_calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_sync(agent_id, tenant_id, *, data_root=None):
        synced_calls.append((agent_id, tenant_id))
        return 42  # arbitrary nonzero

    async def fake_agents(tenant_id, agent_id):
        return [AGENT_A, AGENT_B] if agent_id is None else [agent_id]

    monkeypatch.setattr(rh, "sync_t3_to_hindsight", fake_sync)
    monkeypatch.setattr(rh, "_agents_in_tenant", fake_agents)

    total = await rh.rebuild(TENANT, None)

    assert total == 84
    assert {a for a, _ in synced_calls} == {AGENT_A, AGENT_B}
    assert all(t == TENANT for _, t in synced_calls)


@pytest.mark.asyncio
async def test_rebuild_scoped_to_single_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.config import get_settings
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    called = []

    async def fake_sync(agent_id, tenant_id, *, data_root=None):
        called.append(agent_id)
        return 7

    async def fake_agents(tenant_id, agent_id):
        return [agent_id] if agent_id else []

    monkeypatch.setattr(rh, "sync_t3_to_hindsight", fake_sync)
    monkeypatch.setattr(rh, "_agents_in_tenant", fake_agents)

    total = await rh.rebuild(TENANT, AGENT_A)
    assert total == 7
    assert called == [AGENT_A]


@pytest.mark.asyncio
async def test_rebuild_returns_zero_when_no_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.config import get_settings
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    async def fake_sync(*args, **kwargs):
        pytest.fail("sync should not run when tenant has no agents")

    async def fake_agents(tenant_id, agent_id):
        return []

    monkeypatch.setattr(rh, "sync_t3_to_hindsight", fake_sync)
    monkeypatch.setattr(rh, "_agents_in_tenant", fake_agents)

    assert await rh.rebuild(TENANT, None) == 0
