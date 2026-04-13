from pathlib import Path


def test_gateway_report_result_does_not_write_legacy_gw_agent_conversation_ids():
    source = Path("/Users/rocky243/vc-saas/hiveclaw/backend/app/api/gateway.py").read_text(encoding="utf-8")

    assert 'msg.conversation_id or f"gw_agent_{msg.sender_agent_id}_{agent.id}"' not in source
    assert "find_or_create_agent_pair_session(" in source
