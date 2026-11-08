"""Member copy and file visibility on the native tool paths."""

import json
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest


def payload(text):
    return json.loads(str(text).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])


def test_default_assistant_identity_uses_the_chinese_ui_name():
    from app.runtime.prompt_sections.identity import build_identity_section

    section = build_identity_section("My Assistant", soul_text="I am My Assistant.", member_assistant=True)
    assert 'The member sees you as "我的助理" in the Chinese interface or "My Assistant" in the English interface' in section
    assert section.index('The member sees you as "我的助理"') > section.index("### Personality")
    custom = build_identity_section("周报助手")
    assert 'Your current display name is "周报助手".' in custom


ERROR_CLASSES = (
    "provider_bad_request", "auth_or_permission", "quota_or_billing", "not_found", "timeout",
    "rate_limited", "provider_unavailable", "provider_error", "tool_execution_error", "bad_arguments",
    "authority_context_unavailable", "backend_unavailable", "approval_required", "cancelled",
    "configuration_required", "invalid_tool_arguments", "operation_failed", "empty_content",
    "incomplete_content", "invalid_input", "unsupported_file_type", "unreadable_pdf", "wrong_tool",
    "provider_bad_response", "provider_empty_response", "provider_not_configured", "not_configured",
    "auth_required", "forbidden", "authz_policy_violation", "bad_state", "invalid_arguments",
    "invalid_approval_form", "missing_handoff_authority", "approval_asset_revision_drift",
    "approval_hook_block", "approval_payload_mutation", "approval_ticket_unavailable",
    "extension_disabled", "governance_dependency_unavailable", "runtime_budget_service_unavailable",
    "plan_mode_readonly_violation", "transport_failure", "unauthorized_secret_bytes",
    "custom_api_connector_error", "custom_api_request_error", "future_error_class",
)


@pytest.mark.parametrize("error_class", ERROR_CLASSES)
def test_tool_error_classes_offer_chinese_messages_and_hints(error_class):
    from app.tools.result_envelope import render_tool_error

    result = render_tool_error(tool_name="run_command", error_class=error_class,
        message="LLMError: ReadTimeout workspace/private.txt", http_status=403,
        actionable_hint="Check worker explicit_private_id before retrying.")
    error = payload(result)
    assert re.search("[一-鿿]", error["message"])
    assert re.search("[一-鿿]", error["actionable_hint"])
    assert not re.search(r"[A-Za-z_/<>]", error["message"] + error["actionable_hint"])
    assert error["error_class"] == error_class
    assert error["tool_name"] == "run_command"
    assert error["http_status"] == 403


def test_timeout_preserves_duration_and_unknown_outcome():
    from app.services.runtime_tool_timeout_sources import _timeout_text

    error = payload(_timeout_text("run_command", 120))
    assert error["message"] == "操作超过 120 秒没有完成，结果未知。"
    assert error["timeout_seconds"] == 120
    assert error["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_fields", [False, True])
