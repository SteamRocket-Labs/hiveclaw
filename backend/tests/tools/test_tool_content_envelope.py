"""Step 2: typed multimodal tool-result content (image/document blocks).

adapt_and_call no longer flattens every tool result to a string (AI-Native L1).
A ToolContentEnvelope carries typed blocks + a text fallback; the engine builds a
multimodal content list; llm_client maps blocks per provider (Anthropic native,
OpenAI/Gemini fall back to text); read_file returns images as image blocks.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import pytest

# 1x1 transparent PNG
_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/KrwAAAAAElFTkSuQmCC")


def test_envelope_str_is_text_fallback():
    from app.tools.result_envelope import ToolContentEnvelope

    e = ToolContentEnvelope.image(text="[image: a.png]", media_type="image/png", data="QQ==")
    assert str(e) == "[image: a.png]"
    assert len(e.blocks) == 1
    assert e.blocks[0].type == "image"
    assert e.blocks[0].media_type == "image/png"


@pytest.mark.asyncio
async def test_adapt_and_call_passes_envelope_through():
    """A handler returning a ToolContentEnvelope is NOT flattened to str (L1)."""
    from app.tools.adapters import adapt_and_call
    from app.tools.decorator import ToolMeta
    from app.tools.result_envelope import ToolContentEnvelope
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    env = ToolContentEnvelope.image(text="[img]", media_type="image/png", data="QQ==")

    def handler(args):
        return env

    meta = ToolMeta(name="x", description="", parameters={}, category="c", display_name="X", adapter="args_only")
    ctx = ToolExecutionContext(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), tenant_id=None, workspace=Path("/tmp"))
    req = ToolExecutionRequest(tool_name="x", arguments={}, context=ctx)
    result = await adapt_and_call(meta, handler, req)
    assert result is env  # passed through untouched


def test_engine_tool_message_content():
    from app.kernel.engine import _tool_message_content
    from app.tools.result_envelope import ToolContentEnvelope

    # plain string → string
    assert _tool_message_content("hello", "hello") == "hello"
    # envelope with image → content list [text, image]
    env = ToolContentEnvelope.image(text="[img]", media_type="image/png", data="QQ==")
    content = _tool_message_content("[img]", env)
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image"
    assert content[1]["data"] == "QQ=="
    # envelope with no media blocks → text only
    plain_env = ToolContentEnvelope(text="just text", blocks=())
    assert _tool_message_content("just text", plain_env) == "just text"


def test_anthropic_tool_result_mapping():
    from app.services.llm_client import _anthropic_tool_result_content

    assert _anthropic_tool_result_content("plain") == "plain"
    blocks = [{"type": "text", "text": "hi"}, {"type": "image", "media_type": "image/png", "data": "QQ=="}]
    mapped = _anthropic_tool_result_content(blocks)
    assert mapped[0] == {"type": "text", "text": "hi"}
    assert mapped[1]["type"] == "image"
    assert mapped[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "QQ=="}


def test_text_only_provider_flatten_notes_dropped_blocks():
    from app.services.llm_client import _flatten_tool_content_to_text

    assert _flatten_tool_content_to_text("plain") == "plain"
    blocks = [{"type": "text", "text": "hi"}, {"type": "image", "media_type": "image/png", "data": "QQ=="}]
    flat = _flatten_tool_content_to_text(blocks)
    assert "hi" in flat
    assert "omitted" in flat  # non-text block noted, never silently dropped


def test_llm_message_tool_result_per_provider():
    from app.services.llm_client import LLMMessage

    blocks = [{"type": "text", "text": "hi"}, {"type": "image", "media_type": "image/png", "data": "QQ=="}]
    m = LLMMessage(role="tool", tool_call_id="t1", content=blocks)
    # Anthropic: native multimodal tool_result
    inner = m.to_anthropic_format()["content"][0]["content"]
    assert [b["type"] for b in inner] == ["text", "image"]
    # OpenAI Chat: tool content flattened to str
    assert isinstance(m.to_openai_format()["content"], str)


def test_read_file_image_returns_envelope(tmp_path):
    from app.services.agent_tool_domains.workspace import _read_file
    from app.tools.result_envelope import ToolContentEnvelope

    (tmp_path / "pic.png").write_bytes(_PNG)
    result = _read_file(tmp_path, "pic.png")
    assert isinstance(result, ToolContentEnvelope)
    assert result.blocks[0].type == "image"
    assert result.blocks[0].media_type == "image/png"
    assert base64.b64decode(result.blocks[0].data) == _PNG  # round-trips


def test_read_file_text_returns_str(tmp_path):
    from app.services.agent_tool_domains.workspace import _read_file

    (tmp_path / "note.txt").write_text("hello world", encoding="utf-8")
    result = _read_file(tmp_path, "note.txt")
    assert isinstance(result, str)
    assert "hello world" in result
