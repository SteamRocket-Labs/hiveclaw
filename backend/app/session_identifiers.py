"""Canonical session identifier helpers shared by runtime and maintenance paths."""

from __future__ import annotations

import uuid


def build_legacy_gateway_conversation_ids(
    source_agent_id: uuid.UUID | str,
    target_agent_id: uuid.UUID | str,
) -> tuple[str, str]:
    source = str(source_agent_id)
    target = str(target_agent_id)
    return (
        f"gw_agent_{source}_{target}",
        f"gw_agent_{target}_{source}",
    )


def parse_legacy_gateway_conversation_id(conversation_id: str | None) -> tuple[str, str] | None:
    if not conversation_id or not conversation_id.startswith("gw_agent_"):
        return None

    raw_pair = conversation_id.removeprefix("gw_agent_")
    try:
        source_raw, target_raw = raw_pair.split("_", 1)
        uuid.UUID(source_raw)
        uuid.UUID(target_raw)
    except (ValueError, AttributeError, TypeError):
        return None
    return source_raw, target_raw


def build_feishu_p2p_conv_id(provider_user_id: str | None = None, provider_open_id: str | None = None) -> str | None:
    stable_id = (provider_user_id or "").strip() or (provider_open_id or "").strip()
    if not stable_id:
        return None
    return f"feishu_p2p_{stable_id}"


def parse_feishu_p2p_conv_id(external_conv_id: str | None) -> str | None:
    conv_id = (external_conv_id or "").strip()
    if not conv_id.startswith("feishu_p2p_"):
        return None
    identifier = conv_id[len("feishu_p2p_") :]
    return identifier or None


def list_legacy_feishu_conv_ids(provider_open_id: str | None, canonical_conv_id: str | None = None) -> list[str]:
    open_id = (provider_open_id or "").strip()
    if not open_id:
        return []
    legacy = f"feishu_p2p_{open_id}"
    if canonical_conv_id and legacy == canonical_conv_id:
        return []
    return [legacy]


def build_feishu_session_lookup_ids(
    *,
    provider_user_id: str | None,
    provider_open_id: str | None,
    chat_type: str = "p2p",
    chat_id: str | None = None,
) -> tuple[str, list[str]]:
    if chat_type == "group" and chat_id:
        return f"feishu_group_{chat_id}", []

    conv_id = build_feishu_p2p_conv_id(provider_user_id, provider_open_id) or f"feishu_p2p_{provider_open_id or provider_user_id}"
    return conv_id, list_legacy_feishu_conv_ids(provider_open_id, conv_id)
