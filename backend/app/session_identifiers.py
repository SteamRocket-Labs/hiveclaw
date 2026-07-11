"""Canonical session identifier helpers shared by runtime and maintenance paths."""

from __future__ import annotations

import uuid


_AGENT_PAIR_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def canonicalize_agent_pair_ids(
    source_agent_id: uuid.UUID | str,
    target_agent_id: uuid.UUID | str,
) -> tuple[uuid.UUID | str, uuid.UUID | str]:
    return tuple(sorted((source_agent_id, target_agent_id), key=str))


def build_agent_pair_session_id(
    source_agent_id: uuid.UUID | str,
    target_agent_id: uuid.UUID | str,
    *,
    owner_user_id: uuid.UUID | str | None = None,
    root_session_id: uuid.UUID | str | None = None,
) -> uuid.UUID:
    first, second = canonicalize_agent_pair_ids(source_agent_id, target_agent_id)
    authority_suffix = ""
    if owner_user_id is not None or root_session_id is not None:
        authority_suffix = f"|owner:{owner_user_id or ''}|root:{root_session_id or ''}"
    return uuid.uuid5(_AGENT_PAIR_NAMESPACE, f"{first}_{second}{authority_suffix}")


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

    conv_id = build_feishu_p2p_conv_id(provider_user_id, provider_open_id) or (
        f"feishu_p2p_{provider_open_id or provider_user_id}"
    )
    return conv_id, list_legacy_feishu_conv_ids(provider_open_id, conv_id)
