from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_task_notification_runtime_context_includes_full_canonical_model_context():
    from app.services.agent_session_continuation import build_task_notification_runtime_context

    model_context = '{"team":"Research","member_outputs":[{"summary":"full evidence"}]}'
    result = build_task_notification_runtime_context(
        task_id="close:team-1:1",
        task_type="agent_team_close",
        status="completed",
        summary="Team outputs are ready for synthesis.",
        source="agent_team_close",
        model_context=model_context,
    )

    assert model_context in result
    assert "Synthesize the canonical context" in result


class _DB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _agent_session(*, state: str = "open", session_kind: str = "subagent", runtime_source: str = "subagent"):
    parent_session_id = uuid4()
    return SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        parent_session_id=parent_session_id,
        root_session_id=parent_session_id,
        visibility_scope="team",
        listed_surface="parent",
        session_kind=session_kind,
        runtime_source=runtime_source,
        transcript_metadata_json={"session_state": state},
    )


@pytest.mark.asyncio
async def test_agent_session_continuation_active_run_queues_to_session_v2_round(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="running")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    captured: dict = {}
    active_run = SimpleNamespace(
        id=uuid4(), status="running", metadata_json={}, created_at=None, started_at=None, completed_at=None
    )

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**kwargs):
        captured["find_active"] = kwargs
        return active_run

    async def fake_queue(**kwargs):
        captured["queue"] = kwargs
        return {"run_id": kwargs["active_run"].id.hex, "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "_submit_active_session_input", fake_queue)

    result = await svc.continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message="inspect the new evidence",
        parent_session_id=str(session.parent_session_id),
    )

    assert result["status"] == "queued"
    assert result["consumer"] == "session_v2_round_input"
    assert captured["append"]["event_type"] == "agent_session_message"
    assert captured["append"]["role"] == "user"
    assert captured["append"]["metadata"]["mailbox_kind"] == "followup"
    assert captured["queue"]["message_already_in_t0"] is True
    assert captured["queue"]["content"] == "inspect the new evidence"


@pytest.mark.asyncio
async def test_agent_session_continuation_inactive_open_session_starts_turn(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="open")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    captured: dict = {}

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**kwargs):
        return None

    async def fake_start(**kwargs):
        captured["start"] = kwargs
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    result = await svc.continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message="continue from the last result",
        parent_session_id=str(session.parent_session_id),
    )

    assert result["status"] == "started"
    assert result["run_id"] == "run-1"
    assert captured["append"]["event_type"] == "agent_session_message"
    assert captured["start"]["append_user_message"] is False
    assert captured["start"]["runtime_task_type"] == "web_chat_turn"
    assert captured["start"]["extra_metadata"]["source"] == "agent_session_mailbox"
    assert captured["start"]["extra_metadata"]["latest_user_prompt_overrides_history"] is True


@pytest.mark.asyncio
async def test_task_notification_continuation_is_system_runtime_context_not_user_turn(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="open")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    captured: dict = {}

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**kwargs):
        return None

    async def fake_start(**kwargs):
        captured["start"] = kwargs
        return {"run_id": "run-task-notification", "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    result = await svc.continue_parent_session_with_task_notification(
        db=db,
        agent=agent,
        user=user,
        session=session,
        task_id="task-1",
        task_type="a2a_delegation",
        status="completed",
        summary="Research is complete.",
        child_session_id="child-session-1",
        child_agent_name="Researcher",
        source="a2a_delegation",
        metadata={"trace_id": "trace-1"},
    )

    assert result["status"] == "started"
    assert captured["append"]["event_type"] == "agent_task_notification"
    assert captured["append"]["role"] == "system"
    assert captured["append"]["materialize_chat_message"] is False
    assert captured["append"]["metadata"]["mailbox_kind"] == "task_notification"
    assert captured["append"]["metadata"]["task_type"] == "a2a_delegation"
    assert captured["append"]["metadata"]["trace_id"] == "trace-1"
    assert captured["append"]["content"] == "Researcher completed: Research is complete."
    assert "<task-notification>" not in captured["append"]["content"]
    assert "<task-notification>" in captured["append"]["metadata"]["task_notification_envelope"]
    assert "<task-id>task-1</task-id>" in captured["append"]["metadata"]["task_notification_envelope"]
    assert (
        "<child-session-id>child-session-1</child-session-id>"
        in captured["append"]["metadata"]["task_notification_envelope"]
    )
    assert captured["start"]["append_user_message"] is False
    assert "<task-notification>" not in captured["start"]["content"]
    assert "Runtime task notification" in captured["start"]["content"]
    assert "This is not a user message" in captured["start"]["content"]
    assert captured["start"]["extra_metadata"]["source"] == "task_notification"
    assert captured["start"]["extra_metadata"]["task_notification"] is True
    assert captured["start"]["extra_metadata"]["runtime_mailbox_role"] == "system"
    assert captured["start"]["extra_metadata"]["latest_user_prompt_overrides_history"] is False


