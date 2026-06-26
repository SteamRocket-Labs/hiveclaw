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
    assert artifact["snapshot"]["preview_content"] == "# Report\n"
    assert artifact["preview_snapshot_content"] == "# Report\n"


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


@pytest.mark.parametrize(
    "tool_name, args, expected",
    [
        ("write_file", {"path": "workspace/a.md"}, ["workspace/a.md"]),
        ("edit_file", {"path": "workspace/b.md"}, ["workspace/b.md"]),
        ("fs_write", {"path": "workspace/c.md"}, ["workspace/c.md"]),
        ("office_document_create", {"path": "workspace/d.docx"}, ["workspace/d.docx"]),
        ("office_document_apply", {"output_path": "workspace/e.docx", "path": "workspace/src.docx"}, ["workspace/e.docx"]),
        ("office_document_apply", {"path": "workspace/src.docx"}, ["workspace/src.docx"]),
    ],
)
def test_tool_session_write_paths_resolves_path_bearing_writers(tool_name, args, expected):
    """A-5: every file-producing tool whose output path is derivable from its
    args resolves to that workspace path. Reverting any branch drops coverage."""
    from app.services.chat_artifact_delivery import tool_session_write_paths

    assert tool_session_write_paths(tool_name, args) == expected


@pytest.mark.parametrize(
    "tool_name, args",
    [
        # execute_code / run_command write arbitrary files via code/command
        # strings — the produced path is NOT derivable from args, so this
        # mechanism cannot cover them (documented non-coverage, not a miss).
        ("execute_code", {"language": "python", "code": "open('x','w').write('1')"}),
        ("run_command", {"command": "echo hi > out.txt"}),
        # send_channel_file / upload_image consume an existing file; registering
        # them here would double-register the artifact its producer already did.
        ("send_channel_file", {"file_path": "workspace/report.md"}),
        ("upload_image", {"file_path": "workspace/chart.png"}),
    ],
)
def test_tool_session_write_paths_skips_non_derivable_or_consumer_tools(tool_name, args):
    from app.services.chat_artifact_delivery import tool_session_write_paths

    assert tool_session_write_paths(tool_name, args) == []


def test_build_session_artifact_parts_returns_rowless_parts_for_safe_paths(tmp_path):
    """A-1 helper: producers without a chat message (workflow run)
    build artifact-delivery parts directly from safe workspace paths."""
    from app.services.chat_artifact_delivery import build_session_artifact_parts

    workspace = tmp_path / "agent"
    report = workspace / "workspace" / "workflow_reports" / "run-1" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Report\n", encoding="utf-8")

    parts = build_session_artifact_parts(
        agent_id=uuid4(),
        session_id=uuid4(),
        runtime_task_id=uuid4(),
        paths=["workspace/workflow_reports/run-1/report.md", "memory/t3/user.md"],
        workspace_root=workspace,
        source="workflow",
    )

    assert len(parts) == 1  # internal memory path rejected, report kept
    part = parts[0]
    assert part["type"] == "artifact"
    assert part["path"] == "workspace/workflow_reports/run-1/report.md"
    assert part["preview_kind"] == "markdown"
    assert part["source"] == "workflow"
    assert part["artifact_id"]
    assert part["preview_snapshot_content"] == "# Report\n"


def test_build_session_artifact_parts_dedupes_identical_paths(tmp_path):
    from app.services.chat_artifact_delivery import build_session_artifact_parts

    workspace = tmp_path / "agent"
    report = workspace / "workspace" / "r.md"
    report.parent.mkdir(parents=True)
    report.write_text("# R\n", encoding="utf-8")

    parts = build_session_artifact_parts(
        agent_id=uuid4(),
        session_id=uuid4(),
        runtime_task_id=None,
        paths=["workspace/r.md", "workspace/r.md"],
        workspace_root=workspace,
    )

    assert len(parts) == 1
