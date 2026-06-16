from __future__ import annotations

from pathlib import Path
from uuid import uuid4


def test_update_session_memory_writes_structured_markdown(tmp_path: Path) -> None:
    from app.services.session_memory import SessionMemoryPayload, update_session_memory

    agent_id = uuid4()
    path = update_session_memory(
        agent_id,
        SessionMemoryPayload(
            session_id="session-123",
            source="response_complete",
            session_title="Claude 对标补齐",
            current_state="Comparing Clawith with external agents and fixing gaps.",
            task_spec="Implement eval runner, session continuity, skill loop, and team memory.",
            important_files=["backend/app/kernel/engine.py", "backend/app/runtime/task_eval.py"],
            workflow=["Write failing tests", "Implement minimum behavior", "Re-run full gates"],
            errors_corrections=["Earlier compaction summary omitted the latest pending work."],
            key_results=["Eval runner skeleton is in place."],
            pending_work=["Add eval CLI report writer", "Inject session memory during restore"],
            last_successful_step="Added failing tests for permission and session memory contracts.",
            worklog=["Wrote eval red tests.", "Started continuity integration."],
            compaction_count=2,
            last_compaction_at="2026-04-09T01:02:03Z",
        ),
        data_root=tmp_path,
    )

    content = path.read_text(encoding="utf-8")

    assert path == tmp_path / str(agent_id) / "runtime_artifacts" / "session_memory.md"
    assert not (tmp_path / str(agent_id) / "workspace" / "session_memory.md").exists()
    assert content.startswith("---\n")
    assert "version: 2" in content
    assert "session_id: session-123" in content
    assert "source: response_complete" in content
    assert "## Session Title" in content
    assert "## Current State" in content
    assert "## Task Specification" in content
    assert "## Files and Functions" in content
    assert "## Workflow" in content
    assert "## Errors & Corrections" in content
    assert "## Key Results" in content
    assert "## Pending Work" in content
    assert "## Worklog" in content
    assert "backend/app/kernel/engine.py" in content
    assert "Added failing tests for permission and session memory contracts." in content


def test_build_session_memory_payload_from_messages_extracts_latest_state() -> None:
    from app.services.session_memory import build_session_memory_payload_from_messages

    payload = build_session_memory_payload_from_messages(
        [
            {"role": "user", "content": "Please implement the eval runner and session continuity plan."},
            {"role": "assistant", "content": "I added the failing tests for the eval runner."},
            {"role": "assistant", "content": "Next I will wire the compaction restore path and the team memory API."},
        ],
        metadata={
            "important_files": ["backend/app/kernel/engine.py", "backend/app/api/memory.py"],
            "errors_corrections": ["Earlier summary did not mention team memory scope."],
        },
    )

    assert payload.task_spec.startswith("Please implement the eval runner")
    assert payload.current_state == "I added the failing tests for the eval runner."
    assert payload.pending_work == ["Next I will wire the compaction restore path and the team memory API."]
    assert payload.important_files == ["backend/app/kernel/engine.py", "backend/app/api/memory.py"]
    assert payload.errors_corrections == ["Earlier summary did not mention team memory scope."]
    assert payload.last_successful_step == "I added the failing tests for the eval runner."
    assert payload.session_title
    assert payload.worklog


def test_build_session_memory_payload_from_messages_detects_chinese_future_steps() -> None:
    from app.services.session_memory import build_session_memory_payload_from_messages

    payload = build_session_memory_payload_from_messages(
        [
            {"role": "user", "content": "请继续补齐 bakeoff 和 team memory。"},
            {"role": "assistant", "content": "我已经补上了 team memory 的后端接口。"},
            {"role": "assistant", "content": "下一步我会修复 bakeoff 的 timeout 评分。"},
            {"role": "assistant", "content": "然后修复前端删除确认。"},
        ],
    )

    assert payload.current_state == "我已经补上了 team memory 的后端接口。"
    assert payload.pending_work == [
        "下一步我会修复 bakeoff 的 timeout 评分。",
        "然后修复前端删除确认。",
    ]


