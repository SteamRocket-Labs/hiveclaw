from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4


def test_write_trigger_output_artifact_records_job_output(tmp_path):
    from app.services.trigger_artifacts import write_trigger_output_artifact

    agent_id = uuid4()
    runtime_task_id = uuid4().hex
    trigger = SimpleNamespace(
        id=uuid4(),
        name="daily_report",
        type="cron",
        config={"trigger_class": "scheduled_job"},
    )

    artifact = write_trigger_output_artifact(
        agent_data_dir=tmp_path,
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        triggers=[trigger],
        final_reply="Report delivered.\n[OBJECTIVE_EVIDENCE: workspace/report.md]",
        metadata={"model_id": "model-1", "execution_class": "scheduled_job"},
    )

    assert artifact is not None
    artifact_path = tmp_path / str(agent_id) / artifact["path"]
    assert artifact_path.exists()
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["runtime_task_id"] == runtime_task_id
    assert payload["triggers"][0]["name"] == "daily_report"
    assert payload["metadata"]["execution_class"] == "scheduled_job"
    assert "Report delivered" in payload["final_reply"]
