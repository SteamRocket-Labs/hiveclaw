import inspect
import uuid
from datetime import datetime, timezone

from app.api.agents import _operator_agent_list_shell_from_mapping
from app.api.agents import _agent_list_out_from_mapping
from app.api.agents import _agent_list_summary_stmt
from app.api.agents import list_agents


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
    list_source = inspect.getsource(list_agents)

    assert "_agent_list_summary_stmt" in list_source
    assert "select(Agent)" not in list_source


def test_operator_only_list_shell_excludes_private_agent_fields():
    row = {
        "id": uuid.uuid4(),
        "name": "Private Agent",
        "avatar_url": None,
        "status": "running",
        "creator_id": uuid.uuid4(),
        "sponsor_user_id": uuid.uuid4(),
        "participant_id": uuid.uuid4(),
        "owner_user_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "primary_model_id": uuid.uuid4(),
        "tokens_used_today": 99,
        "security_zone": "restricted",
        "agent_type": "native",
        "agent_class": "internal_tenant",
    }

    payload = _operator_agent_list_shell_from_mapping(row).model_dump()

    assert payload["access_level"] == "operator"
    assert payload["operator_shell"] is True
    assert payload["action_capabilities"]["can_operator_inspect"] is True
    assert {
        "creator_id",
        "sponsor_user_id",
        "participant_id",
        "owner_user_id",
        "tenant_id",
        "primary_model_id",
        "tokens_used_today",
        "security_zone",
    }.isdisjoint(payload)


def test_agent_list_summary_includes_the_persisted_session_permission_default():
    selected_names = {column["name"] for column in _agent_list_summary_stmt().column_descriptions}

    assert "default_session_permission_mode" in selected_names