@pytest.mark.asyncio
async def test_result_page_preserves_runtime_action_projection_without_copying_result_bytes(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="open")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    appended: list[dict] = []
    captured: dict = {}
    result_ref = f"runtime-result://{uuid4()}/{'a' * 64}"
    manifest = {
        "schema": "hive.runtime_result_integration_page.v1",
        "integration_epoch": 4,
        "manifest_sha256": "b" * 64,
        "root_runtime_task_id": str(uuid4()),
        "mailbox_sequence_start": 9,
        "mailbox_sequence_end": 9,
        "coverage": {"expected": 1, "terminal": 1, "conserved": True},
        "items": [
            {
                "outbox_id": str(uuid4()),
                "mailbox_sequence": 9,
                "source_kind": "workflow",
                "source_run_id": "workflow-run-9",
                "task_type": "workflow",
                "terminal_status": "completed",
                "child_agent_name": "Reporter",
                "result_ref": result_ref,
                "result_sha256": "a" * 64,
                "result_size_bytes": 1_048_576,
                "artifact_count": 1,
            }
        ],
    }

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**_kwargs):
        return None

    async def fake_start(**kwargs):
        captured["start"] = kwargs
        return {"run_id": "result-integration-run", "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    result = await svc.continue_parent_session_with_result_page(
        db=db,
        agent=agent,
        user=user,
        session=session,
        integration_page_id=uuid4(),
        manifest=manifest,
    )

    assert result["status"] == "started"
    assert [event["event_type"] for event in appended] == [
        "runtime_action_completed",
        "agent_task_notification",
    ]
    action = appended[0]
    assert action["metadata"]["result_ref"] == result_ref
    assert action["metadata"]["runtime_task_id"] == "workflow-run-9"
    assert action["metadata"]["workflow_run_id"] == "workflow-run-9"
    assert "summary" not in action["metadata"]
    assert "artifacts" not in action["metadata"]
    assert result_ref in captured["start"]["content"]
    assert "read_runtime_result" in captured["start"]["content"]
    assert "decisive child body" not in captured["start"]["content"]


@pytest.mark.asyncio
async def test_task_notification_projects_runtime_action_before_mailbox(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="open")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    appended: list[dict] = []
    captured: dict = {}

    async def fake_append(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**kwargs):
        return None

    async def fake_start(**kwargs):
        captured["start"] = kwargs
        return {"run_id": "run-task-notification", "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    result = await svc.continue_parent_session_with_task_notification(
        db=db,
        agent=agent,
        user=user,
        session=session,
        task_id="workflow-run-1",
        task_type="workflow",
        status="completed",
        summary="Workflow delivered the report.",
        source="workflow",
        metadata={"workflow_run_id": "workflow-run-1"},
    )

    assert result["status"] == "started"
    assert [call["event_type"] for call in appended] == ["runtime_action_completed", "agent_task_notification"]
    runtime_event = appended[0]
    assert runtime_event["role"] == "system"
    assert runtime_event["materialize_chat_message"] is False
    assert runtime_event["metadata"]["type"] == "runtime_action_completed"
    assert runtime_event["metadata"]["action_kind"] == "workflow"
    assert runtime_event["metadata"]["notification_source"] == "workflow"
    assert runtime_event["metadata"]["runtime_task_id"] == "workflow-run-1"
    assert runtime_event["metadata"]["workflow_run_id"] == "workflow-run-1"
    assert runtime_event["metadata"]["status"] == "completed"
    assert runtime_event["content"] == "Workflow delivered the report."


