from pathlib import Path

import pytest
from pydantic import ValidationError


def test_agent_and_template_api_surface_no_longer_exposes_legacy_autonomy_fields():
    project_root = Path(__file__).resolve().parents[3]
    schemas_source = (project_root / "backend/app/schemas/schemas.py").read_text()
    agent_create_source = schemas_source.split("class AgentCreate(BaseModel):", 1)[1].split("class AgentOut(BaseModel):", 1)[0]
    agents_api_source = (project_root / "backend/app/api/agents.py").read_text()
    advanced_api_source = (project_root / "backend/app/api/advanced.py").read_text()
    model_source = (project_root / "backend/app/models/agent.py").read_text()
    main_source = (project_root / "backend/app/main.py").read_text()
    template_seeder_source = (project_root / "backend/app/services/template_seeder.py").read_text()
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
    assert "default_autonomy_policy" not in template_seeder_source
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


def test_agent_runtime_surface_removes_legacy_role_and_message_cleanup_paths():
    project_root = Path(__file__).resolve().parents[3]
    agents_api_source = (project_root / "backend/app/api/agents.py").read_text(encoding="utf-8")
    trigger_source = (project_root / "backend/app/services/trigger_daemon.py").read_text(encoding="utf-8")

    assert '"super_admin"' not in agents_api_source
    assert "sender_agent_id = NULL" not in agents_api_source
    assert 'role="tool_result"' not in trigger_source
