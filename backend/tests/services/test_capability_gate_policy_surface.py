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
        "get_current_time": "system.time.read",
        "manage_tasks": "agent.task.modify",
        "feishu_task_create": "channel.feishu.task",
        "feishu_task_complete": "channel.feishu.task",
        "feishu_task_comment": "channel.feishu.task",
        "feishu_doc_create": "channel.feishu.document",
        "feishu_doc_append": "channel.feishu.document",
        "feishu_doc_share": "channel.feishu.document",
        "feishu_doc_delete": "channel.feishu.document",
        "feishu_base_app_create": "channel.feishu.base",
        "feishu_base_field_create": "channel.feishu.base",
        "feishu_base_record_upsert": "channel.feishu.base",
        "feishu_base_record_upload_attachment": "channel.feishu.base",
        "feishu_base_record_delete": "channel.feishu.base",
    }

    for tool_name, capability in expected.items():
        assert CAPABILITY_MAP.get(tool_name) == capability


def test_capability_map_covers_finance_pack_tools():
    expected = {
        "finance_get_provider_status": "finance.data.read",
        "finance_resolve_entity": "finance.data.read",
        "finance_get_source_ledger": "finance.data.read",
        "finance_get_price_history": "finance.data.read",
        "finance_get_financial_statements": "finance.data.read",
        "finance_search_filings": "finance.data.read",
        "finance_get_filing": "finance.data.read",
        "finance_get_ipo_pipeline": "finance.primary_market.read",
        "finance_get_funding_rounds": "finance.primary_market.read",
        "finance_get_company_registry": "finance.registry.read",
        "finance_compute_dcf": "finance.analysis.run",
        "finance_build_comps": "finance.analysis.run",
        "finance_compile_research_packet": "finance.analysis.run",
        "finance_run_workflow": "finance.analysis.run",
    }

    for tool_name, capability in expected.items():
        assert CAPABILITY_MAP.get(tool_name) == capability


def test_capability_definitions_expose_policy_capabilities_for_frontend():
    definitions = {item["capability"]: set(item["tools"]) for item in get_all_capabilities()}

    assert "workspace.file.read" in definitions
    assert "workspace.file.write" in definitions
    assert "workspace.file.delete" in definitions
    assert "workspace.command.execute" in definitions
    assert "workspace.command.dangerous" in definitions
    assert "workspace.command.secret_exfiltration" in definitions
    assert "agent.task.modify" in definitions
    assert "channel.feishu.document" in definitions
    assert "channel.feishu.base" in definitions
    assert "run_command" in definitions["workspace.command.execute"]
    assert "run_command" in definitions["workspace.command.dangerous"]
    assert "run_command" in definitions["workspace.command.secret_exfiltration"]
    assert "feishu_doc_delete" in definitions["channel.feishu.document"]
    assert "feishu_base_record_delete" in definitions["channel.feishu.base"]
