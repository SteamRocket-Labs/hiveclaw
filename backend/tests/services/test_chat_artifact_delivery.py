from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select


def test_artifact_policy_accepts_workspace_user_artifact(tmp_path):
    from app.services.chat_artifact_delivery import build_artifact_candidate

    agent_id = uuid4()
    workspace = tmp_path / "agent"
    report = workspace / "workspace" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Report\n", encoding="utf-8")

    artifact = build_artifact_candidate(
        agent_id=agent_id,
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
    assert artifact["snapshot"]["content_hash"]
    snapshot_path = workspace / artifact["snapshot"]["snapshot_storage_path"]
    assert snapshot_path.read_text(encoding="utf-8") == "# Report\n"
    assert artifact["preview_snapshot_content"] == "# Report\n"
    assert artifact["owner_agent_id"] == str(agent_id)
    assert artifact["source_agent_id"] == str(agent_id)
    assert artifact["download_agent_id"] == str(agent_id)
    assert artifact["snapshot"]["owner_agent_id"] == str(agent_id)


def test_artifact_open_returns_delivery_snapshot_after_workspace_overwrite(tmp_path):
    from app.models.chat_artifact import ChatArtifact
    from app.services.chat_artifact_delivery import build_artifact_candidate, read_chat_artifact_snapshot_content

    agent_id = uuid4()
    session_id = uuid4()
    runtime_task_id = uuid4()
    workspace = tmp_path / "agent"
    report = workspace / "workspace" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("delivery version\n", encoding="utf-8")

    candidate = build_artifact_candidate(
        agent_id=agent_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        path="workspace/report.md",
        workspace_root=workspace,
        source="workspace_write",
    )
    assert candidate is not None

    artifact = ChatArtifact(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=None,
        session_id=session_id,
        message_id=uuid4(),
        runtime_task_id=runtime_task_id,
        path=candidate["path"],
        name=candidate["name"],
        mime_type=candidate.get("mime_type"),
        size=candidate.get("size"),
        modified_at=candidate.get("modified_at"),
        preview_kind=candidate.get("preview_kind", "download"),
        source=candidate.get("source", "workspace_write"),
        snapshot_hash=candidate["snapshot_hash"],
        snapshot_json=candidate["snapshot"],
    )

    report.write_text("current workspace version\n", encoding="utf-8")

    content = read_chat_artifact_snapshot_content(artifact, workspace)

    assert content["content"] == "delivery version\n"
    assert content["uses_snapshot"] is True
    assert content["legacy_current_file_fallback"] is False
    assert content["workspace_changed"] is True


def test_chat_artifact_snapshot_cleanup_deletes_only_unreferenced_expired_files(tmp_path):
    from app.services.chat_artifact_delivery import (
        CHAT_ARTIFACT_SNAPSHOT_DIR,
        CHAT_ARTIFACT_SNAPSHOT_GC_REPORT,
        cleanup_chat_artifact_snapshots,
    )

    workspace = tmp_path / "agent"
    snapshot_root = workspace / CHAT_ARTIFACT_SNAPSHOT_DIR
    referenced = snapshot_root / "session-a" / "run-a" / "keep.md"
    old_unreferenced = snapshot_root / "session-b" / "run-b" / "delete.md"
    recent_unreferenced = snapshot_root / "session-c" / "run-c" / "recent.md"
    for path in (referenced, old_unreferenced, recent_unreferenced):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    recent_ts = datetime(2026, 2, 14, tzinfo=timezone.utc).timestamp()
    for path in (referenced, old_unreferenced):
        os.utime(path, (old_ts, old_ts))
    os.utime(recent_unreferenced, (recent_ts, recent_ts))

    report = cleanup_chat_artifact_snapshots(
        workspace_root=workspace,
        referenced_snapshot_paths=[str(referenced.relative_to(workspace))],
        retention_days=30.0,
        now=datetime(2026, 2, 15, tzinfo=timezone.utc),
    )

    assert referenced.exists()
    assert recent_unreferenced.exists()
    assert not old_unreferenced.exists()
    assert report["removed_count"] == 1
    assert report["kept_referenced_count"] == 1
    assert report["kept_recent_count"] == 1
    gc_report = json.loads((workspace / CHAT_ARTIFACT_SNAPSHOT_GC_REPORT).read_text(encoding="utf-8"))
    assert gc_report["schema"] == "chat_artifact_snapshot_gc.v1"
    assert gc_report["removed_count"] == 1
    assert gc_report["retention_days"] == 30.0


def test_execute_heartbeat_wires_chat_artifact_snapshot_retention():
    import inspect

    from app.services import heartbeat

    source = inspect.getsource(heartbeat._execute_heartbeat)
    assert "_run_chat_artifact_snapshot_retention(" in source


def test_chat_artifact_insert_statement_uses_postgres_on_conflict(tmp_path):
    from sqlalchemy.dialects import postgresql

    from app.services.chat_artifact_delivery import build_artifact_candidate, _chat_artifact_insert_statement

    agent_id = uuid4()
    session_id = uuid4()
    runtime_task_id = uuid4()
    message_id = uuid4()
    workspace = tmp_path / "agent"
    report = workspace / "workspace" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Report\n", encoding="utf-8")

    candidate = build_artifact_candidate(
        agent_id=agent_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        path="workspace/report.md",
        workspace_root=workspace,
    )
    assert candidate is not None

    statement = _chat_artifact_insert_statement(
        candidate=candidate,
        agent_id=agent_id,
        tenant_id=None,
        session_id=session_id,
        message_id=message_id,
        runtime_task_id=runtime_task_id,
        source="workspace_write",
    )
    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT ON CONSTRAINT uq_chat_artifacts_agent_session_run_path_snapshot DO NOTHING" in compiled
    assert "RETURNING chat_artifacts.id" in compiled


@pytest.mark.asyncio
async def test_chat_artifact_delivery_idempotent_for_same_run_path_snapshot(tmp_path):
    from app.services.chat_artifact_delivery import create_chat_artifacts_for_message

    agent_id = uuid4()
    session_id = uuid4()
    runtime_task_id = uuid4()
    workspace = tmp_path / "agent"
    report = workspace / "workspace" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Report\n", encoding="utf-8")

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _Db:
        def __init__(self):
            self.rows = {}

        async def execute(self, stmt):
            from sqlalchemy.dialects import postgresql

            if getattr(stmt, "is_insert", False):
                params = stmt.compile(dialect=postgresql.dialect()).params
                key = (
                    params["agent_id"],
                    params["session_id"],
                    params["runtime_task_id"],
                    params["path"],
                    params["snapshot_hash"],
                )
                if key in self.rows:
                    return _Result(None)
                from app.models.chat_artifact import ChatArtifact

                artifact = ChatArtifact(
                    id=params["id"],
                    agent_id=params["agent_id"],
                    tenant_id=params["tenant_id"],
                    session_id=params["session_id"],
                    message_id=params["message_id"],
                    runtime_task_id=params["runtime_task_id"],
                    path=params["path"],
                    name=params["name"],
                    mime_type=params["mime_type"],
                    size=params["size"],
                    modified_at=params["modified_at"],
                    preview_kind=params["preview_kind"],
                    source=params["source"],
                    snapshot_hash=params["snapshot_hash"],
                    snapshot_json=params["snapshot_json"],
                )
                self.rows[key] = artifact
                return _Result(params["id"])

            return _Result(next(iter(self.rows.values()), None))

    db = _Db()

    first = await create_chat_artifacts_for_message(
        db=db,
        agent_id=agent_id,
        tenant_id=None,
        session_id=session_id,
        message_id=uuid4(),
        runtime_task_id=runtime_task_id,
        paths=["workspace/report.md"],
        workspace_root=workspace,
    )
    second = await create_chat_artifacts_for_message(
        db=db,
        agent_id=agent_id,
        tenant_id=None,
        session_id=session_id,
        message_id=uuid4(),
        runtime_task_id=runtime_task_id,
        paths=["workspace/report.md"],
        workspace_root=workspace,
    )

    assert len(db.rows) == 1
    assert first[0]["artifact_id"] == second[0]["artifact_id"]
    assert first[0]["path"] == second[0]["path"] == "workspace/report.md"


@pytest.mark.asyncio
async def test_terminal_delivery_rebinds_existing_tool_artifact_to_final_message(tmp_path):
    from app.services.chat_artifact_delivery import create_chat_artifacts_for_message

    agent_id = uuid4()
    session_id = uuid4()
    runtime_task_id = uuid4()
    tool_message_id = uuid4()
    final_message_id = uuid4()
    workspace = tmp_path / "agent"
    report = workspace / "workspace" / "report.xlsx"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"xlsx")

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _Db:
        def __init__(self):
            self.artifact = None

        async def execute(self, stmt):
            from sqlalchemy.dialects import postgresql

            if getattr(stmt, "is_insert", False):
                params = stmt.compile(dialect=postgresql.dialect()).params
                if self.artifact is not None:
                    return _Result(None)
                from app.models.chat_artifact import ChatArtifact

                self.artifact = ChatArtifact(**params)
                return _Result(params["id"])
            return _Result(self.artifact)

    db = _Db()
    await create_chat_artifacts_for_message(
        db=db,
        agent_id=agent_id,
        tenant_id=None,
        session_id=session_id,
        message_id=tool_message_id,
        runtime_task_id=runtime_task_id,
        paths=["workspace/report.xlsx"],
        workspace_root=workspace,
    )
    await create_chat_artifacts_for_message(
        db=db,
        agent_id=agent_id,
        tenant_id=None,
        session_id=session_id,
        message_id=final_message_id,
        runtime_task_id=runtime_task_id,
        paths=["workspace/report.xlsx"],
        workspace_root=workspace,
        rebind_existing_to_message=True,
    )

    assert db.artifact.message_id == final_message_id


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_chat_artifact_delivery_real_pg_concurrent_idempotency(owner_sessionmaker, tmp_path):
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.audit import ChatMessage
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_session import ChatSession
    from app.models.participant import Participant
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_artifact_delivery import create_chat_artifacts_for_message

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    participant_id = uuid4()
    session_id = uuid4()
    runtime_task_id = uuid4()
    message_ids = [uuid4(), uuid4()]
    workspace = tmp_path / "agent"
    report = workspace / "workspace" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Concurrent Report\n", encoding="utf-8")

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="artifact-concurrency", slug=f"artifact-{tenant_id.hex[:10]}"))

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as db:
        db.add(Participant(id=participant_id, type="agent", ref_id=agent_id, display_name="Artifact Agent"))
        db.add(
            User(
                id=user_id,
                username=f"artifact-u-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@artifact.test",
                password_hash="x",
                display_name="Artifact Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="artifact-agent",
                role_description="artifact",
                creator_id=user_id,
                sponsor_user_id=user_id,
                participant_id=participant_id,
            )
        )
        db.add(
            RuntimeTask(
                id=runtime_task_id,
                task_type="web_chat_turn",
                parent_agent_id=agent_id,
                tenant_id=tenant_id,
                parent_session_id=str(session_id),
                status="running",
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Artifact concurrency",
                source_channel="web",
                runtime_task_id=runtime_task_id,
            )
        )
        for message_id in message_ids:
            db.add(
                ChatMessage(
                    id=message_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    role="assistant",
                    content="done",
                    conversation_id=str(session_id),
                )
            )

    async def deliver(message_id):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as db:
            parts = await create_chat_artifacts_for_message(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                message_id=message_id,
                runtime_task_id=runtime_task_id,
                paths=["workspace/report.md"],
                workspace_root=workspace,
            )
            await db.commit()
            return parts

    first, second = await asyncio.gather(*(deliver(message_id) for message_id in message_ids))

    assert first[0]["artifact_id"] == second[0]["artifact_id"]
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as db:
        row_count = (
            await db.execute(
                select(func.count(ChatArtifact.id)).where(
                    ChatArtifact.agent_id == agent_id,
                    ChatArtifact.session_id == session_id,
                    ChatArtifact.runtime_task_id == runtime_task_id,
                    ChatArtifact.path == "workspace/report.md",
                )
            )
        ).scalar_one()
    assert row_count == 1


