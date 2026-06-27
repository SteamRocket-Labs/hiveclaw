from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


def test_build_a2a_agent_card_exposes_identity_auth_without_legacy_gateway() -> None:
    from app.services.interoperability import build_a2a_agent_card

    agent_id = uuid4()
    tenant_id = uuid4()
    participant_id = uuid4()
    sponsor_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Research Analyst",
        role_description="Investigates market evidence.",
        bio=None,
        tenant_id=tenant_id,
        participant_id=participant_id,
        sponsor_user_id=sponsor_id,
        security_zone="standard",
        agent_class="internal_tenant",
        status="running",
        deleted_at=None,
    )

    card = build_a2a_agent_card(agent, base_url="https://hive.example")

    assert card["protocol"] == "a2a"
    assert card["name"] == "Research Analyst"
    assert card["url"] == f"https://hive.example/api/v1/agents/{agent_id}/a2a-card"
    assert "gateway_poll" not in card["endpoints"]
    assert "gateway_report" not in card["endpoints"]
    assert "gateway_send_message" not in card["endpoints"]
    assert all(scheme.get("name") != "X-Api-Key" for scheme in card["authentication"]["schemes"])
    assert card["authentication"]["oauth_delegation"]["status"] == "not_exposed"
    assert card["capabilities"]["agent_to_agent_messages"] is True
    assert card["hive"]["agent_id"] == str(agent_id)
    assert card["hive"]["tenant_id"] == str(tenant_id)
    assert card["hive"]["participant_id"] == str(participant_id)
    assert card["hive"]["sponsor_user_id"] == str(sponsor_id)


def test_build_interoperability_profile_is_machine_readable_and_honest() -> None:
    from app.services.interoperability import build_interoperability_profile

    profile = build_interoperability_profile(base_url="https://hive.example")

    assert profile["platform"] == "hive"
    assert profile["standards"]["mcp"]["token_passthrough"] == "forbidden"
    assert profile["standards"]["mcp"]["oauth_authorization_client"]["status"] == "implemented"
    assert profile["standards"]["mcp"]["oauth_authorization_client"]["pkce"] == "S256"
    assert "PKCE authorization flow" not in profile["standards"]["mcp"]["oauth_resource_server"]["missing"]
    assert profile["standards"]["a2a"]["agent_card_endpoint"] == "/api/v1/agents/{agent_id}/a2a-card"
    assert profile["standards"]["a2a"]["json_rpc_tasks"]["status"] == "not_exposed"
    assert profile["standards"]["oauth_delegation"]["status"] == "not_exposed"
    assert "unsupported" not in profile["standards"]["a2a"]["json_rpc_tasks"]
    assert "gateway" not in str(profile).lower()