async def test_memory_lookup_failure_message_is_readable_chinese(monkeypatch, invalid_fields):
    from app.tools.handlers import memory

    async def denied(*args, **kwargs):
        raise memory.SessionRecallError("content_source_access_denied", "403 content_source_access_denied", status="denied")

    monkeypatch.setattr(memory, "search_session_history", denied)
    args = {"query": "本周工作", "scope": "sessions"}
    if invalid_fields:
        args["reader_override"] = "untrusted"
    result = await memory.search_memory(uuid4(), args)
    assert "\\u" not in result
    error = json.loads(result.split("\n", 1)[-1] if result.startswith("##") else result)
    assert re.search("[一-鿿]", error["message"])
    assert error["status"] == ("invalid_arguments" if invalid_fields else "denied")
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_content_first_memory_keeps_exact_references_for_update_and_retire(monkeypatch, tmp_path):
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.tools.handlers import memory

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    from tests.tools.test_memory_handler import _safe_memory_threat_classifier

    monkeypatch.setattr("app.memory.write_gate.classify_memory_write_threat_with_llm", _safe_memory_threat_classifier)
    agent = uuid4()
    fact = "用户的测试代号是白鹭五号。"
    await memory.save_memory(agent, {"content": fact, "category": "feedback"})
    found = await memory.search_memory(agent, {"query": "白鹭", "scope": "facts"})
    original = re.search(r"id=([a-zA-Z0-9_-]+)", found).group(1)
    assert found.index(fact) < found.index(original)
    assert "source=" not in found
    assert "不要向用户展示" in found
    loaded = memory.load_memory(agent, {"ids": [original]})
    assert loaded.index(fact) < loaded.index(original)
    assert "source=" not in loaded
    assert "不要向用户展示" in loaded
    await memory.update_memory(agent, {"memory_id": original, "content": "测试代号改成白鹭六号。", "category": "feedback"})
    updated = await memory.search_memory(agent, {"query": "白鹭", "scope": "facts"})
    current = re.search(r"id=([a-zA-Z0-9_-]+)", updated).group(1)
    assert current != original
    assert fact not in updated
    assert "白鹭六号" in memory.load_memory(agent, {"ids": [current]})
    await memory.retire_memory(agent, {"memory_id": current, "reason": "用户要求忘记"})
    assert "白鹭六号" not in await memory.search_memory(agent, {"query": "白鹭", "scope": "facts"})
    assert {entry.status for entry in load_explicit_overlay_entries(tmp_path, agent)} == {"retired"}


INTERNAL_FILES = (
    "memory/.staging/t2_jobs/job/platform_gate_report.json",
    "memory/.staging/t2_episode_jobs/job/source_bundle.json",
    "memory/.staging/t3_jobs/job/consolidation_pitch.md",
    "memory/.archive/t2/sessions/session/summary.md",
    "memory/.rollback/soul/old.md",
    "memory/.dream_workspace/scratch.md",
    "memory/learnings/legacy.md",
    "memory/control/t2_job_sweep.json",
    "memory/indexes/wiki_map.md",
    "memory/retired_artifacts/memory.json",
    "memory/lifecycle.json",
    "memory/distillation_audit.jsonl",
    "memory/archive.md",
    "memory/memory.sqlite3",
    "memory/memory.json",
)


@pytest.mark.parametrize("path", INTERNAL_FILES)
def test_file_tools_hide_platform_memory_records_but_keep_user_files(tmp_path, path):
    from app.tools.handlers.filesystem import fs_list, fs_read, read_file, glob_search, grep_search

    internal = tmp_path / path
    internal.parent.mkdir(parents=True)
    internal.write_text("PLATFORM_PIPELINE_NEEDLE")
    normal_paths = ("workspace/report.md", "workspace/platform_gate_report.json", "memory/knowledge/project.md",
        "memory/self/self.md", "memory/profiles/owner.md", "memory/milestones/launch.md")
    for normal in normal_paths:
        file = tmp_path / normal
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("USER_WORK_NEEDLE")
        assert "USER_WORK_NEEDLE" in str(fs_read(tmp_path, {"path": normal}))
    for reader in (read_file, fs_read):
        error = payload(reader(tmp_path, {"path": path}))
        assert error["error_class"] == "auth_or_permission"
        case_alias = path.upper()
        if (tmp_path / case_alias).exists():
            assert payload(reader(tmp_path, {"path": case_alias}))["error_class"] == "auth_or_permission"
    assert payload(fs_list(tmp_path, {"path": path}))['ok'] is False
    listing = str(fs_list(tmp_path, {"path": "memory"}))
    assert path.split("/")[1] not in listing
    globbed = str(glob_search(tmp_path, {"pattern": "**/*"}))
    assert path not in globbed
    assert all(normal in globbed for normal in normal_paths)
    assert "PLATFORM_PIPELINE_NEEDLE" not in str(grep_search(tmp_path, {"pattern": "NEEDLE"}))
    assert "USER_WORK_NEEDLE" in str(grep_search(tmp_path, {"pattern": "NEEDLE"}))
    assert payload(fs_read(tmp_path, {"mode": "grep", "path": path, "pattern": "NEEDLE"}))['ok'] is False
    alias = tmp_path / "pipeline-alias"
    alias.symlink_to(internal)
    assert payload(fs_read(tmp_path, {"path": "pipeline-alias"}))['ok'] is False
