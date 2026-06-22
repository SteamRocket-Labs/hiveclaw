from app.services.capability_gate import CAPABILITY_MAP, get_all_capabilities
from app.tools.collector import collect_tools


def test_capability_map_covers_all_governance_classified_tools():
    collected = collect_tools()
    governance_tools = set(collected.safe_tools) | set(collected.sensitive_tools)

    missing = sorted(tool for tool in governance_tools if tool not in CAPABILITY_MAP)

    assert missing == []


def test_capability_mapping_audit_has_no_runtime_registry_drift():
    from app.services.capability_gate import audit_capability_mapping

    drift = audit_capability_mapping()

    assert drift == {"unmapped": [], "stale": []}


def test_capability_map_covers_all_core_tools_when_strict_default_is_on():
    from app.services.agent_tools import CORE_TOOL_NAMES

    missing = sorted(tool for tool in CORE_TOOL_NAMES if tool not in CAPABILITY_MAP)

    assert missing == []


def test_safe_governance_tools_are_read_only():
    collected = collect_tools()
    definitions = {tool["function"]["name"]: tool["function"]["description"] for tool in collected.openai_tools}
    non_read_only_safe = sorted(tool for tool in collected.safe_tools if tool not in collected.read_only_names)

    assert non_read_only_safe == [], definitions


def test_capability_map_covers_agent_settings_controls_and_destructive_feishu_tools():
    expected = {
        "list_files": "workspace.file.read",
        "read_file": "workspace.file.read",
        "read_document": "workspace.file.read",
        "write_file": "workspace.file.write",
        "edit_file": "workspace.file.write",
        "delete_file": "workspace.file.delete",
        "execute_code": "workspace.code.execute",
        "run_command": "workspace.command.execute",
        "send_email": "channel.email.send",
        "reply_email": "channel.email.send",
        "import_mcp_server": "agent.tool.install",
        "send_feishu_message": "channel.feishu.message",
        "web_search": "external.web.search",
        "bing_search": "external.web.search",
        "exa_search": "external.web.search",
        "tavily_search": "external.web.search",
        "get_current_time": "system.time.read",
        "feishu_task_create": "channel.feishu.task",
        "feishu_task_complete": "channel.feishu.task",
        "feishu_task_comment": "channel.feishu.task",
        "feishu_doc_create": "channel.feishu.document",
        "feishu_doc_append": "channel.feishu.document",
        "feishu_doc_share": "channel.feishu.document",
        "feishu_doc_delete": "channel.feishu.document",
        "feishu_url_resolve": "channel.feishu.document",
        "feishu_url_read": "channel.feishu.document",
        "feishu_drive_file_read": "channel.feishu.document",
        "feishu_base_app_create": "channel.feishu.base",
        "feishu_base_field_create": "channel.feishu.base",
        "feishu_base_record_upsert": "channel.feishu.base",
        "feishu_base_record_upload_attachment": "channel.feishu.base",
        "feishu_base_record_delete": "channel.feishu.base",
    }

    for tool_name, capability in expected.items():
        assert CAPABILITY_MAP.get(tool_name) == capability


def test_capability_map_covers_deep_research_pack_tools():
    expected = {
        "deep_research_run": "research.deep.run",
        "deep_research_start": "research.deep.run",
        "deep_research_check": "research.deep.read",
        "deep_research_export": "research.deep.read",
        "deep_research_cancel": "research.deep.modify",
    }

    for tool_name, capability in expected.items():
        assert CAPABILITY_MAP.get(tool_name) == capability


def test_capability_map_covers_subagent_spawn_tool():
    assert CAPABILITY_MAP.get("spawn_subagent") == "agent.subagent.spawn"


def test_capability_map_covers_cc_codex_command_tools():
    expected = {
        "task_create": "agent.task.track",
        "task_update": "agent.task.track",
        "task_list": "agent.task.read",
        "task_get": "agent.task.read",
        "task_output": "agent.async_task.read",
        "task_stop": "agent.async_task.modify",
        "goal_start": "agent.goal.modify",
        "team_create": "agent.team.modify",
        "advanced_plan": "agent.plan.modify",
    }

    for tool_name, capability in expected.items():
        assert CAPABILITY_MAP.get(tool_name) == capability


def test_capability_map_covers_memory_write_controls():
    assert CAPABILITY_MAP.get("save_memory") == "agent.memory.write"
    assert CAPABILITY_MAP.get("update_memory") == "agent.memory.write"
    assert CAPABILITY_MAP.get("retire_memory") == "agent.memory.write"


def test_capability_definitions_expose_policy_capabilities_for_frontend():
    definitions = {item["capability"]: set(item["tools"]) for item in get_all_capabilities()}

    assert "workspace.file.read" in definitions
    assert "workspace.file.write" in definitions
    assert "workspace.file.delete" in definitions
    assert "workspace.command.execute" in definitions
    assert "workspace.command.dangerous" in definitions
    assert "workspace.command.secret_exfiltration" in definitions
    # F-2: agent.task.modify retired with manage_tasks; the surviving agent
    # task-domain capability is agent.task.track (track_todo / record_finding).
    assert "agent.task.track" in definitions
    assert "channel.feishu.document" in definitions
    assert "channel.feishu.base" in definitions
    assert "run_command" in definitions["workspace.command.execute"]
    assert "run_command" in definitions["workspace.command.dangerous"]
    assert "run_command" in definitions["workspace.command.secret_exfiltration"]
    assert "feishu_doc_delete" in definitions["channel.feishu.document"]
    assert "feishu_base_record_delete" in definitions["channel.feishu.base"]
