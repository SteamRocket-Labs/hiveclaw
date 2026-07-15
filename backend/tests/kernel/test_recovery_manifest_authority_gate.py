from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4


def test_post_compaction_does_not_consume_foreign_session_manifest(tmp_path, monkeypatch) -> None:
    """A session mismatch must block every post-compaction recovery byte."""

    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    manifest_path = tmp_path / str(agent_id) / "runtime_artifacts" / "recovery_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": "session-a",
                "pending_items": ["PRIVATE_SESSION_A_RECOVERY_SENTINEL"],
                "permission_profile": {"mode": "full_access"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(
        agent_id,
        session_context=SessionContext(session_id="session-b"),
    )

    assert "PRIVATE_SESSION_A_RECOVERY_SENTINEL" not in restored
    assert "full_access" not in restored
