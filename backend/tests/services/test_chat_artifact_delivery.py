from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_artifact_policy_accepts_workspace_user_artifact(tmp_path):
    from app.services.chat_artifact_delivery import build_artifact_candidate

    workspace = tmp_path / "agent"
    report = workspace / "workspace" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Report\n", encoding="utf-8")

    artifact = build_artifact_candidate(
        agent_id=uuid4(),
        session_id=uuid4(),
        runtime_task_id=uuid4(),
        path="workspace/report.md",
        workspace_root=workspace,
        source="workspace_write",
    )

    assert artifact is not None
    assert artifact["path"] == "workspace/report.md"
    assert artifact["name"] == "report.md"
    assert artifact["preview_kind"] == "markdown"
    assert artifact["size"] == report.stat().st_size


@pytest.mark.parametrize(
    "path",
    [
        "memory/t3/user.md",
        "evolution/scorecard.md",
        "runtime_artifacts/long_tasks/task/work_ledger.json",
        ".staging/t2_jobs/job/source_bundle.json",
        "../secret.txt",
        "/tmp/secret.txt",
        "workspace/../secret.txt",
        "workspace/workspace/shadow.md",
    ],
)
def test_artifact_policy_rejects_internal_or_escaping_paths(tmp_path, path):
    from app.services.chat_artifact_delivery import build_artifact_candidate

    workspace = tmp_path / "agent"
    workspace.mkdir()

    artifact = build_artifact_candidate(
        agent_id=uuid4(),
        session_id=uuid4(),
        runtime_task_id=uuid4(),
        path=path,
        workspace_root=workspace,
        source="workspace_write",
    )

    assert artifact is None


def test_serialize_chat_message_appends_artifact_parts():
    from app.services.chat_message_parts import serialize_chat_message

    message_id = uuid4()
    message = SimpleNamespace(
        id=message_id,
        role="assistant",
        content="已完成。",
        thinking=None,
        created_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
    )

    serialized = serialize_chat_message(
        message,
        artifacts=[
            {
                "id": "artifact-1",
                "path": "workspace/report.md",
                "name": "report.md",
                "mime_type": "text/markdown",
                "size": 128,
                "preview_kind": "markdown",
                "source": "workspace_write",
                "runtime_task_id": "run-1",
                "created_at": "2026-06-20T00:00:00+00:00",
            }
        ],
    )

    assert serialized["parts"] == [
        {"type": "text", "text": "已完成。"},
        {
            "type": "artifact",
            "artifact_id": "artifact-1",
            "path": "workspace/report.md",
            "name": "report.md",
            "mime_type": "text/markdown",
            "size": 128,
            "preview_kind": "markdown",
            "source": "workspace_write",
            "runtime_task_id": "run-1",
            "created_at": "2026-06-20T00:00:00+00:00",
        },
    ]


def test_build_done_event_appends_artifact_parts():
    from app.services.chat_message_parts import build_done_event

    event = build_done_event(
        "已完成。",
        artifacts=[
            {
                "artifact_id": "artifact-1",
                "path": "workspace/report.md",
                "name": "report.md",
                "preview_kind": "markdown",
                "source": "workspace_write",
            }
        ],
    )

    assert event["type"] == "done"
    assert event["parts"][-1]["type"] == "artifact"
    assert event["artifacts"][0]["path"] == "workspace/report.md"


def test_platform_runtime_source_must_not_create_agent_session():
    from app.services.chat_artifact_delivery import ensure_agent_session_source

    with pytest.raises(ValueError, match="pure platform"):
        ensure_agent_session_source("health_check")
