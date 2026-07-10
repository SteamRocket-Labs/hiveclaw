from __future__ import annotations

from app.models.local_agent_channel import (
    LocalAgentChannel,
    LocalAgentChannelEvent,
    LocalAgentChannelMessage,
    LocalAgentChannelSession,
    LocalAgentChannelWsTicket,
)


EXPECTED_LOCAL_AGENT_CHANNEL_INDEXES = {
    LocalAgentChannel: {
        "ix_local_agent_channels_user_status",
        "ix_local_agent_channels_tenant_id",
    },
    LocalAgentChannelSession: {
        "ix_local_agent_channel_sessions_user_status",
        "ix_local_agent_channel_sessions_source_agent",
        "ix_local_agent_channel_sessions_chat_session",
        "ix_local_agent_channel_sessions_tenant_id",
    },
    LocalAgentChannelMessage: {
        "ix_local_agent_channel_messages_session_status",
        "ix_local_agent_channel_messages_user_status",
        "ix_local_agent_channel_messages_source_agent",
        "ix_local_agent_channel_messages_tenant_id",
        "ix_local_agent_channel_messages_request_hash",
        "ix_local_agent_channel_messages_capability_snapshot_hash",
        "ix_local_agent_channel_messages_replay_key",
    },
    LocalAgentChannelEvent: {
        "ix_local_agent_channel_events_session_created",
        "ix_local_agent_channel_events_session_sequence",
        "ix_local_agent_channel_events_message",
        "ix_local_agent_channel_events_tenant_id",
    },
    LocalAgentChannelWsTicket: {
        "ix_local_agent_channel_ws_tickets_connection",
        "ix_local_agent_channel_ws_tickets_expires",
        "ix_local_agent_channel_ws_tickets_tenant_id",
    },
}


def test_local_agent_channel_models_have_no_duplicate_index_names() -> None:
    for model in EXPECTED_LOCAL_AGENT_CHANNEL_INDEXES:
        names = [idx.name for idx in model.__table__.indexes]
        assert len(names) == len(set(names)), f"{model.__tablename__} has duplicate indexes: {names}"


def test_local_agent_channel_model_indexes_match_migration_contract() -> None:
    for model, expected_names in EXPECTED_LOCAL_AGENT_CHANNEL_INDEXES.items():
        names = {idx.name for idx in model.__table__.indexes}
        assert names == expected_names, f"{model.__tablename__} metadata indexes drifted from migration contract"
