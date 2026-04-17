"""Tests for T3 → Hindsight incremental sync."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.memory.backend import reset_memory_backend
from app.memory.hindsight_sync import (
    _CURSOR_FILENAME,
    _collect_items,
    _document_id,
    sync_t3_to_hindsight,
)

TENANT = uuid.UUID("aaaaaaaa-1111-2222-3333-444444444444")
AGENT = uuid.UUID("bbbbbbbb-5555-6666-7777-888888888888")


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    from app.config import get_settings
    reset_memory_backend()
    get_settings.cache_clear()
    yield
    reset_memory_backend()
    get_settings.cache_clear()


@pytest.fixture
def agent_memory_dir(tmp_path: Path) -> Path:
    mem = tmp_path / str(AGENT) / "memory"
    mem.mkdir(parents=True)
    return mem


def _seed_t3_file(mem: Path, name: str, bullets: list[str]) -> Path:
    path = mem / name
    header = f"# {name.removesuffix('.md').title()}\n\n"
    body = "\n".join(f"- {b}" for b in bullets)
    path.write_text(header + body + "\n", encoding="utf-8")
    return path


# ── _document_id determinism ──────────────────────────────────


TS_1 = "2026-04-10T00:00:00+00:00"
TS_2 = "2026-04-15T00:00:00+00:00"


def test_document_id_stable_for_same_input() -> None:
    a = _document_id(AGENT, "feedback", "user prefers terse replies", TS_1)
    b = _document_id(AGENT, "feedback", "user prefers terse replies", TS_1)
    assert a == b
    assert len(a) == 16


def test_document_id_changes_with_agent() -> None:
    a = _document_id(AGENT, "feedback", "x", TS_1)
    b = _document_id(uuid.uuid4(), "feedback", "x", TS_1)
    assert a != b


def test_document_id_changes_with_category() -> None:
    a = _document_id(AGENT, "feedback", "x", TS_1)
    b = _document_id(AGENT, "knowledge", "x", TS_1)
    assert a != b


def test_document_id_changes_with_timestamp() -> None:
    """Same content captured at different times must produce distinct IDs
    — otherwise Hindsight silently dedupes distinct observations."""
    a = _document_id(AGENT, "feedback", "user prefers terse replies", TS_1)
    b = _document_id(AGENT, "feedback", "user prefers terse replies", TS_2)
    assert a != b


# ── _collect_items ────────────────────────────────────────────


def test_collect_items_includes_all_categories(agent_memory_dir: Path) -> None:
    _seed_t3_file(agent_memory_dir, "feedback.md", ["user likes blue"])
    _seed_t3_file(agent_memory_dir, "knowledge.md", ["Python has asyncio", "SQL is declarative"])

    items, touched = _collect_items(AGENT, agent_memory_dir, cursor={})

    assert len(items) == 3
    categories = {i["category"] for i in items}
    assert categories == {"feedback", "knowledge"}
    assert {name for name, _, _ in touched} == {"feedback.md", "knowledge.md"}


def test_collect_items_respects_cursor(agent_memory_dir: Path) -> None:
    path = _seed_t3_file(agent_memory_dir, "feedback.md", ["fact 1"])
    st = path.stat()

    items, touched = _collect_items(
        AGENT, agent_memory_dir,
        cursor={"feedback.md": {"mtime": st.st_mtime + 1, "size": float(st.st_size)}},
    )
    # Cursor ahead of mtime AND size matches → no new work
    assert items == []
    assert touched == []


def test_collect_items_resyncs_when_size_changes_but_mtime_same(
    agent_memory_dir: Path,
) -> None:
    """Guard against coarse-mtime filesystems skipping same-second appends."""
    path = _seed_t3_file(agent_memory_dir, "feedback.md", ["original", "added"])
    st = path.stat()

    # Cursor claims mtime is at least current but recorded an older smaller size
    items, touched = _collect_items(
        AGENT, agent_memory_dir,
        cursor={"feedback.md": {"mtime": st.st_mtime, "size": float(st.st_size - 10)}},
    )
    # Size mismatch forces re-scan; both bullets emitted (retain dedupes via doc_id)
    assert len(items) == 2
    assert any(name == "feedback.md" for name, _, _ in touched)


def test_collect_items_skips_unknown_files(agent_memory_dir: Path) -> None:
    _seed_t3_file(agent_memory_dir, "feedback.md", ["a"])
    (agent_memory_dir / "random.md").write_text("- should not be synced\n")
    (agent_memory_dir / "notes.txt").write_text("- ignored\n")

    items, _ = _collect_items(AGENT, agent_memory_dir, cursor={})
    assert len(items) == 1
    assert items[0]["category"] == "feedback"


# ── sync_t3_to_hindsight ──────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_returns_zero_when_tenant_is_none(tmp_path: Path) -> None:
    # No backend should be touched
    result = await sync_t3_to_hindsight(AGENT, None, data_root=tmp_path)
    assert result == 0


@pytest.mark.asyncio
async def test_sync_returns_zero_when_md_backend_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_memory_dir: Path,
) -> None:
    _seed_t3_file(agent_memory_dir, "feedback.md", ["a"])
    monkeypatch.setenv("MEMORY_BACKEND", "md")
    result = await sync_t3_to_hindsight(AGENT, TENANT, data_root=tmp_path)
    assert result == 0


async def _stub_tenant_pref_hindsight(tenant_id):
    """Pretend the tenant has explicitly opted into 'hindsight'."""
    return "hindsight"


@pytest.mark.asyncio
async def test_sync_first_run_uploads_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_memory_dir: Path,
) -> None:
    _seed_t3_file(agent_memory_dir, "feedback.md", ["one", "two"])
    _seed_t3_file(agent_memory_dir, "knowledge.md", ["three"])

    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    batches_captured: list[list[dict]] = []

    async def fake_retain_batch(self, agent_id, items):
        batches_captured.append(items)
        return True

    monkeypatch.setattr(
        "app.memory.hindsight_sync._fetch_tenant_backend_pref",
        _stub_tenant_pref_hindsight,
    )
    monkeypatch.setattr(
        "app.memory.backends.hindsight.HindsightBackend.retain_batch",
        fake_retain_batch,
    )

    result = await sync_t3_to_hindsight(AGENT, TENANT, data_root=tmp_path)

    assert result == 3
    assert len(batches_captured) == 1
    contents = {i["content"] for i in batches_captured[0]}
    assert contents == {"one", "two", "three"}
    # Cursor written
    cursor_path = agent_memory_dir / _CURSOR_FILENAME
    assert cursor_path.exists()
    cursor = json.loads(cursor_path.read_text())
    assert set(cursor) == {"feedback.md", "knowledge.md"}


@pytest.mark.asyncio
async def test_sync_second_run_incremental(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_memory_dir: Path,
) -> None:
    _seed_t3_file(agent_memory_dir, "feedback.md", ["a"])
    _seed_t3_file(agent_memory_dir, "knowledge.md", ["b"])

    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    call_count = {"n": 0}
    seen_items: list[dict] = []

    async def fake_retain_batch(self, agent_id, items):
        call_count["n"] += 1
        seen_items.extend(items)
        return True

    monkeypatch.setattr(
        "app.memory.hindsight_sync._fetch_tenant_backend_pref",
        _stub_tenant_pref_hindsight,
    )
    monkeypatch.setattr(
        "app.memory.backends.hindsight.HindsightBackend.retain_batch",
        fake_retain_batch,
    )

    # Run 1: sync both files
    r1 = await sync_t3_to_hindsight(AGENT, TENANT, data_root=tmp_path)
    assert r1 == 2

    # Run 2: no changes → nothing to do
    r2 = await sync_t3_to_hindsight(AGENT, TENANT, data_root=tmp_path)
    assert r2 == 0
    assert call_count["n"] == 1  # still only the first call

    # Modify only one file
    time.sleep(0.01)  # ensure mtime advances
    _seed_t3_file(agent_memory_dir, "feedback.md", ["a", "new-fact"])

    # Run 3: only feedback.md resynced (both bullets, upload is per-file)
    r3 = await sync_t3_to_hindsight(AGENT, TENANT, data_root=tmp_path)
    assert r3 == 2  # both "a" and "new-fact" re-uploaded (idempotent via doc_id)
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_sync_does_not_advance_cursor_on_retain_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_memory_dir: Path,
) -> None:
    _seed_t3_file(agent_memory_dir, "feedback.md", ["only"])

    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    async def fail_retain_batch(self, agent_id, items):
        return False

    monkeypatch.setattr(
        "app.memory.hindsight_sync._fetch_tenant_backend_pref",
        _stub_tenant_pref_hindsight,
    )
    monkeypatch.setattr(
        "app.memory.backends.hindsight.HindsightBackend.retain_batch",
        fail_retain_batch,
    )

    result = await sync_t3_to_hindsight(AGENT, TENANT, data_root=tmp_path)
    assert result == 0

    cursor_path = agent_memory_dir / _CURSOR_FILENAME
    assert not cursor_path.exists(), "cursor must not be written on failure"


@pytest.mark.asyncio
async def test_sync_missing_memory_dir_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    result = await sync_t3_to_hindsight(AGENT, TENANT, data_root=tmp_path)
    assert result == 0


@pytest.mark.asyncio
async def test_sync_fails_closed_when_tenant_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_memory_dir: Path,
) -> None:
    """If DB lookup fails, env MEMORY_BACKEND=hindsight must NOT take effect —
    unknown tenant state must fail-closed to MD (no external sync).
    """
    _seed_t3_file(agent_memory_dir, "feedback.md", ["secret fact"])
    monkeypatch.setenv("MEMORY_BACKEND", "hindsight")
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    from app.memory import hindsight_sync as hs

    async def lookup_fails(tenant_id):
        return hs.LOOKUP_FAILED

    called = {"retain": False}

    async def fake_retain(self, agent_id, items):
        called["retain"] = True
        return True

    monkeypatch.setattr(hs, "_fetch_tenant_backend_pref", lookup_fails)
    monkeypatch.setattr(
        "app.memory.backends.hindsight.HindsightBackend.retain_batch", fake_retain,
    )

    result = await sync_t3_to_hindsight(AGENT, TENANT, data_root=tmp_path)
    assert result == 0
    assert called["retain"] is False, "must not call external backend when tenant state is unknown"


@pytest.mark.asyncio
async def test_sync_swallows_unexpected_exceptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_memory_dir: Path,
) -> None:
    _seed_t3_file(agent_memory_dir, "feedback.md", ["a"])
    monkeypatch.setenv("HINDSIGHT_URL", "http://mock.local")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    async def boom(self, agent_id, items):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(
        "app.memory.hindsight_sync._fetch_tenant_backend_pref",
        _stub_tenant_pref_hindsight,
    )
    monkeypatch.setattr(
        "app.memory.backends.hindsight.HindsightBackend.retain_batch",
        boom,
    )

    # Must not raise
    result = await sync_t3_to_hindsight(AGENT, TENANT, data_root=tmp_path)
    assert result == 0
