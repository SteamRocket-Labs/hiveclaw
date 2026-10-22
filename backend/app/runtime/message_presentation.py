"""Exact image protocol presentation shared by runtime and input provenance."""

from __future__ import annotations

import re
from typing import Any

from app.services.llm_client import LLMMessage


def apply_vision_transform(api_messages: list[LLMMessage], supports_vision: bool) -> list[LLMMessage]:
    if supports_vision:
        image_pattern = r"\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]"
        for i, msg in enumerate(api_messages):
            if msg.role != "user" or not isinstance(msg.content, str):
                continue
            images = re.findall(image_pattern, msg.content)
            if not images:
                continue
            text = re.sub(image_pattern, "", msg.content).strip()
            parts: list[dict[str, Any]] = []
            for image_url in images:
                parts.append({"type": "image_url", "image_url": {"url": image_url}})
            if text:
                parts.append({"type": "text", "text": text})
            api_messages[i] = LLMMessage(role=msg.role, content=parts, source_ref=msg.source_ref)  # type: ignore[arg-type]
        return api_messages

    strip_pattern = r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]"
    for i, msg in enumerate(api_messages):
        if msg.role != "user" or not isinstance(msg.content, str) or "[image_data:" not in msg.content:
            continue
        image_count = len(re.findall(strip_pattern, msg.content))
        cleaned = re.sub(strip_pattern, "", msg.content).strip()
        if image_count > 0:
            cleaned += (
                f"\n[The user sent {image_count} image(s), but the current model does not support vision, "
                "so the image content is unavailable.]"
            )
        api_messages[i] = LLMMessage(role=msg.role, content=cleaned, source_ref=msg.source_ref)
    return api_messages


def match_vision_presentation(canonical: dict[str, Any], wire: dict[str, Any]) -> dict[str, Any] | None:
    """Recognize only the runtime's exact presentation of a canonical user input."""
    if (set(canonical) != {"role", "content"} or canonical["role"] != "user"
        or not isinstance(canonical["content"], str)
        or not re.search(r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]", canonical["content"])):
        return None
    for supports_vision in (False, True):
        message = apply_vision_transform([LLMMessage(**canonical)], supports_vision)[0]
        if wire == {"role": message.role, "content": message.content}:
            return {"schema": "hive.vision_message_presentation.v1", "supports_vision": supports_vision}
    return None
