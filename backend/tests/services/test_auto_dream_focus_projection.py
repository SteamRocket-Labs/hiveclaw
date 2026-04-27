from __future__ import annotations

from uuid import uuid4


def test_cleanup_focus_skips_objective_projection(monkeypatch, tmp_path):
    from app.services import auto_dream

    agent_id = uuid4()
    ws = tmp_path / str(agent_id)
    ws.mkdir(parents=True)
    focus = ws / "focus.md"
    original = (
        "# Focus\n\n"
        "<!-- AUTO-GENERATED FROM agent_objectives. Do not edit manually. -->\n\n"
        "## Tasks\n"
        "- [x] old_task :: done\n"
    )
    focus.write_text(original, encoding="utf-8")

    monkeypatch.setattr("app.services.heartbeat._get_canonical_workspace", lambda _agent_id: ws)

    auto_dream._cleanup_focus(agent_id)

    assert focus.read_text(encoding="utf-8") == original
