"""Every native AI-asset mutation must write the thin control-plane projection."""

import inspect


def _source(module_name: str, function_name: str) -> str:
    module = __import__(module_name, fromlist=[function_name])
    return inspect.getsource(getattr(module, function_name))


def test_agent_mutations_register_ai_asset_projection() -> None:
    for function_name in ("create_agent", "update_agent", "delete_agent"):
        source = _source("app.api.agents", function_name)
        assert "register_projection" in source, function_name
        assert "project_agent" in source, function_name


def test_every_agent_constructor_path_registers_control_plane_asset() -> None:
    constructors = (
        ("app.services.agent_seeder", "seed_default_agents"),
        ("app.services.local_bridge_service", "ensure_default_local_agent_for_pairing"),
        ("app.services.auto_provision", "ensure_main_agent"),
        ("app.api.agents", "get_or_create_hr_agent"),
        ("app.services.hr_provisioning_runner", "run_hr_provisioning"),
        ("app.api.desktop_agents", "create_sub_agent"),
    )
    for module_name, function_name in constructors:
        source = _source(module_name, function_name)
        assert "register_agent_asset" in source, f"{module_name}.{function_name}"


def test_desktop_agent_config_mutations_register_revisions() -> None:
    for function_name in ("update_sub_agent", "delete_sub_agent"):
        assert "register_agent_asset" in _source("app.api.desktop_agents", function_name)
    assert "register_agent_asset" in _source("app.api.agent_subagents", "set_evolution_mode")


def test_skill_mutations_register_ai_asset_projection() -> None:
    for function_name in ("create_skill", "update_skill", "delete_skill", "browse_write", "browse_delete"):
        source = _source("app.api.skills", function_name)
        assert "register_projection" in source, function_name
        assert "project_skill" in source, function_name


def test_workflow_lifecycle_registers_ai_asset_projection() -> None:
    from app.services.workflow_definitions import WorkflowDefinitionService

    assert "register_projection" in inspect.getsource(WorkflowDefinitionService.create_draft)
    assert "register_projection" in inspect.getsource(WorkflowDefinitionService._transition)
    assert "project_workflow" in inspect.getsource(WorkflowDefinitionService.create_draft)
    assert "project_workflow" in inspect.getsource(WorkflowDefinitionService._transition)


def test_external_capability_publish_and_revoke_register_projection() -> None:
    for function_name in ("approve_external_capability_snapshot", "revoke_external_capability_snapshot"):
        source = _source("app.services.external_capabilities.trust_gate", function_name)
        assert "register_projection" in source, function_name
        assert "project_external_capability" in source, function_name


def test_subagent_file_mutations_use_governed_asset_helpers() -> None:
    for function_name in ("put_agent_subagent", "approve_subagent_proposal", "put_tenant_subagent"):
        source = _source("app.api.agent_subagents", function_name)
        assert "_save_subagent_asset" in source or "_register_subagent_asset" in source, function_name
    for function_name in ("delete_agent_subagent", "delete_tenant_subagent"):
        source = _source("app.api.agent_subagents", function_name)
        assert "_delete_subagent_asset" in source, function_name


def test_auto_evolved_subagent_definition_registers_asset_revision() -> None:
    source = _source("app.agents.subagent_evolution", "maybe_nominate")
    assert "register_evolved_subagent_asset" in source


def test_auto_evolved_workspace_skill_registers_asset_revision() -> None:
    source = _source("app.services.skill_distillation_runner", "run_skill_distillation")
    assert "_commit_skill_with_asset_revision" in source
    helper = _source("app.services.skill_distiller", "_commit_skill_with_asset_revision")
    assert "register_evolved_workspace_skill_asset" in helper


def test_native_runtime_consumers_record_ai_asset_usage_evidence() -> None:
    from app.services.workflow_definitions import WorkflowDefinitionService
    from app.tools.service import ToolRuntimeService

    assert "record_asset_usage" in _source("app.services.invocation_trace", "record_invocation_span")
    assert "_record_resolved_asset_usage_for_tool" in inspect.getsource(ToolRuntimeService.execute_with_context)
    assert not hasattr(ToolRuntimeService, "_execute_without_governance")
    tool_usage_source = inspect.getsource(ToolRuntimeService._record_resolved_asset_usage_for_tool)
    assert "record_tool_asset_usage" in tool_usage_source
    assert "arguments.get" not in tool_usage_source
    assert "resolve_tool_asset_refs" in _source("app.tools.execution_pipeline", "run_tool_execution")
    assert "record_asset_usage" not in inspect.getsource(WorkflowDefinitionService.resolve_for_execution)
    assert "asset_ref" in inspect.getsource(WorkflowDefinitionService.resolve_for_execution)
    assert "record_asset_usage" not in _source(
        "app.services.external_capabilities.activation", "activate_external_extension_for_agent"
    )
    assert "record_asset_usage" not in _source(
        "app.services.external_capabilities.activation", "try_external_extension_in_chat"
    )


def test_file_native_assets_have_explicit_backfill_and_reconciliation_path() -> None:
    source = _source("app.services.ai_assets", "backfill_file_native_assets")
    assert "project_workspace_skill" in source
    assert "definition_store_for_agent" in source
    assert "definition_store_for_tenant" in source
    assert '"orphaned"' in source