def test_artifact_policy_preserves_a2a_producer_as_download_owner(tmp_path):
    from app.services.chat_artifact_delivery import build_session_artifact_parts

    parent_agent_id = uuid4()
    producer_agent_id = uuid4()
    workspace = tmp_path / "producer"
    report = workspace / "workspace" / "web3-report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Web3 Report\n", encoding="utf-8")

    parts = build_session_artifact_parts(
        agent_id=producer_agent_id,
        session_id=uuid4(),
        runtime_task_id=uuid4(),
        paths=["workspace/web3-report.md"],
        workspace_root=workspace,
        source="a2a_workspace_write",
        owner_agent_id=producer_agent_id,
        source_agent_id=producer_agent_id,
        download_agent_id=producer_agent_id,
        delivery_agent_id=parent_agent_id,
    )

    assert len(parts) == 1
    part = parts[0]
    assert part["path"] == "workspace/web3-report.md"
    assert part["owner_agent_id"] == str(producer_agent_id)
    assert part["source_agent_id"] == str(producer_agent_id)
    assert part["download_agent_id"] == str(producer_agent_id)
    assert part["delivery_agent_id"] == str(parent_agent_id)


def test_a2a_delivery_projects_child_artifact_ref_without_copying_parent_workspace(tmp_path):
    from app.agents.orchestrator import _project_a2a_artifact_refs_to_parent_session

    parent_agent_id = uuid4()
    child_agent_id = uuid4()
    parent_session_id = uuid4()
    runtime_task_id = uuid4()
    child_workspace = tmp_path / str(child_agent_id)
    child_report = child_workspace / "workspace" / "chapter9.md"
    child_report.parent.mkdir(parents=True)
    child_report.write_text("# Chapter 9\n", encoding="utf-8")

    projected = _project_a2a_artifact_refs_to_parent_session(
        artifact_parts=[
            {
                "type": "artifact",
                "artifact_id": "child-artifact-1",
                "path": "workspace/chapter9.md",
                "name": "chapter9.md",
                "owner_agent_id": str(child_agent_id),
                "source_agent_id": str(child_agent_id),
                "download_agent_id": str(child_agent_id),
            }
        ],
        data_root=tmp_path,
        parent_agent_id=parent_agent_id,
        parent_session_id=parent_session_id,
        runtime_task_id=runtime_task_id,
    )

    parent_report = tmp_path / str(parent_agent_id) / "workspace" / "chapter9.md"
    assert not parent_report.exists()
    assert len(projected) == 1
    assert projected[0]["path"] == "workspace/chapter9.md"
    assert projected[0]["owner_agent_id"] == str(child_agent_id)
    assert projected[0]["download_agent_id"] == str(child_agent_id)
    assert projected[0]["source_agent_id"] == str(child_agent_id)
    assert projected[0]["delivery_agent_id"] == str(parent_agent_id)
    assert projected[0]["source"] == "a2a_delivery_ref"
    assert projected[0]["source_artifact_id"] == "child-artifact-1"
    assert projected[0]["preview_snapshot_content"] == "# Chapter 9\n"