@pytest.mark.asyncio
async def test_task_notification_carries_a2a_artifact_refs_to_parent_turn(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="open")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    captured: dict = {}
    artifact_parts = [
        {
            "type": "artifact",
            "artifact_id": "artifact-1",
            "path": "workspace/web3-report.md",
            "name": "web3-report.md",
            "preview_kind": "markdown",
            "source": "a2a_workspace_write",
            "owner_agent_id": "agent-b",
            "source_agent_id": "agent-b",
            "download_agent_id": "agent-b",
        }
    ]

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**kwargs):
        return None

    async def fake_start(**kwargs):
        captured["start"] = kwargs
        return {"run_id": "run-task-notification", "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    await svc.continue_parent_session_with_task_notification(
        db=db,
        agent=agent,
        user=user,
        session=session,
        task_id="task-a2a",
        task_type="a2a_delegation",
        status="completed",
        summary="Report is ready.",
        child_session_id="child-session-1",
        child_agent_name="Researcher",
        source="a2a_delegation",
        artifacts=artifact_parts,
    )

    assert captured["append"]["parts"] == artifact_parts
    assert captured["append"]["metadata"]["artifacts"] == artifact_parts
    assert captured["start"]["extra_metadata"]["artifacts"] == artifact_parts
    assert (
        "<artifact-path>workspace/web3-report.md</artifact-path>"
        in captured["append"]["metadata"]["task_notification_envelope"]
    )
    assert (
        "<download-agent-id>agent-b</download-agent-id>" in captured["append"]["metadata"]["task_notification_envelope"]
    )
    assert "workspace/web3-report.md" in captured["start"]["content"]


@pytest.mark.asyncio
async def test_task_notification_active_parent_run_queues_to_midrun_consumer(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="open")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    active_run = SimpleNamespace(
        id=uuid4(), status="running", metadata_json={}, created_at=None, started_at=None, completed_at=None
    )
    captured: dict = {}

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**kwargs):
        return active_run

    async def fake_queue(**kwargs):
        captured["queue"] = kwargs
        return {"run_id": kwargs["active_run"].id.hex, "status": "running"}

    async def fake_start(**_kwargs):
        raise AssertionError("active parent run must consume task notifications through mid-run drain")

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "_submit_active_session_input", fake_queue)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    result = await svc.continue_parent_session_with_task_notification(
        db=db,
        agent=agent,
        user=user,
        session=session,
        task_id="task-2",
        task_type="subagent",
        status="completed",
        summary="Subagent finished.",
        child_session_id="child-session-2",
        child_agent_name="Subagent",
        source="subagent",
    )

    assert result["status"] == "queued"
    assert result["consumer"] == "session_v2_round_input"
    assert captured["append"]["event_type"] == "agent_task_notification"
    assert captured["append"]["role"] == "system"
    assert captured["append"]["materialize_chat_message"] is False
    assert captured["queue"]["source_channel"] == "task_notification"
    assert captured["queue"]["message_already_in_t0"] is True
    assert captured["queue"]["role"] == "system"
    assert "<task-id>task-2</task-id>" not in captured["queue"]["content"]
    assert "Runtime task notification" in captured["queue"]["content"]


@pytest.mark.asyncio
async def test_agent_session_continuation_non_subagent_terminal_session_rejects_and_writes_transcript(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="completed", session_kind="agent_chat", runtime_source="agent")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    captured: dict = {}

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid4())

    async def fake_start(**_kwargs):
        raise AssertionError("terminal agent sessions must not start a continuation")

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    result = await svc.continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message="reopen this",
        parent_session_id=str(session.parent_session_id),
    )

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert result["reason"] == "terminal_agent_session"
    assert captured["append"]["event_type"] == "agent_session_message_rejected"
    assert captured["append"]["metadata"]["session_state"] == "completed"
    assert db.commits == 1
