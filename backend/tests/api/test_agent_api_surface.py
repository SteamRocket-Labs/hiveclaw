from pathlib import Path
import pytest
from pydantic import ValidationError


def test_agent_and_template_api_surface_no_longer_exposes_legacy_autonomy_fields():
    project_root = Path(__file__).resolve().parents[3]
    schemas_source = (project_root / "backend/app/schemas/schemas.py").read_text()
    agent_create_source = schemas_source.split("class AgentCreate(BaseModel):", 1)[1].split(
        "class AgentOut(BaseModel):", 1
    )[0]
    agents_api_source = (project_root / "backend/app/api/agents.py").read_text()
    advanced_api_source = (project_root / "backend/app/api/advanced.py").read_text()
    model_source = (project_root / "backend/app/models/agent.py").read_text()
    main_source = (project_root / "backend/app/main.py").read_text()
    approval_service_path = project_root / "backend/app/services/approval_service.py"
    autonomy_service_path = project_root / "backend/app/services/autonomy_service.py"
    bootstrap_service_path = project_root / "backend/app/services/agent_bootstrap_service.py"

    assert "autonomy_policy:" not in schemas_source
    assert "class AgentBootstrapCreate" not in schemas_source
    assert "class AgentBootstrapOut" not in schemas_source
    assert "default_autonomy_policy" not in advanced_api_source
    assert '"default_autonomy_policy"' not in agents_api_source
    assert "if data.autonomy_policy" not in agents_api_source
    assert "autonomy_policy" not in model_source
    assert "default_autonomy_policy" not in model_source
    assert not (project_root / "backend/app/services/template_seeder.py").exists()
    assert approval_service_path.exists()
    assert not autonomy_service_path.exists()
    assert 'agent_type: str = "native"' not in agent_create_source
    assert "template_id: uuid.UUID | None = None" not in agent_create_source
    assert 'agent_class: AgentClass = "internal_tenant"' in agent_create_source
    assert "template_id=data.template_id" not in agents_api_source
    assert 'if agent.agent_type == "openclaw":' not in agents_api_source
    assert 'agent_type=data.agent_type or "native"' not in agents_api_source
    assert '@router.post("/bootstrap"' not in agents_api_source
    assert "configure_bootstrap_channels" not in agents_api_source
    assert not bootstrap_service_path.exists()
    assert '@router.get("/templates"' not in advanced_api_source
    assert "seed_agent_templates" not in main_source


def test_agent_create_schema_rejects_legacy_agent_class_value():
    from app.schemas.schemas import AgentCreate

    with pytest.raises(ValidationError):
        AgentCreate(name="测试员工", agent_class="general")

    payload = AgentCreate(name="测试员工", agent_class="internal_tenant")
    assert payload.agent_class == "internal_tenant"


def test_agent_create_accepts_role_descriptions_up_to_prompt_guard_limit():
    from app.schemas.schemas import AgentCreate

    payload = AgentCreate(name="测试员工", role_description="x" * 4000)
    assert len(payload.role_description) == 4000

    with pytest.raises(ValidationError):
        AgentCreate(name="测试员工", role_description="x" * 4001)


def test_agent_role_description_is_text_at_persistence_layer():
    from sqlalchemy import Text

    from app.models.agent import Agent

    assert isinstance(Agent.__table__.c.role_description.type, Text)


def test_agent_schemas_expose_smart_model_routing_config():
    from app.schemas.schemas import AgentCreate, SmartModelRoutingConfig

    payload = AgentCreate(
        name="测试员工",
        smart_model_routing=SmartModelRoutingConfig(enabled=True, max_simple_chars=120, max_simple_words=18),
    )

    assert payload.smart_model_routing is not None
    assert payload.smart_model_routing.enabled is True
    assert payload.smart_model_routing.max_simple_chars == 120
    assert payload.smart_model_routing.max_simple_words == 18


@pytest.mark.parametrize("mode", ["default", "auto", "bypassPermissions"])
def test_agent_update_accepts_all_session_permission_defaults(mode: str):
    from app.schemas.schemas import AgentUpdate

    payload = AgentUpdate(default_session_permission_mode=mode)

    assert payload.default_session_permission_mode == mode


def test_agent_update_rejects_unknown_session_permission_default():
    from app.schemas.schemas import AgentUpdate

    with pytest.raises(ValidationError):
        AgentUpdate(default_session_permission_mode="unrestricted")


def test_agent_model_persists_session_permission_default():
    from app.models.agent import Agent

    column = Agent.__table__.c.default_session_permission_mode
    assert column.nullable is False
    assert str(column.server_default.arg) == "default"


def test_agent_full_access_default_uses_existing_manage_authority_without_role_override():
    project_root = Path(__file__).resolve().parents[3]
    agents_api_source = (project_root / "backend/app/api/agents.py").read_text(encoding="utf-8")

    assert "_validate_default_session_permission_mode_update" not in agents_api_source
    assert 'if access_level != "manage"' in agents_api_source


def test_agent_runtime_surface_removes_legacy_role_and_message_cleanup_paths():
    project_root = Path(__file__).resolve().parents[3]
    agents_api_source = (project_root / "backend/app/api/agents.py").read_text(encoding="utf-8")
    trigger_source = (project_root / "backend/app/services/trigger_daemon.py").read_text(encoding="utf-8")

    assert '"super_admin"' not in agents_api_source
    assert "sender_agent_id = NULL" not in agents_api_source
    assert 'role="tool_result"' not in trigger_source


def test_openclaw_gateway_surface_is_retired_from_backend_contract():
    project_root = Path(__file__).resolve().parents[3]
    schemas_source = (project_root / "backend/app/schemas/schemas.py").read_text(encoding="utf-8")
    agents_api_source = (project_root / "backend/app/api/agents.py").read_text(encoding="utf-8")
    agent_model_source = (project_root / "backend/app/models/agent.py").read_text(encoding="utf-8")
    main_source = (project_root / "backend/app/main.py").read_text(encoding="utf-8")
    local_bridge_source = (project_root / "backend/app/api/local_bridge.py").read_text(encoding="utf-8")
    local_bridge_service_source = (project_root / "backend/app/services/local_bridge_service.py").read_text(
        encoding="utf-8"
    )

    assert not (project_root / "backend/app/api/gateway.py").exists()
    assert not (project_root / "backend/app/models/gateway_message.py").exists()
    assert "gateway_router" not in main_source
    assert "app.api.gateway" not in main_source
    assert "GatewayMessage" not in schemas_source
    assert "GatewayPollResponse" not in schemas_source
    assert "GatewayReportRequest" not in schemas_source
    assert "GatewaySendMessageRequest" not in schemas_source
    assert "openclaw_last_seen" not in schemas_source
    assert "openclaw_last_seen" not in agent_model_source
    assert "api_key_hash" not in agent_model_source
    assert "openclaw" not in agent_model_source.lower()
    assert '@router.post("/{agent_id}/api-key")' not in agents_api_source
    assert '@router.get("/{agent_id}/gateway-messages")' not in agents_api_source
    assert "GatewayMessage" not in local_bridge_source
    assert "local-bridge/work-requests" not in local_bridge_source
    assert "create_local_bridge_work_request" not in local_bridge_service_source
