from __future__ import annotations

from uuid import uuid4


def test_update_evolution_files_separates_heartbeat_and_trigger_metrics(tmp_path, monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "evolution").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(heartbeat, "_get_canonical_workspace", lambda _agent_id: workspace)

    heartbeat._update_evolution_files(agent_id, "action_taken", 7, "trigger summary", source="trigger")
    heartbeat._update_evolution_files(agent_id, "action_taken", 8, "heartbeat summary", source="heartbeat")

    scorecard = (workspace / "evolution" / "scorecard.md").read_text(encoding="utf-8")
    assert "- total_heartbeats: 1" in scorecard
    assert "- useful_heartbeats: 1" in scorecard
    assert "- total_trigger_runs: 1" in scorecard
    assert "- useful_trigger_runs: 1" in scorecard
    assert "- failed_runs: 0" in scorecard


def test_update_evolution_files_lineage_records_source(tmp_path, monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "evolution").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(heartbeat, "_get_canonical_workspace", lambda _agent_id: workspace)

    heartbeat._update_evolution_files(agent_id, "action_taken", 6, "delegation summary", source="delegation")

    lineage = (workspace / "evolution" / "lineage.md").read_text(encoding="utf-8")
    assert "- Source: delegation" in lineage

