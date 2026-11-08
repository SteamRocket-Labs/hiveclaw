"""Retired explicit memory cannot return through compaction's file restoration."""

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.parametrize("surface", ["index", "recent_file"])
def test_compaction_restores_active_memory_without_retired_preview_or_file(tmp_path, monkeypatch, surface):
    from app.kernel.engine import _build_restoration_context
    from app.memory.explicit_overlay import prepare_explicit_memory_entry, stage_explicit_memory_entry, update_explicit_overlay_status
    from app.runtime.session import SessionContext
    from app.services.agent_asset_transaction import AgentAssetTransaction

    agent = uuid4()
    root = tmp_path / str(agent)
    for entry_id, content in [("explicit-old", "RETIRED_PRIVATE_MARKER"), ("explicit-current", "ACTIVE_CURRENT_MARKER")]:
        prepared = prepare_explicit_memory_entry(agent_id=agent, data_root=tmp_path, entry_id=entry_id,
            decision=SimpleNamespace(category="user", content=content, sensitivity="normal", metadata={}),
            source_refs=[], target_hint="user", origin="explicit_user_request")
        with AgentAssetTransaction(root, operation="test_recovery") as tx:
            stage_explicit_memory_entry(tx, prepared)
            tx.commit()
    update_explicit_overlay_status(tmp_path, agent, "explicit-old", status="retired", reason="user_requested")
    (root / "soul.md").write_text("A valid identity that starts the ordinary recovery path.")
    session = SessionContext()
    if surface == "recent_file":
        session.recent_files = ["memory/explicit/entries/explicit-old.md", "memory/explicit/entries/explicit-current.md"]
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    restored = _build_restoration_context(agent, session_context=session)
    assert "ACTIVE_CURRENT_MARKER" in restored
    assert "RETIRED_PRIVATE_MARKER" not in restored
    if surface == "recent_file":
        assert "### Recent File: memory/explicit/entries/explicit-current.md" in restored
