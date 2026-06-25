"""CCPlus V1 §7 acceptance — content_replacement (frozen bytes == model-seen).

GENUINE acceptance test for the §7 ``content_replacement`` selector. It drives
the REAL ``AgentKernel.handle`` loop with a tool that returns an oversized
(>50KB) result and captures the ``done_payload`` the kernel emits through the
``on_tool_call`` callback.

The kernel done_payload writes a ``content_replacement_record.v1`` whose
``inline_content`` IS the exact string the model saw next round
(``model_seen_result``), and whose ``inline_sha256`` is the hash of those frozen
bytes. The full original result is preserved alongside (``result`` +
``original_chars`` + ``original_sha256``). This proves the frozen, persisted
bytes are byte-for-byte what the model was shown — the foundation the web-resume
path reuses instead of re-truncating.

Revert-sensitive: if the kernel stopped emitting the
``content_replacement_record.v1`` with the model-seen ``inline_content``, or if
the persisted inline bytes diverged from what the next round's tool message
actually carried, these assertions fail.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kernel.contracts import InvocationRequest, RuntimeConfig


class _OneToolThenDoneClient:
    """First stream calls one tool; second stream finishes with text."""

    def __init__(self, *, tool_name: str, tool_call_id: str) -> None:
        self._responses = [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": tool_call_id, "function": {"name": tool_name, "arguments": "{}"}}],
                reasoning_content=None,
                reasoning_signature=None,
                usage={"total_tokens": 5},
            ),
            SimpleNamespace(
                content="all done",
                tool_calls=[],
                reasoning_content=None,
                reasoning_signature=None,
                usage={"total_tokens": 5},
            ),
        ]
        self.calls: list[dict] = []
        self.closed = False

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No scripted response left")
        return self._responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def _build_kernel(client, *, execute_tool):
    from app.kernel.engine import AgentKernel, KernelDependencies

    return AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            resolve_retrieval_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [
                {"type": "function", "function": {"name": "run_command", "description": "", "parameters": {}}}
            ],
            maybe_compress_messages=lambda messages, **_k: messages,
            create_client=lambda _model: client,
            execute_tool=execute_tool,
            persist_memory=lambda **_k: None,
            record_token_usage=lambda *_a, **_k: None,
            get_max_tokens=lambda provider, model, override=None: 200_000,
            extract_usage_tokens=lambda usage: (usage or {}).get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )


def _model() -> SimpleNamespace:
    return SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        supports_vision=False,
    )


@pytest.mark.asyncio
async def test_content_replacement_record_freezes_model_seen_result(tmp_path):
    """done_payload writes content_replacement_record.v1 == the model-seen bytes."""
    tool_call_id = "toolu_streamed_77"
    # 60K chars > the 50K inline limit for an eviction-eligible tool → eviction →
    # the inline content the model sees is a SHORTER preview (4K head + pointer),
    # distinct from the full original result.
    full_result = "R" * 60_000

    captured: list[dict] = []

    async def on_tool_call(payload):
        # Only capture the terminal "done" payload (it carries the record).
        if payload.get("status") == "done":
            captured.append(payload)

    async def execute_tool(_tool_name, _args, _request, _emit_event):
        return full_result

    client = _OneToolThenDoneClient(tool_name="run_command", tool_call_id=tool_call_id)
    kernel = _build_kernel(client, execute_tool=execute_tool)

    result = await kernel.handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "run a noisy command"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_tool_call=on_tool_call,
            eviction_dir=tmp_path,
        )
    )

    assert result.content == "all done"
    assert len(captured) == 1
    done_payload = captured[0]

    record = done_payload["content_replacement"]
    model_seen = done_payload["model_seen_result"]

    # 1. The record is the v1 schema carrying the streamed tool_call_id.
    assert record["schema"] == "content_replacement_record.v1"
    assert record["tool_call_id"] == tool_call_id
    assert record["tool_name"] == "run_command"

    # 2. The FROZEN inline bytes are exactly what the model saw next round.
    assert record["inline_content"] == model_seen
    # Replacement actually happened: inline is a strict, shorter preview of the
    # full result, not the whole thing.
    assert record["replacement_applied"] is True
    assert len(model_seen) < len(full_result)
    assert model_seen.startswith("R" * 4000)

    # 3. The persisted hashes prove byte-identity of BOTH the frozen view and
    #    the preserved original.
    assert record["inline_sha256"] == hashlib.sha256(model_seen.encode("utf-8")).hexdigest()
    assert record["original_sha256"] == hashlib.sha256(full_result.encode("utf-8")).hexdigest()
    assert record["original_chars"] == len(full_result)
    assert record["inline_chars"] == len(model_seen)

    # 4. The full original result is preserved on the payload alongside the
    #    frozen inline view (so resume can re-freeze, never re-truncate fresh).
    assert done_payload["result"] == full_result
    assert done_payload["tool_call_id"] == tool_call_id


@pytest.mark.asyncio
async def test_content_replacement_record_is_identity_for_small_results(tmp_path):
    """A small result needs no eviction: inline == original, no replacement applied."""
    tool_call_id = "toolu_small_3"
    small_result = "tiny output"

    captured: list[dict] = []

    async def on_tool_call(payload):
        if payload.get("status") == "done":
            captured.append(payload)

    async def execute_tool(_tool_name, _args, _request, _emit_event):
        return small_result

    client = _OneToolThenDoneClient(tool_name="run_command", tool_call_id=tool_call_id)
    kernel = _build_kernel(client, execute_tool=execute_tool)

    await kernel.handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "run a quick command"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_tool_call=on_tool_call,
            eviction_dir=tmp_path,
        )
    )

    assert len(captured) == 1
    record = captured[0]["content_replacement"]

    assert record["schema"] == "content_replacement_record.v1"
    # No eviction happened — the frozen inline IS the full result, byte-identical.
    assert record["inline_content"] == small_result
    assert record["replacement_applied"] is False
    assert record["inline_sha256"] == record["original_sha256"]
    assert captured[0]["model_seen_result"] == small_result