def test_merge_session_memory_into_recovery_manifest_restores_pending_work(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import RecoveryManifest, merge_session_memory_into_manifest
    from app.services.session_memory import SessionMemoryPayload, update_session_memory

    agent_id = uuid4()
    update_session_memory(
        agent_id,
        SessionMemoryPayload(
            current_state="Compaction completed and restore is in progress.",
            task_spec="Keep continuity across compaction boundaries.",
            pending_work=["Re-inject session memory", "Verify long-context benchmark"],
            last_successful_step="Compaction summary persisted to runtime_artifacts/compaction_summary.md.",
            key_results=["Continuation restore logic was written before compaction."],
        ),
        data_root=tmp_path,
    )

    manifest = merge_session_memory_into_manifest(
        RecoveryManifest(session_id="session-1"),
        agent_id=agent_id,
        data_root=tmp_path,
    )

    assert "Re-inject session memory" in manifest.pending_items
    assert "Verify long-context benchmark" in manifest.pending_items
    assert any("Compaction completed and restore is in progress." in item for item in manifest.recent_tool_outcomes[0].values())
    assert any("Continuation restore logic was written before compaction." in item for item in manifest.recent_tool_outcomes[0].values())


def test_merge_session_memory_into_manifest_logs_load_failures(caplog) -> None:
    import app.runtime.recovery_manifest as recovery_manifest

    def boom(*args, **kwargs):
        raise OSError("corrupted session memory")

    original_loader = recovery_manifest.load_session_memory
    recovery_manifest.load_session_memory = boom  # type: ignore[assignment]
    try:
        with caplog.at_level("WARNING"):
            manifest = recovery_manifest.merge_session_memory_into_manifest(
                recovery_manifest.RecoveryManifest(session_id="session-1"),
                agent_id=uuid4(),
            )
    finally:
        recovery_manifest.load_session_memory = original_loader  # type: ignore[assignment]

    assert manifest.pending_items == []
    assert "Failed to load session memory" in caplog.text


def test_load_session_memory_supports_legacy_schema_without_frontmatter(tmp_path: Path) -> None:
    from app.services.session_memory import load_session_memory

    agent_id = uuid4()
    path = tmp_path / str(agent_id) / "workspace" / "session_memory.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Session Memory\n"
        "Updated At: 2026-04-09T00:00:00Z\n\n"
        "## Current State\n"
        "Legacy state.\n\n"
        "## Task Spec\n"
        "Legacy task.\n\n"
        "## Important Files / Artifacts\n"
        "- backend/app/runtime/prompt_builder.py\n\n"
        "## Pending Work\n"
        "- Restore session continuity.\n\n"
        "## Last Successful Step\n"
        "Recorded the previous summary.\n",
        encoding="utf-8",
    )

    payload = load_session_memory(agent_id, data_root=tmp_path)

    assert payload is not None
    assert payload.current_state == "Legacy state."
    assert payload.task_spec == "Legacy task."
    assert payload.important_files == ["backend/app/runtime/prompt_builder.py"]
    assert payload.pending_work == ["Restore session continuity."]
    assert payload.last_successful_step == "Recorded the previous summary."


def test_update_session_memory_migrates_legacy_workspace_file(tmp_path: Path) -> None:
    from app.services.session_memory import SessionMemoryPayload, update_session_memory

    agent_id = uuid4()
    legacy_path = tmp_path / str(agent_id) / "workspace" / "session_memory.md"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("# Session Memory\n\nlegacy", encoding="utf-8")

    new_path = update_session_memory(
        agent_id,
        SessionMemoryPayload(current_state="New runtime state.", task_spec="Keep runtime files out of workspace."),
        data_root=tmp_path,
    )

    assert new_path == tmp_path / str(agent_id) / "runtime_artifacts" / "session_memory.md"
    assert new_path.exists()
    assert not legacy_path.exists()


def test_update_session_memory_caps_lists_and_worklog(tmp_path: Path) -> None:
    from app.services.session_memory import SessionMemoryPayload, load_session_memory, update_session_memory

    agent_id = uuid4()
    update_session_memory(
        agent_id,
        SessionMemoryPayload(
            session_id="session-456",
            source="session_close",
            current_state="Closing out the session.",
            task_spec="Cap noisy sections.",
            important_files=[f"backend/app/file_{index}.py" for index in range(20)],
            pending_work=[f"pending {index}" for index in range(20)],
            worklog=[f"log entry {index} " + ("x" * 300) for index in range(25)],
        ),
        data_root=tmp_path,
    )

    payload = load_session_memory(agent_id, data_root=tmp_path)

    assert payload is not None
    assert len(payload.important_files) == 12
    assert len(payload.pending_work) == 10
    assert len(payload.worklog) == 20
    assert all(len(item) <= 200 for item in payload.worklog)
