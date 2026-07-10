import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.api.agents import _agent_list_out_from_mapping


def test_agent_list_summary_omits_detail_heavy_fields_and_truncates_description():
    row = {
        "id": uuid.uuid4(),
        "name": "Researcher",
        "avatar_url": None,
        "role_description": "x" * 1200,
        "status": "running",
        "creator_id": uuid.uuid4(),
        "sponsor_user_id": uuid.uuid4(),
        "participant_id": uuid.uuid4(),
        "owner_user_id": None,
        "tenant_id": uuid.uuid4(),
        "primary_model_id": None,
        "fallback_model_id": None,
        "tokens_used_today": 17,
        "tokens_used_month": 29,
        "tokens_used_total": 31,
        "max_tool_rounds": 200,
        "max_triggers": 20,
        "min_poll_interval_min": 5,
        "webhook_rate_limit": 5,
        "last_heartbeat_at": None,
        "timezone": "Asia/Shanghai",
        "agent_type": "native",
        "agent_class": "internal_tenant",
        "security_zone": "standard",
        "execution_mode": "standard",
        "created_at": datetime.now(timezone.utc),
        "last_active_at": None,
        "deleted_at": None,
        "deactivated_at": None,
        "deactivation_reason": None,
    }

    payload = _agent_list_out_from_mapping(
        row,
        access_level="use",
        current_user_id=uuid.uuid4(),
    )

    assert len(payload.role_description) < len(row["role_description"])
    assert payload.bio is None
    assert payload.welcome_message is None
    assert payload.smart_model_routing is None
    assert payload.tokens_used_today == 17
    assert payload.tenant_id == row["tenant_id"]


def test_agent_list_route_uses_summary_projection_not_full_agent_rows():
    project_root = Path(__file__).resolve().parents[3]
    source = (project_root / "backend/app/api/agents.py").read_text(encoding="utf-8")
    list_source = source.split('@router.get("/", response_model=list[AgentOut])', 1)[1].split("HR_AGENT_NAME", 1)[0]

    assert "_agent_list_summary_stmt" in list_source
    assert "select(Agent)" not in list_source
