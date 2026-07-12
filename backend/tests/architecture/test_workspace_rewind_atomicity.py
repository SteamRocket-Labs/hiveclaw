from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_startup_recovers_workspace_restore_before_any_workspace_migration() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    recovery = source.index("await recover_workspace_restores_from_transcript()")
    migration = source.index("migrate_all_workspaces()")
    assert recovery < migration


def test_workspace_rewind_uses_scoped_evidence_and_deferred_atomic_finalize() -> None:
    command_source = (ROOT / "app" / "services" / "session_command_runtime.py").read_text(encoding="utf-8")
    api_source = (ROOT / "app" / "api" / "commands.py").read_text(encoding="utf-8")
    snapshot_source = (ROOT / "app" / "services" / "session_workspace_snapshot.py").read_text(encoding="utf-8")

    assert "_workspace_restore_scope_after_checkpoint" in command_source
    assert "expected_current_states=plan.states" in command_source
    assert "defer_finalize=True" in command_source
    assert "await asyncio.to_thread(\n        restore_session_workspace_snapshot" in command_source
    assert "finalize_workspace_restore" in api_source
    assert "os.replace(stage, workspace)" in snapshot_source
    assert "hive.workspace_restore_transaction.v1" in snapshot_source


def test_every_governed_mutating_tool_observes_the_agent_workspace_lock() -> None:
    tool_runtime = (ROOT / "app" / "tools" / "service.py").read_text(encoding="utf-8")

    assert "async_agent_workspace_lock" in tool_runtime
    assert "is_workspace_mutating_tool(tool_name)" in tool_runtime
    assert "async with async_agent_workspace_lock(context.agent_id)" in tool_runtime
    assert "workspace_mutation_evidence_captured" in tool_runtime
    assert "_capture_workspace_mutation_evidence" in tool_runtime


def test_non_tool_workspace_write_entrypoints_share_the_rewind_lock() -> None:
    files_api = (ROOT / "app" / "api" / "files.py").read_text(encoding="utf-8")
    upload_api = (ROOT / "app" / "api" / "upload.py").read_text(encoding="utf-8")
    office_api = (ROOT / "app" / "api" / "office.py").read_text(encoding="utf-8")

    assert files_api.count("async with async_agent_workspace_lock(agent_id)") >= 3
    assert "async with async_agent_workspace_lock(agent_id)" in upload_api
    assert "async with async_agent_workspace_lock(agent_id)" in office_api


def test_checkpoint_capture_never_blocks_the_async_web_runtime_on_workspace_lock() -> None:
    runtime = (ROOT / "app" / "services" / "web_chat_runtime.py").read_text(encoding="utf-8")

    assert "async def _capture_user_checkpoint_workspace_snapshot" in runtime
    assert "await asyncio.to_thread(\n            capture_session_workspace_snapshot" in runtime
    assert "await _capture_user_checkpoint_workspace_snapshot(" in runtime
