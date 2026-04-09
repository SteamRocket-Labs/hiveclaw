"""Helpers for masked channel secret handling during updates."""

from __future__ import annotations


MASK_PREFIX = "****"


def is_masked_secret(value: object) -> bool:
    """Return True when a UI placeholder is sent back instead of the real secret."""
    return isinstance(value, str) and value.startswith(MASK_PREFIX)


def resolve_secret_value(
    value: str | None,
    existing: str | None = None,
    *,
    preserve_missing: bool = False,
) -> str | None:
    """Resolve an updated secret value while preserving masked placeholders."""
    if value is None:
        return existing if preserve_missing else None

    cleaned = value.strip()
    if is_masked_secret(cleaned) and existing is not None:
        return existing
    return cleaned


def resolve_secret_field(payload: dict, key: str, existing: str | None = None) -> str | None:
    """Resolve a secret field from a generic payload dict."""
    if key not in payload:
        return existing
    return resolve_secret_value(payload.get(key), existing)