def test_a2a_delivery_projects_latest_duplicate_child_artifact_ref_only(tmp_path):
    from app.agents.orchestrator import _project_a2a_artifact_refs_to_parent_session

    parent_agent_id = uuid4()
    child_agent_id = uuid4()
    child_workspace = tmp_path / str(child_agent_id)
    child_report = child_workspace / "workspace" / "chapter9.md"
    child_report.parent.mkdir(parents=True)
    child_report.write_text("# Latest Chapter 9\n", encoding="utf-8")

    projected = _project_a2a_artifact_refs_to_parent_session(
        artifact_parts=[
            {
                "type": "artifact",
                "artifact_id": "old-artifact",
                "path": "workspace/chapter9.md",
                "owner_agent_id": str(child_agent_id),
            },
            {
                "type": "artifact",
                "artifact_id": "latest-artifact",
                "path": "workspace/chapter9.md",
                "owner_agent_id": str(child_agent_id),
            },
        ],
        data_root=tmp_path,
        parent_agent_id=parent_agent_id,
        parent_session_id=uuid4(),
        runtime_task_id=uuid4(),
    )

    assert len(projected) == 1
    assert projected[0]["source_artifact_id"] == "latest-artifact"
    assert projected[0]["download_agent_id"] == str(child_agent_id)
    assert projected[0]["preview_snapshot_content"] == "# Latest Chapter 9\n"
    assert not (tmp_path / str(parent_agent_id) / "workspace" / "chapter9.md").exists()


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
        ("delete_file", {"path": "workspace/deleted.md"}, ["workspace/deleted.md"]),
        ("fs_write", {"path": "workspace/c.md"}, ["workspace/c.md"]),
        ("office_document_create", {"path": "workspace/d.docx"}, ["workspace/d.docx"]),
        (
            "office_document_apply",
            {"output_path": "workspace/e.docx", "path": "workspace/src.docx"},
            ["workspace/e.docx"],
        ),
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


def test_tool_session_write_paths_consumes_structured_execution_artifacts():
    from app.services.chat_artifact_delivery import tool_session_write_paths

    assert tool_session_write_paths(
        "run_command",
        {"command": "python build.py"},
        artifacts=[
            {"path": "workspace/report.xlsx", "source": "run_command"},
            {"path": "../escape.txt", "source": "run_command"},
            {"path": "workspace/report.xlsx", "source": "run_command"},
        ],
    ) == ["workspace/report.xlsx"]


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
