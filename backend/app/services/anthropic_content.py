"""Pure provider-neutral content conversion for Anthropic Messages API."""

from __future__ import annotations

import base64
import binascii
from typing import Any


_ALLOWED_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_ALLOWED_DOCUMENT_MEDIA_TYPES = frozenset({"application/pdf", "text/plain"})
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 32 * 1024 * 1024


class AnthropicContentError(ValueError):
    """Provider-neutral content cannot be represented safely by Anthropic."""


def _validate_base64(*, block_type: str, media_type: str, data: Any) -> str:
    if not isinstance(data, str) or not data:
        raise AnthropicContentError(f"Anthropic {block_type} data must be non-empty base64")
    allowed = _ALLOWED_IMAGE_MEDIA_TYPES if block_type == "image" else _ALLOWED_DOCUMENT_MEDIA_TYPES
    if media_type not in allowed:
        raise AnthropicContentError(f"Unsupported Anthropic {block_type} media type: {media_type or '<empty>'}")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AnthropicContentError(f"Anthropic {block_type} data is not valid base64") from exc
    limit = _MAX_IMAGE_BYTES if block_type == "image" else _MAX_DOCUMENT_BYTES
    if not decoded or len(decoded) > limit:
        raise AnthropicContentError(f"Anthropic {block_type} exceeds the {limit}-byte inline limit")
    return data


def _parse_data_url(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or not value.startswith("data:"):
        raise AnthropicContentError("Remote image URLs are not enabled for the Anthropic content contract")
    header, separator, data = value.partition(",")
    if not separator or not header.endswith(";base64"):
        raise AnthropicContentError("Anthropic image URL must be an inline base64 data URL")
    media_type = header[len("data:") : -len(";base64")].lower()
    return media_type, data


def _with_cache_control(mapped: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    cache_control = source.get("cache_control")
    if isinstance(cache_control, dict):
        mapped["cache_control"] = dict(cache_control)
    return mapped


def to_anthropic_content_block(block: Any) -> dict[str, Any]:
    """Convert one neutral/native block and reject malformed provider payloads."""
    if not isinstance(block, dict):
        raise AnthropicContentError("Anthropic content blocks must be objects")
    block_type = block.get("type")
    if block_type == "text":
        text = block.get("text", "")
        if not isinstance(text, str):
            raise AnthropicContentError("Anthropic text block must contain a string")
        return _with_cache_control({"type": "text", "text": text}, block)

    if block_type == "image_url":
        image_url = block.get("image_url")
        value = image_url.get("url") if isinstance(image_url, dict) else None
        media_type, data = _parse_data_url(value)
        data = _validate_base64(block_type="image", media_type=media_type, data=data)
        return _with_cache_control(
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            block,
        )

    if block_type in {"image", "document"}:
        source = block.get("source")
        if isinstance(source, dict):
            if source.get("type") != "base64":
                raise AnthropicContentError(f"Anthropic {block_type} source must use inline base64")
            media_type = str(source.get("media_type") or "").lower()
            data = source.get("data")
        else:
            media_type = str(block.get("media_type") or "").lower()
            data = block.get("data")
        data = _validate_base64(block_type=block_type, media_type=media_type, data=data)
        return _with_cache_control(
            {
                "type": block_type,
                "source": {"type": "base64", "media_type": media_type, "data": data},
            },
            block,
        )

    # These shapes are already Anthropic-native and can appear in a durable
    # assistant replay. Their semantic fields are validated by the provider;
    # images/documents never enter through this bypass.
    if block_type in {"thinking", "redacted_thinking", "tool_use", "tool_result"}:
        return dict(block)
    raise AnthropicContentError(f"Unsupported Anthropic content block type: {block_type!r}")


def to_anthropic_content_blocks(content: list[Any]) -> list[dict[str, Any]]:
    return [to_anthropic_content_block(block) for block in content]
