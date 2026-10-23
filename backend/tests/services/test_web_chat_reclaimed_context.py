"""Reclaiming native work preserves real evidence without inventing a plan."""

from types import SimpleNamespace
from uuid import uuid4

from app.services.web_chat_runtime import _with_reclaimed_web_chat_resume_context


def test_reclaimed_native_chat_without_long_task_artifacts_keeps_canonical_recovery(monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    metadata = {"reclaimed_expired_claim": True, "session_restart_resume": {"committed_rounds": 1}}
    task = SimpleNamespace(id=uuid4(), parent_agent_id=uuid4(), metadata_json=metadata)
    result = _with_reclaimed_web_chat_resume_context(task)
    assert "restart_resume_context" not in result
    assert result["session_restart_resume"] == {"committed_rounds": 1}
    assert result["resumed_after_restart"] is True and result["recovery_state"] == "recovering"
    assert task.metadata_json == metadata and "resumed_after_restart" not in metadata


def test_reclaimed_work_keeps_actual_long_task_plan_and_ledger(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.services.long_task_runtime import write_long_task_plan_artifact

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    agent_id, run_id = uuid4(), uuid4()
    write_long_task_plan_artifact(agent_id=agent_id, runtime_task_id=run_id, spec="Continue the saved report",
        acceptance_criteria=["Read the final saved report"], verification_commands=["python verify.py"],
        risk_gates=[], data_root=tmp_path)
    task = SimpleNamespace(id=run_id, parent_agent_id=agent_id, metadata_json={"reclaimed_expired_claim": True})
    result = _with_reclaimed_web_chat_resume_context(task)
    context = result["restart_resume_context"]
    assert context["plan"]["spec"] == "Continue the saved report"
    assert context["work_ledger"]["present"] is True
    assert "Continue the saved report" in context["resume_prompt"]
    assert "python verify.py" in context["resume_prompt"]


def test_reclaimed_work_preserves_existing_resume_data_for_its_source_gate():
    stored = {"resume_prompt": "Unverified stored recovery content", "schema": "legacy"}
    task = SimpleNamespace(id=uuid4(), parent_agent_id=uuid4(),
        metadata_json={"reclaimed_expired_claim": True, "restart_resume_context": stored})
    result = _with_reclaimed_web_chat_resume_context(task)
    assert result["restart_resume_context"] == stored
    assert result["resumed_after_restart"] is True
