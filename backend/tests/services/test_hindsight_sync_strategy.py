"""P1-W3-8 — Hindsight sync trigger policy.

Pin the canonical trigger surface so future contributors who add a
"sync now" call have a forcing function to add it to the policy doc.

Two contracts:
  1. The dream completion path now syncs to Hindsight (it didn't before
     this audit; recall would see stale data until the next heartbeat).
  2. The set of files that import `sync_t3_to_hindsight` matches the
     three sanctioned trigger points: heartbeat, dream, admin rebuild.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services import auto_dream


@pytest.mark.asyncio
async def test_dream_runs_hindsight_sync_after_apply(monkeypatch, tmp_path) -> None:
    """run_dream's tail must invoke sync_t3_to_hindsight so the recall
    layer sees fresh T3 immediately."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path), raising=False)

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_root = tmp_path / str(agent_id)
    (agent_root / "memory").mkdir(parents=True)
    # `auto_dream.run_dream` reads soul.md; touch an empty one.
    (agent_root / "soul.md").write_text("# soul\n", encoding="utf-8")

    sync_spy = AsyncMock(return_value=3)
    monkeypatch.setattr(
        "app.memory.hindsight_sync.sync_t3_to_hindsight",
        sync_spy,
    )

    # Stub everything else dream depends on so the test stays fast and
    # only asserts the Hindsight wiring.
    monkeypatch.setattr(
        auto_dream,
        "_read_all_t3",
        lambda _aid: {"feedback.md": "- recent feedback"},
    )
    monkeypatch.setattr(auto_dream, "_dream_llm_consolidate", AsyncMock(return_value=None))
    monkeypatch.setattr(auto_dream, "_consolidate_t3_files", lambda _aid: {"removed": 0, "added": 0})
    monkeypatch.setattr(
        auto_dream,
        "_promote_repeated_feedback_to_soul",
        lambda _aid, _txt: {"count": 0, "decisions": []},
    )
    monkeypatch.setattr(auto_dream, "_truncate_t2", lambda _aid, keep=10: 0)
    monkeypatch.setattr(auto_dream, "_count_t3_entries", lambda _aid: 1)

    await auto_dream.run_dream(agent_id, tenant_id)

    sync_spy.assert_awaited_once()
    args, kwargs = sync_spy.call_args
    assert args[0] == agent_id
    assert args[1] == tenant_id


@pytest.mark.asyncio
async def test_dream_swallows_hindsight_sync_errors(monkeypatch, tmp_path) -> None:
    """A Hindsight failure must not break the dream return path."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path), raising=False)

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_root = tmp_path / str(agent_id)
    (agent_root / "memory").mkdir(parents=True)
    (agent_root / "soul.md").write_text("# soul\n", encoding="utf-8")

    monkeypatch.setattr(
        "app.memory.hindsight_sync.sync_t3_to_hindsight",
        AsyncMock(side_effect=RuntimeError("hindsight down")),
    )
    monkeypatch.setattr(
        auto_dream,
        "_read_all_t3",
        lambda _aid: {"feedback.md": "- recent feedback"},
    )
    monkeypatch.setattr(auto_dream, "_dream_llm_consolidate", AsyncMock(return_value=None))
    monkeypatch.setattr(auto_dream, "_consolidate_t3_files", lambda _aid: {"removed": 0, "added": 0})
    monkeypatch.setattr(
        auto_dream,
        "_promote_repeated_feedback_to_soul",
        lambda _aid, _txt: {"count": 0, "decisions": []},
    )
    monkeypatch.setattr(auto_dream, "_truncate_t2", lambda _aid, keep=10: 0)
    monkeypatch.setattr(auto_dream, "_count_t3_entries", lambda _aid: 1)

    # Should return cleanly even though hindsight raised.
    result = await auto_dream.run_dream(agent_id, tenant_id)
    assert isinstance(result, dict)


def test_sanctioned_callers_only() -> None:
    """The set of importers of `sync_t3_to_hindsight` defines the trigger
    policy. Adding a fourth caller without updating this allowlist (and
    the docstring in `app/memory/hindsight_sync.py`) trips this test.
    """
    backend_root = Path(__file__).parent.parent.parent
    app_dir = backend_root / "app"

    sanctioned = {
        # The function definition itself.
        Path("app/memory/hindsight_sync.py"),
        # Governed T3 append API: every durable T3 write propagates (spec §12 P2).
        Path("app/memory/t3_store.py"),
        # Heartbeat: T2→T3 curation tick.
        Path("app/services/heartbeat.py"),
        # Dream: post-consolidation propagation (P1-W3-8).
        Path("app/services/auto_dream.py"),
        # Admin: manual rebuild after migrations.
        Path("app/admin/rebuild_hindsight.py"),
    }

    importer_re = re.compile(r"\bsync_t3_to_hindsight\b")
    callers: set[Path] = set()
    for py_file in app_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        if importer_re.search(text):
            callers.add(py_file.relative_to(backend_root))

    extra = callers - sanctioned
    assert not extra, (
        "Unsanctioned callers of sync_t3_to_hindsight detected. Either "
        "remove them or add the file to the allowlist + the trigger doc "
        f"in app/memory/hindsight_sync.py: {extra}"
    )
    missing = sanctioned - callers
    assert not missing, f"Sanctioned trigger missing its call: {missing}"


def test_hindsight_module_docstring_lists_three_triggers() -> None:
    """The module docstring is the contract surface — drift between code
    and docs is what got us here. Pin the trigger headings."""
    from app.memory import hindsight_sync

    doc = hindsight_sync.__doc__ or ""
    assert "Heartbeat tick" in doc
    assert "Dream completion" in doc
    assert "Admin rebuild" in doc
