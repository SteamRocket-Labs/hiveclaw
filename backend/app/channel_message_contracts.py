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
