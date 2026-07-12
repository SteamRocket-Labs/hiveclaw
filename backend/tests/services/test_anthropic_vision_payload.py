from __future__ import annotations

import base64

import pytest


PNG_DATA = base64.b64encode(b"png-image-bytes").decode("ascii")
JPEG_DATA = base64.b64encode(b"jpeg-image-bytes").decode("ascii")


def _image_url(media_type: str, data: str) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}


def test_anthropic_main_message_maps_multiple_neutral_images_and_text() -> None:
    from app.services.llm_client import LLMMessage

    message = LLMMessage(
        role="user",
        content=[
            _image_url("image/png", PNG_DATA),
            {"type": "text", "text": "compare these"},
            _image_url("image/jpeg", JPEG_DATA),
        ],
    )

    payload = message.to_anthropic_format()

    assert payload == {
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": PNG_DATA},
            },
            {"type": "text", "text": "compare these"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": JPEG_DATA},
            },
        ],
    }


def test_anthropic_main_and_tool_images_share_one_canonical_mapping() -> None:
    from app.services.llm_client import LLMMessage, _anthropic_tool_result_content

    main = LLMMessage(role="user", content=[_image_url("image/png", PNG_DATA)]).to_anthropic_format()
    tool = _anthropic_tool_result_content([{"type": "image", "media_type": "image/png", "data": PNG_DATA}])

    assert main["content"] == tool


@pytest.mark.parametrize(
    "block",
    [
        _image_url("image/svg+xml", PNG_DATA),
        _image_url("image/png", "not-valid-base64%%%"),
        {"type": "image_url", "image_url": {"url": "https://attacker.example/image.png"}},
        {"type": "image", "media_type": "image/png", "data": "bad%%%"},
    ],
)
def test_anthropic_rejects_unsupported_or_malformed_image_blocks(block: dict) -> None:
    from app.services.anthropic_content import AnthropicContentError
    from app.services.llm_client import LLMMessage

    with pytest.raises(AnthropicContentError):
        LLMMessage(role="user", content=[block]).to_anthropic_format()


def test_anthropic_native_image_payload_is_replay_idempotent() -> None:
    from app.services.llm_client import LLMMessage

    initial = LLMMessage(
        role="user",
        content=[_image_url("image/png", PNG_DATA), {"type": "text", "text": "resume"}],
    ).to_anthropic_format()

    replayed = LLMMessage(role="user", content=initial["content"]).to_anthropic_format()

    assert replayed == initial


def test_anthropic_client_payload_snapshot_contains_no_openai_image_blocks() -> None:
    from app.services.llm_client import AnthropicClient, LLMMessage

    client = AnthropicClient(api_key="test", model="claude-test")
    payload = client._build_payload(
        [
            LLMMessage(role="system", content="system"),
            LLMMessage(
                role="user",
                content=[_image_url("image/png", PNG_DATA), {"type": "text", "text": "inspect"}],
            ),
        ],
        tools=None,
        temperature=0.2,
        max_tokens=128,
    )

    assert payload["messages"][0]["content"][0]["type"] == "image"
    assert payload["messages"][0]["content"][0]["source"]["media_type"] == "image/png"
    assert "image_url" not in str(payload["messages"])


def test_invoker_vision_transform_is_valid_for_anthropic_and_strips_when_disabled() -> None:
    from app.runtime.invoker import _apply_vision_transform
    from app.services.llm_client import LLMMessage

    marker = f"[image_data:data:image/png;base64,{PNG_DATA}]"
    enabled = _apply_vision_transform([LLMMessage(role="user", content=f"inspect {marker}")], True)
    disabled = _apply_vision_transform([LLMMessage(role="user", content=f"inspect {marker}")], False)

    enabled_payload = enabled[0].to_anthropic_format()
    assert enabled_payload["content"][0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG_DATA},
    }
    assert "does not support vision" in disabled[0].content
    assert "image_data" not in disabled[0].content
