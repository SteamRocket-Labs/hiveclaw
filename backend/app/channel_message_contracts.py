"""Shared parsing helpers for channel-authored message prefixes."""

from __future__ import annotations

import re

_SENDER_PREFIX_RE = re.compile(r"^\[发送者[:：]\s*([^\]]+?)(?:\s*\(ID:.*?\))?\]\s*")


def extract_sender_label_from_message(content: str | None) -> str | None:
    match = _SENDER_PREFIX_RE.match((content or "").strip())
    if not match:
        return None
    label = match.group(1).strip()
    if "(" in label:
        label = label.split("(", 1)[0].strip()
    return label or None


def strip_sender_label_prefix(content: str | None) -> str:
    return _SENDER_PREFIX_RE.sub("", content or "", count=1)


def prefix_message_with_sender_label(
    content: str,
    *,
    sender_name: str | None,
    sender_id: str | None = None,
) -> str:
    normalized_name = (sender_name or "").strip()
    if not normalized_name:
        return content

    normalized_id = (sender_id or "").strip()
    id_part = f" (ID: {normalized_id})" if normalized_id else ""
    return f"[发送者: {normalized_name}{id_part}] {content}"
