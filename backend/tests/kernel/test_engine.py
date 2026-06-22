from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No fake response prepared")
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        return None


def test_llm_message_dict_round_trip_preserves_reasoning_signature() -> None:
    from app.kernel.engine import _dicts_to_llm_messages, _llm_messages_to_dicts
    from app.services.llm_client import LLMMessage

    messages = [
        LLMMessage(
            role="assistant",
            content="tool decision",
            reasoning_content="signed thinking",
            reasoning_signature="sig-round-trip",
        )
    ]

    as_dicts = _llm_messages_to_dicts(messages)
    restored = _dicts_to_llm_messages(as_dicts)

    assert as_dicts[0]["reasoning_signature"] == "sig-round-trip"
    assert restored[0].reasoning_signature == "sig-round-trip"


def test_split_concatenated_json_returns_single_object_when_valid():
    """T1-4: a single valid JSON object passes through untouched."""
    from app.kernel.engine import _split_concatenated_json

    assert _split_concatenated_json('{"query":"a"}') == ['{"query":"a"}']
    assert _split_concatenated_json('  {"query":"a"}  ') == ['{"query":"a"}']


@pytest.mark.asyncio
async def test_kernel_continues_streaming_output_after_output_cap() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="part one ",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 10},
                finish_reason="length",
            ),
            SimpleNamespace(
                content="part two",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 6},
                finish_reason="stop",
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "write a long report"}],
            agent_name="Writer",
            role_description="Writes reports",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "part one part two"
    assert result.tokens_used == 16
    # Continuation escalates to the provider's per-provider output ceiling
    # (openai = 131072), not the old flat 65536.
    assert [call["max_tokens"] for call in fake_client.calls] == [2048, 131072]
    continuation_messages = fake_client.calls[1]["messages"]
    assert any(message.role == "assistant" and message.content == "part one " for message in continuation_messages)
    assert "Continue the previous answer" in continuation_messages[-1].content


@pytest.mark.asyncio
async def test_kernel_converts_stream_retry_tombstone_to_runtime_event() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.services.llm_utils import STREAM_RETRY_TOMBSTONE

    class _TombstoneClient:
        async def stream(self, **kwargs):
            await kwargs["on_chunk"]("partial ")
            await kwargs["on_chunk"](STREAM_RETRY_TOMBSTONE)
            await kwargs["on_chunk"]("final")
            return SimpleNamespace(
                content="final",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
                finish_reason="stop",
            )

        async def close(self) -> None:
            return None

    chunks: list[str] = []
    runtime_events: list[dict] = []
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: _TombstoneClient(),
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_chunk=chunks.append,
            on_event=runtime_events.append,
        )
    )

    assert result.content == "final"
    assert chunks == ["partial ", "final"]
    assert {"type": "stream_retry_tombstone"} in runtime_events


@pytest.mark.asyncio
async def test_kernel_records_prompt_cache_metrics_from_response_usage() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.memory.metrics import reset_all, snapshot

    reset_all()
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="cached answer",
                tool_calls=[],
                reasoning_content=None,
                usage={"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 750}},
                finish_reason="stop",
            ),
        ]
    )
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") or usage.get("prompt_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    snap = snapshot()
    assert result.content == "cached answer"
    assert snap["prompt_cache_observations_total"]["openai:hit"] == 1
    assert snap["prompt_cache_read_tokens_total"]["openai"] == 750
    assert snap["prompt_cache_hit_rate"]["openai"] == 0.75


@pytest.mark.asyncio
async def test_response_complete_hook_failure_is_counted_without_failing_invocation(monkeypatch) -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.memory.metrics import reset_all, snapshot

    reset_all()

    async def raising_emit_hook(*_args, **_kwargs):
        raise RuntimeError("hook bus down")

    monkeypatch.setattr("app.runtime.hooks.emit_hook", raising_emit_hook)

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 3},
                finish_reason="stop",
            )
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="Answers",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    for _ in range(10):
        if "response_complete:kernel:RuntimeError" in snapshot()["hook_failure_total"]:
            break
        await asyncio.sleep(0.01)
    assert snapshot()["hook_failure_total"]["response_complete:kernel:RuntimeError"] == 1


@pytest.mark.asyncio
async def test_kernel_drains_mid_run_user_messages_between_tool_rounds() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "call_1", "function": {"name": "list_files", "arguments": "{}"}}],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
            SimpleNamespace(
                content="noted",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 7},
            ),
        ]
    )
    drain_calls = 0

    async def drain_mid_run_messages():
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 2:
            return [{"role": "user", "content": "Actually focus on the security boundary."}]
        return []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {
                    "type": "function",
                    "function": {"name": "list_files", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "files",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "Inspect the project"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
            mid_run_message_drain=drain_mid_run_messages,
        )
    )

    assert result.content == "noted"
    second_round_messages = fake_client.calls[1]["messages"]
    assert any(
        message.role == "user" and message.content == "Actually focus on the security boundary."
        for message in second_round_messages
    )


def test_mid_run_drain_prefers_structured_llm_content_over_display_text() -> None:
    from app.kernel.engine import _mid_run_items_to_user_messages

    messages = _mid_run_items_to_user_messages(
        [
            {
                "content": "[file:bank.pdf]\nSummarize it.",
                "display_content": "[file:bank.pdf]\nSummarize it.",
                "llm_content": "[File: bank.pdf]\nFull extracted text\n\nQuestion: Summarize it.",
                "attachments": [{"name": "bank.pdf", "path": "workspace/bank.pdf"}],
            }
        ]
    )

    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content == "[File: bank.pdf]\nFull extracted text\n\nQuestion: Summarize it."


def test_split_concatenated_json_splits_double_object():
    """T1-4: DeepSeek-V4 style {"a":1}{"b":2} concatenation is split into separate payloads."""
    from app.kernel.engine import _split_concatenated_json

    result = _split_concatenated_json('{"query":"a"}{"query":"b"}')
    assert result == ['{"query":"a"}', '{"query":"b"}']


def test_split_concatenated_json_splits_with_whitespace_between():
    """T1-4: whitespace between concatenated payloads is tolerated."""
    from app.kernel.engine import _split_concatenated_json

    result = _split_concatenated_json('{"query":"a"} {"query":"b"}\n{"query":"c"}')
    assert result == ['{"query":"a"}', '{"query":"b"}', '{"query":"c"}']


def test_split_concatenated_json_keeps_string_braces_intact():
    """T1-4: a closing brace inside a string must not be treated as object boundary."""
    from app.kernel.engine import _split_concatenated_json

    payload = '{"query":"contains } brace"}'
    assert _split_concatenated_json(payload) == [payload]


def test_split_concatenated_json_falls_back_when_partial():
    """T1-4: when the buffer is not cleanly split-able, return it as-is so the caller can
    report a parse error rather than swallowing an unknown shape."""
    from app.kernel.engine import _split_concatenated_json

    assert _split_concatenated_json("garbage") == ["garbage"]
    assert _split_concatenated_json('{"a":1}garbage{"b":2}') == ['{"a":1}garbage{"b":2}']


def test_expand_concatenated_tool_calls_splits_each_payload():
    """T1-4: concatenated tool_call.arguments expand into separate tool_calls with
    distinct ids so every payload gets executed and every tool message can reference
    a real tool_call id in the assistant history."""
    from app.kernel.engine import _expand_concatenated_tool_calls

    expanded = _expand_concatenated_tool_calls(
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query":"a"}{"query":"b"}'},
            }
        ]
    )

    assert [tc["id"] for tc in expanded] == ["call_1-split1", "call_1-split2"]
    assert [tc["function"]["arguments"] for tc in expanded] == ['{"query":"a"}', '{"query":"b"}']
    assert all(tc["type"] == "function" for tc in expanded)
    assert all(tc["function"]["name"] == "web_search" for tc in expanded)


def test_expand_concatenated_tool_calls_passes_through_clean_payloads():
    """T1-4: a single valid payload is not duplicated or rewritten."""
    from app.kernel.engine import _expand_concatenated_tool_calls

    original = [
        {
            "id": "call_2",
            "type": "function",
            "function": {"name": "load_skill", "arguments": '{"slug":"deep-research"}'},
        }
    ]
    expanded = _expand_concatenated_tool_calls(original)
    assert expanded == original


def test_humanize_llm_error_reports_quota_instead_of_auth_for_403_quota():
    from app.kernel.engine import _humanize_llm_error
    from app.services.llm_utils import LLMError

    message = _humanize_llm_error(
        LLMError('HTTP 403: {"error":{"message":"Your token-plan quota has been exhausted."}}')
    )

    assert message == "[LLM Error] AI 模型额度已耗尽，请联系管理员检查模型额度或切换模型。"


def test_humanize_llm_error_reports_model_not_found_separately():
    from app.kernel.engine import _humanize_llm_error
    from app.services.llm_utils import LLMError

    message = _humanize_llm_error(LLMError('HTTP 404: {"error":{"message":"Model Not Exist"}}'))

    assert message == "[LLM Error] AI 模型名称不存在或未对当前账号开放，请联系管理员检查模型配置。"


def test_humanize_llm_error_reports_provider_bad_request_separately():
    from app.kernel.engine import _humanize_llm_error
    from app.services.llm_utils import LLMError

    message = _humanize_llm_error(LLMError('HTTP 400: {"error":{"message":"invalid model parameter"}}'))

    assert message == "[LLM Error] AI 模型供应商拒绝了当前请求，请检查模型名称、参数或消息格式。"


def test_build_restoration_context_prefers_newest_recent_files(tmp_path, monkeypatch):
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)

    files = []
    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]:
        path = tmp_path / name
        path.write_text(f"content for {name}", encoding="utf-8")
        files.append(str(path))

    session = SessionContext()
    session.recent_files = files

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=session)

    e_idx = restored.index("### Recent File: e.txt")
    d_idx = restored.index("### Recent File: d.txt")
    c_idx = restored.index("### Recent File: c.txt")
    assert e_idx < d_idx < c_idx


def test_build_restoration_context_resolves_relative_recent_file_paths(tmp_path, monkeypatch):
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    agent_root = tmp_path / str(agent_id)
    workspace_dir = agent_root / "workspace"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "report.md").write_text("relative path content", encoding="utf-8")

    session = SessionContext()
    session.track_file_read("workspace/report.md")

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=session)

    assert "### Recent File: workspace/report.md" in restored
    assert "relative path content" in restored


def test_build_restoration_context_restores_five_recent_files(tmp_path, monkeypatch):
    """P2-1 (docs/compaction-cc-alignment.md): restore the working set after compaction —
    up to 5 recent files (CC restores ≤5), not 3."""
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)

    files = []
    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]:
        path = tmp_path / name
        path.write_text(f"content for {name}", encoding="utf-8")
        files.append(str(path))

    session = SessionContext()
    session.recent_files = files

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=session)

    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]:
        assert f"### Recent File: {name}" in restored


def test_build_restoration_context_file_budget_uses_per_file_cap(tmp_path, monkeypatch):
    """Per-file restore budget is the full per-file cap (8K chars), not cap//2."""
    from app.kernel.engine import _POST_COMPACT_PER_FILE_CAP, _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)

    big_file = tmp_path / "big.txt"
    marker_tail = "TAIL-MARKER-AT-6000"
    big_file.write_text("z" * 5980 + marker_tail, encoding="utf-8")  # 5999 chars < 8K cap

    session = SessionContext()
    session.recent_files = [str(big_file)]

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=session)

    assert len(marker_tail) < _POST_COMPACT_PER_FILE_CAP  # sanity
    assert marker_tail in restored  # old cap//2 (4K) would have cut this off


def test_runtime_attachment_sections_report_tool_refresh_and_external_file_changes(tmp_path, monkeypatch):
    from app.kernel.engine import (
        _build_runtime_attachment_sections,
        _snapshot_session_file,
    )
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    agent_root = tmp_path / str(agent_id)
    workspace_dir = agent_root / "workspace"
    workspace_dir.mkdir(parents=True)
    report = workspace_dir / "report.md"
    report.write_text("old content", encoding="utf-8")

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    session = SessionContext()
    session.track_discovered_tools(["web_search"])
    session.track_file_read("workspace/report.md", snapshot=_snapshot_session_file(agent_id, "workspace/report.md"))
    report.write_text("new content with different size", encoding="utf-8")

    text = "\n\n".join(_build_runtime_attachment_sections(agent_id, session))

    assert "Runtime Tool Refresh" in text
    assert "web_search" in text
    assert "Runtime File Change Notice" in text
    assert 'read_file("workspace/report.md")' in text


def test_build_persisted_memory_messages_includes_runtime_events():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _build_persisted_memory_messages
    from app.runtime.session import SessionContext

    session = SessionContext()
    session.track_tool_outcome("web_search", "Found deployment guide and rollback notes")
    session.track_file_write("workspace/deploy-plan.md")
    session.track_external_ref("https://docs.example.com/deploy")
    session.track_pending_item("Verify rollback checklist before production deploy")

    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "继续部署任务"}],
        agent_name="Ops Agent",
        role_description="deployment helper",
        session_context=session,
    )

    persisted = _build_persisted_memory_messages(request, "部署步骤已整理")
    contents = [msg.get("content", "") for msg in persisted if isinstance(msg.get("content"), str)]

    assert any("Runtime event: tool outcome web_search" in content for content in contents)
    assert any("Runtime event: wrote file workspace/deploy-plan.md" in content for content in contents)
    assert any("Runtime event: external reference https://docs.example.com/deploy" in content for content in contents)
    assert any(
        "Runtime event: pending work Verify rollback checklist before production deploy" in content
        for content in contents
    )


def test_should_expand_tools_does_not_expand_fs_read_skill_file():
    from app.kernel.engine import _should_expand_tools

    assert _should_expand_tools("fs_read", {"mode": "text", "path": "skills/research/SKILL.md"}) is False


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_tracks_filesystem_facade_events(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    session = SessionContext(session_id="s1")
    session.prompt_prefix = "cached-prefix"
    session.metadata["prompt_cache_key"] = "old-key"
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "inspect files"}],
        agent_name="Agent",
        role_description="role",
        session_context=session,
        memory_session_id="s1",
    )

    async def fake_execute_tool(_tool_name, _tool_args, _request, _emit_event):
        return "ok"

    async def emit_event(_event):
        return None

    await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="fs_read",
        tool_args={"mode": "text", "path": "workspace/notes.md"},
        emit_event=emit_event,
    )
    await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="fs_write",
        tool_args={"mode": "edit", "path": "soul.md"},
        emit_event=emit_event,
    )

    assert "workspace/notes.md" in session.recent_files
    assert "soul.md" in session.recent_writes
    assert session.prompt_prefix is None
    assert "prompt_cache_key" not in session.metadata
    assert session.metadata["prompt_cache_invalidated_reason"] == "fs_write:soul.md"


@pytest.mark.asyncio
async def test_agent_kernel_handles_tool_round_and_collects_parts():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    agent_id = uuid4()
    user_id = uuid4()
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    tool_load_calls: list[bool] = []
    persisted_payloads: list[dict] = []

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5, quota_message=None)

    async def resolve_current_user_name(_user_id):
        return "Rocky"

    async def build_system_prompt(request, tenant_id, memory_context, current_user_name):
        assert tenant_id is not None
        assert current_user_name == "Rocky"
        assert memory_context == ""
        return f"PROMPT::{request.agent_name}"

    async def resolve_memory_context(request, tenant_id):
        assert tenant_id is not None
        return ""

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    async def maybe_compress_messages(messages, **kwargs):
        return messages

    async def execute_tool(tool_name, args, request, emit_event):
        del request, emit_event
        assert tool_name == "read_file"
        assert args == {"path": "skills/web-research/SKILL.md"}
        return "SKILL_CONTENT"

    async def persist_memory(**kwargs):
        persisted_payloads.append(kwargs)

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path":"skills/web-research/SKILL.md"}'},
                    }
                ],
                reasoning_content="reasoning",
                usage={"total_tokens": 10},
            ),
            SimpleNamespace(
                content="final answer",
                tool_calls=[],
                reasoning_content="final reasoning",
                usage={"total_tokens": 8},
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=resolve_current_user_name,
            build_system_prompt=build_system_prompt,
            resolve_memory_context=resolve_memory_context,
            get_tools=get_tools,
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda model: fake_client,
            execute_tool=execute_tool,
            persist_memory=persist_memory,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "帮我做调研"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=agent_id,
            user_id=user_id,
        )
    )

    assert result.content == "final answer"
    assert tool_load_calls == [True]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"
    assert result.parts == [
        {
            "type": "tool_call",
            "name": "read_file",
            "args": {"path": "skills/web-research/SKILL.md"},
            "status": "done",
            "result": "SKILL_CONTENT",
            "reasoning": "reasoning",
        },
        {"type": "reasoning", "text": "final reasoning"},
        {"type": "text", "text": "final answer"},
    ]
    assert persisted_payloads


@pytest.mark.asyncio
async def test_kernel_round_trips_reasoning_signature_after_tool_call() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="anthropic", model="claude-sonnet-4", api_key="key", base_url=None)
    agent_id = uuid4()
    user_id = uuid4()
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path":"notes.md"}'},
                    }
                ],
                reasoning_content="tool thinking",
                reasoning_signature="sig-tool-turn",
                usage={"total_tokens": 10},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content="final thinking",
                reasoning_signature="sig-final",
                usage={"total_tokens": 5},
                finish_reason="stop",
            ),
        ]
    )

    async def execute_tool(tool_name, args, request, emit_event):
        del request, emit_event
        assert tool_name == "read_file"
        assert args == {"path": "notes.md"}
        return "file contents"

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=execute_tool,
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "read notes"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=agent_id,
            user_id=user_id,
        )
    )

    assert result.content == "done"
    assert result.reasoning_signature == "sig-final"
    second_round_messages = fake_client.calls[1]["messages"]
    assistant_tool_turn = next(message for message in second_round_messages if message.role == "assistant")
    assert assistant_tool_turn.reasoning_content == "tool thinking"
    assert assistant_tool_turn.reasoning_signature == "sig-tool-turn"


@pytest.mark.asyncio
async def test_plan_mode_tool_intercept_notice_follows_tool_result_message() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.runtime.session import SessionContext

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_delegate",
                        "function": {
                            "name": "delegate_to_agent",
                            "arguments": json.dumps(
                                {
                                    "agent_name": "飞书知识库助手",
                                    "message": "去飞书知识库搜索飞翼艇报告并总结",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 9},
            ),
            SimpleNamespace(
                content="plan submitted",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 7},
                finish_reason="stop",
            ),
        ]
    )

    requires_confirmation = json.dumps(
        {
            "ok": False,
            "status": "requires_confirmation",
            "requires_confirmation": True,
            "summary": "Confirm before delegating.",
        },
        ensure_ascii=False,
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: requires_confirmation,
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "使用飞书知识库助手找飞翼艇报告"}],
            agent_name="Leslie的智能助手",
            role_description="Coordinator",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(
                session_id="wechat-session",
                source="wechat_personal",
                channel="wechat_personal",
            ),
        )
    )

    assert result.content == "plan submitted"
    second_round_messages = fake_client.calls[1]["messages"]
    assistant_index = next(
        i for i, message in enumerate(second_round_messages) if message.role == "assistant" and message.tool_calls
    )
    assert second_round_messages[assistant_index + 1].role == "tool"
    assert second_round_messages[assistant_index + 1].tool_call_id == "call_delegate"
    assert not any(
        message.role == "system" and "[Plan Mode Activated]" in (message.content or "")
        for message in second_round_messages
    )


@pytest.mark.asyncio
async def test_agent_kernel_stops_after_blocking_ask_user_question_result():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )
    clarification_result = (
        '{"status":"awaiting_user_clarification","blocking":true,'
        '"questions":[{"question":"Pick one?","options":[{"label":"A"},{"label":"B"}]}]}'
    )
    tool_events: list[dict] = []
    persisted_payloads: list[dict] = []
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_clarify",
                        "function": {
                            "name": "ask_user_question",
                            "arguments": (
                                '{"questions":[{"question":"Pick one?","options":[{"label":"A"},{"label":"B"}]}]}'
                            ),
                        },
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 4},
            ),
            SimpleNamespace(
                content="this second model turn must not run",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 4},
            ),
        ]
    )

    async def execute_tool(tool_name, args, request, emit_event):
        del args, request, emit_event
        assert tool_name == "ask_user_question"
        return clarification_result

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda request, tenant_id, memory_context, current_user_name: "PROMPT",
            resolve_memory_context=lambda request, tenant_id: "",
            get_tools=lambda _agent_id, _core_only: [
                {
                    "type": "function",
                    "function": {
                        "name": "ask_user_question",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=execute_tool,
            persist_memory=lambda **kwargs: persisted_payloads.append(kwargs),
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "ask me before choosing"}],
            agent_name="Planner",
            role_description="Planning agent",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_tool_call=lambda payload: tool_events.append(payload),
        )
    )

    assert len(fake_client.calls) == 1
    assert result.content == ""
    assert "this second model turn must not run" not in [part.get("text") for part in result.parts]
    assert tool_events[-1]["name"] == "ask_user_question"
    assert tool_events[-1]["status"] == "done"
    assert tool_events[-1]["result"] == clarification_result
    assert persisted_payloads


@pytest.mark.asyncio
async def test_agent_kernel_splits_concatenated_tool_arguments_into_separate_calls():
    """Tier 1-4: concatenated DeepSeek-V4 style args `{"a":1}{"b":2}` must be split into
    two executable tool_calls (rather than dropped to `{}`). Both calls execute and the
    assistant history records each split call with a unique id and a valid arguments JSON.
    """
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_bad",
                        "function": {
                            "name": "web_fetch",
                            "arguments": '{"url":"https://example.com/a"}{"url":"https://example.com/b"}',
                        },
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="recovered",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
        ]
    )

    executed_args: list[dict] = []

    def _capture_execute(name: str, arguments: dict, *_args, **_kwargs):
        executed_args.append(arguments)
        return f"fetched-{arguments.get('url', 'unknown')}"

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {
                    "type": "function",
                    "function": {"name": "web_fetch", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=_capture_execute,
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "fetch both pages"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "recovered"
    assert len(fake_client.calls) == 2
    retry_messages = fake_client.calls[1]["messages"]
    assistant_with_bad_call = next(
        message for message in retry_messages if message.role == "assistant" and message.tool_calls
    )

    # Tier 1-4: the concatenated payload becomes two tool_calls, each with valid JSON
    assert len(assistant_with_bad_call.tool_calls) == 2
    first, second = assistant_with_bad_call.tool_calls
    assert first["function"]["arguments"] == '{"url":"https://example.com/a"}'
    assert second["function"]["arguments"] == '{"url":"https://example.com/b"}'
    assert first["type"] == "function"
    assert second["type"] == "function"
    assert first["id"] != second["id"], "Split tool_calls must carry distinct ids"

    # Both payloads were executed instead of being collapsed
    assert {a.get("url") for a in executed_args} == {
        "https://example.com/a",
        "https://example.com/b",
    }


@pytest.mark.asyncio
async def test_runtime_invoker_delegates_to_agent_kernel(monkeypatch):
    from app.kernel.contracts import InvocationResult
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return InvocationResult(
                content="kernel-result",
                tokens_used=7,
                final_tools=[{"type": "function", "function": {"name": "x"}}],
                parts=[{"type": "text", "text": "kernel-result"}],
            )

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())

    async def allow_quota(_user_id):
        return None

    monkeypatch.setattr("app.runtime.invoker.check_user_token_quota", allow_quota)

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert captured["request"].agent_name == "Agent"
    assert captured["request"].messages == [{"role": "user", "content": "hello"}]
    assert result.content == "kernel-result"
    assert result.tokens_used == 7
    assert result.final_tools == [{"type": "function", "function": {"name": "x"}}]
    assert result.parts == [{"type": "text", "text": "kernel-result"}]


@pytest.mark.asyncio
async def test_agent_kernel_emits_ptl_full_compress_before_round_group_retry_event():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.llm_utils import LLMError

    runtime_events: list[dict] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=128000,
        supports_vision=False,
    )
    fake_client = _FakeClient(
        [
            LLMError("HTTP 400: context_length_exceeded - maximum context length exceeded"),
            SimpleNamespace(
                content="recovered after ptl retry",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 6},
            ),
        ]
    )

    compress_calls: list[list[dict]] = []

    async def maybe_compress_messages(messages, **kwargs):
        compress_calls.append(messages)
        if len(compress_calls) == 1:
            return messages
        return [
            {"role": "system", "content": "compressed summary"},
            messages[-1],
        ]

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "unused",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[
                {"role": "user", "content": "older question"},
                {"role": "assistant", "content": "older answer"},
                {"role": "user", "content": "follow-up question"},
                {"role": "assistant", "content": "follow-up answer"},
                {"role": "user", "content": "latest ask"},
            ],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=runtime_events.append,
        )
    )

    assert result.content == "recovered after ptl retry"
    assert len(compress_calls) >= 2
    assert len(fake_client.calls) == 2
    assert len(fake_client.calls[1]["messages"]) < len(fake_client.calls[0]["messages"])
    assert runtime_events[0] == {
        "type": "session_compact",
        "summary": "Prompt too long; compressed conversation before retry.",
        "original_message_count": 6,
        "kept_message_count": 3,
        "reason": "prompt_too_long_retry",
        "strategy": "full_compress",
        "attempt": 1,
    }


@pytest.mark.asyncio
async def test_agent_kernel_emits_runtime_fallback_event_after_prompt_too_long():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.llm_utils import LLMError

    runtime_events: list[dict] = []
    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="primary",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=128000,
        supports_vision=False,
    )
    fallback_model = SimpleNamespace(
        provider="anthropic",
        model="claude-sonnet",
        api_key="fallback",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=200000,
        supports_vision=False,
    )
    primary_client = _FakeClient(
        [
            LLMError("HTTP 400: context_length_exceeded - maximum context length exceeded"),
        ]
    )
    fallback_client = _FakeClient(
        [
            SimpleNamespace(
                content="fallback recovered",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 9},
            ),
        ]
    )

    def create_client(model):
        if model is primary_model:
            return primary_client
        if model is fallback_model:
            return fallback_client
        raise AssertionError(f"Unexpected model {model}")

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=2),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=create_client,
            execute_tool=lambda *_args, **_kwargs: "unused",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=primary_model,
            fallback_model=fallback_model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=runtime_events.append,
        )
    )

    assert result.content == "fallback recovered"
    # T-G1 also emits reminder_injected observability events (max_tool_rounds=2
    # puts round 0 on the final-pressure threshold), so filter by type.
    fallback_events = [e for e in runtime_events if e["type"] == "runtime_fallback"]
    assert len(fallback_events) == 1
    assert fallback_events[0]["reason"] == "prompt_too_long"
    assert fallback_events[0]["from_model"] == "gpt-4.1"
    assert fallback_events[0]["to_model"] == "claude-sonnet"
    assert fallback_events[0]["provider"] == "anthropic"
    assert fallback_events[0]["part"]["event_type"] == "runtime_fallback"


@pytest.mark.asyncio
async def test_agent_kernel_keeps_core_tools_when_load_skill_has_no_declared_expansion():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    tool_load_calls: list[bool] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=4, quota_message=None)

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "load_skill", "arguments": '{"name":"web research"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=get_tools,
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "SKILL",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "load a skill"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert tool_load_calls == [True]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"


@pytest.mark.asyncio
async def test_agent_kernel_does_not_expand_tools_after_load_skill():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig, ToolExpansionResult

    tool_load_calls: list[bool] = []
    expansion_calls: list[tuple[str, dict[str, str]]] = []
    emitted_events: list[dict] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=4, quota_message=None)

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    async def resolve_tool_expansion(_request, tool_name, args):
        expansion_calls.append((tool_name, args))
        return ToolExpansionResult(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            active_tool_groups=[
                {
                    "name": "web_pack",
                    "summary": "网页搜索与抓取能力",
                    "tools": ["web_search"],
                    "source": "system",
                }
            ],
            event_payload={
                "type": "tool_group_activation",
                "packs": [
                    {
                        "name": "web_pack",
                        "summary": "网页搜索与抓取能力",
                        "tools": ["web_search"],
                        "source": "system",
                    }
                ],
                "tool_groups": [
                    {
                        "name": "web_pack",
                        "summary": "网页搜索与抓取能力",
                        "tools": ["web_search"],
                        "source": "system",
                    }
                ],
                "message": "Activated web_pack",
                "status": "info",
            },
        )

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "load_skill", "arguments": '{"name":"web research"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=get_tools,
            resolve_tool_expansion=resolve_tool_expansion,
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "SKILL",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "load a skill"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=lambda event: emitted_events.append(event),
        )
    )

    assert result.content == "done"
    assert tool_load_calls == [True]
    assert expansion_calls == []
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"
    assert emitted_events == []


@pytest.mark.asyncio
async def test_agent_kernel_does_not_auto_expand_without_skill_or_mcp_trigger():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    tool_load_calls: list[bool] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5, quota_message=None)

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "list_files", "arguments": '{"path":"skills"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_2",
                        "function": {"name": "list_files", "arguments": '{"path":"workspace"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_3",
                        "function": {"name": "list_files", "arguments": '{"path":"memory"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=get_tools,
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "OK",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "keep using the default toolkit"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert tool_load_calls == [True]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[2]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[3]["tools"][0]["function"]["name"] == "core_tool"


@pytest.mark.asyncio
async def test_midloop_compaction_triggers_after_interval():
    """Mid-loop compaction fires every _MIDLOOP_COMPACT_CHECK_INTERVAL rounds
    and compresses when maybe_compress_messages returns fewer messages."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    compress_calls: list[int] = []
    compaction_events: list[dict] = []

    async def maybe_compress_messages(messages, **kwargs):
        compress_calls.append(len(messages))
        # Simulate compression: if >4 messages, summarise the old ones into 1
        if len(messages) > 4:
            on_compaction = kwargs.get("on_compaction")
            if on_compaction:
                result = on_compaction(
                    {
                        "summary": "compressed",
                        "original_message_count": len(messages),
                        "kept_message_count": 3,
                    }
                )
                if result is not None:
                    await result
            summary = {"role": "system", "content": "[Summary of previous conversation]"}
            return [summary] + messages[-2:]
        return messages

    # 4 tool-call rounds then a final text response
    responses = []
    for i in range(4):
        responses.append(
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": f"call_{i}",
                        "function": {"name": "list_files", "arguments": f'{{"path":"dir{i}"}}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 5},
            )
        )
    responses.append(
        SimpleNamespace(
            content="all done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 3},
        )
    )

    fake_client = _FakeClient(responses)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=10,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {
                    "type": "function",
                    "function": {"name": "list_files", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_a, **_kw: "files: a.txt, b.txt",
            persist_memory=lambda **kw: None,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "list everything"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=lambda ev: compaction_events.append(ev) if ev.get("type") == "session_compact" else None,
        )
    )

    assert result.content == "all done"

    # Pre-loop compression is call 1 (1 user message → no compression needed)
    # Mid-loop compression fires at round 3 (interval=3)
    # At that point api_messages[1:] has: user + 3×(assistant+tool) = 7 messages → compressed
    assert len(compress_calls) >= 2, f"Expected at least 2 compress calls, got {compress_calls}"

    # The mid-loop call should have received >4 messages and compressed
    midloop_call_msg_count = compress_calls[1]
    assert midloop_call_msg_count > 4, f"Mid-loop should see >4 messages, got {midloop_call_msg_count}"

    # Compaction event should have been emitted
    assert len(compaction_events) >= 1, "Expected at least one session_compact event"
    assert compaction_events[0]["type"] == "session_compact"


def test_maybe_evict_tool_result_truncates_large_output():
    from app.kernel.engine import _maybe_evict_tool_result

    # Small result — returned unchanged
    small = "hello world"
    assert _maybe_evict_tool_result("run_code", "call_1", small) == small

    # Exempt tool — never evicted even if large
    large = "x" * 60000
    assert _maybe_evict_tool_result("read_file", "call_2", large) == large
    assert _maybe_evict_tool_result("list_files", "call_3", large) == large
    assert _maybe_evict_tool_result("web_search", "call_exempt", large) == large

    # Non-exempt large result — truncated (no eviction_dir)
    evicted = _maybe_evict_tool_result("run_code", "call_4", large)
    assert len(evicted) < len(large)
    assert "truncated" in evicted
    assert "60000 chars" in evicted
    assert "call_4" in evicted
    # Preview should be present (first 2000 chars)
    assert evicted.startswith("x" * 2000)


def test_maybe_evict_writes_file_when_eviction_dir_provided(tmp_path):
    from app.kernel.engine import _maybe_evict_tool_result

    large = "RESULT_DATA\n" * 5000  # ~60000 chars
    eviction_dir = tmp_path / "tool_results"

    evicted = _maybe_evict_tool_result("run_code", "call_99", large, eviction_dir=eviction_dir)

    # File should exist with full content
    written_file = eviction_dir / "call_99.txt"
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == large

    # Inline content should have file reference
    assert "workspace/tool_results/call_99.txt" in evicted
    assert "read_file" in evicted
    assert len(evicted) < len(large)


def test_force_evict_writes_exempt_tool_result_for_round_aggregate_overflow(tmp_path):
    from app.kernel.engine import _maybe_evict_tool_result

    large = "READ_FILE_RESULT\n" * 5000
    eviction_dir = tmp_path / "tool_results"

    evicted = _maybe_evict_tool_result(
        "read_file",
        "call_force",
        large,
        eviction_dir=eviction_dir,
        force=True,
        reason="round aggregate budget",
    )

    written_file = eviction_dir / "call_force.txt"
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == large
    assert "workspace/tool_results/call_force.txt" in evicted
    assert "round aggregate budget" in evicted
    assert len(evicted) < len(large)


@pytest.mark.asyncio
async def test_large_tool_result_evicted_in_kernel_loop():
    """Kernel evicts large tool results inline during the LLM loop."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c1", "function": {"name": "run_code", "arguments": '{"code":"test"}'}}],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 3},
            ),
        ]
    )

    large_result = "RESULT_LINE\n" * 5000  # ~60000 chars (exceeds 50K eviction threshold)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=5,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {
                    "type": "function",
                    "function": {"name": "run_code", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda *_a, **_kw: large_result,
            persist_memory=lambda **kw: None,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "search"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"

    # The second LLM call should have received the evicted (truncated) tool result
    second_call_messages = fake_client.calls[1]["messages"]
    tool_msg = [m for m in second_call_messages if m.role == "tool"][0]
    assert len(tool_msg.content) < len(large_result)
    assert "truncated" in tool_msg.content


@pytest.mark.asyncio
async def test_empty_tool_result_is_wrapped_with_actionable_message():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"empty.txt"}'}}],
                reasoning_content=None,
                usage={"total_tokens": 3},
            ),
            SimpleNamespace(content="handled empty", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda *_a, **_kw: "",
            persist_memory=lambda **kw: None,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "read empty file"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "handled empty"
    tool_msg = [m for m in fake_client.calls[1]["messages"] if m.role == "tool"][0]
    assert "[Tool returned empty result]" in tool_msg.content
    tool_part = [p for p in result.parts if p.get("type") == "tool_call"][0]
    assert "[Tool returned empty result]" in tool_part["result"]


@pytest.mark.asyncio
async def test_persist_memory_called_on_max_rounds_exceeded():
    """persist_memory must be called even when max tool rounds exhausted."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
    )
    persist_calls: list[dict] = []

    # 3 rounds of tool calls, max_tool_rounds=2 → will exceed
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": f"c{i}", "function": {"name": "list_files", "arguments": "{}"}}],
                reasoning_content=None,
                usage={"total_tokens": 3},
            )
            for i in range(3)
        ]
    )

    async def persist_memory(**kwargs):
        persist_calls.append(kwargs)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=2,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {
                    "type": "function",
                    "function": {"name": "list_files", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda *_a, **_kw: "ok",
            persist_memory=persist_memory,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "keep going"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-max",
        )
    )

    assert "tool-round limit" in result.content
    assert "continue" in result.content.lower()
    assert len(persist_calls) == 1, f"Expected 1 persist call, got {len(persist_calls)}"
    assert persist_calls[0]["session_id"] == "sess-max"


@pytest.mark.asyncio
async def test_turn_token_budget_stops_before_next_tool_round():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}],
                reasoning_content=None,
                usage={"total_tokens": 50},
            ),
            SimpleNamespace(content="must not run", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )
    executed: list[str] = []
    persist_calls: list[dict] = []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                turn_token_budget=40,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda tool_name, *_a, **_kw: executed.append(tool_name) or "tool result",
            persist_memory=lambda **kw: persist_calls.append(kw),
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "respect budget"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-budget",
        )
    )

    assert result.content.startswith("[Runtime Limit]")
    assert "higher turn budget" not in result.content
    assert executed == []
    assert len(fake_client.calls) == 1
    assert persist_calls[0]["session_id"] == "sess-budget"


@pytest.mark.asyncio
async def test_turn_token_budget_ignores_cached_prompt_reads_before_tool_round():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.token_tracker import extract_usage_tokens

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}],
                reasoning_content=None,
                usage={
                    "total_tokens": 1000,
                    "prompt_tokens": 980,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 960},
                },
            ),
            SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )
    executed: list[str] = []
    persist_calls: list[dict] = []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                turn_token_budget=100,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda tool_name, *_a, **_kw: executed.append(tool_name) or "tool result",
            persist_memory=lambda **kw: persist_calls.append(kw),
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=extract_usage_tokens,
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "respect effective budget"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-budget-cache",
        )
    )

    assert result.content == "done"
    assert executed == ["read_file"]
    assert len(persist_calls) == 1
    persisted_text = "\n".join(
        str(message.get("content") or "")
        for message in persist_calls[0].get("messages", [])
        if isinstance(message, dict)
    )
    assert "[Runtime Limit]" not in persisted_text
    assert len(fake_client.calls) == 2


@pytest.mark.asyncio
async def test_persist_memory_called_on_llm_error():
    """persist_memory must be called when LLM returns an error (after fallback exhausted)."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.llm_utils import LLMError

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
    )
    persist_calls: list[dict] = []

    class _ErrorClient:
        async def stream(self, **kwargs):
            raise LLMError("rate limit exceeded")

        async def close(self):
            pass

    async def persist_memory(**kwargs):
        persist_calls.append(kwargs)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=5,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: _ErrorClient(),
            execute_tool=lambda *_a, **_kw: "ok",
            persist_memory=persist_memory,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-err",
        )
    )

    assert "LLM Error" in result.content
    assert len(persist_calls) == 1
    assert persist_calls[0]["session_id"] == "sess-err"


class TestPromptTooLongDetection:
    """_is_prompt_too_long detects PTL errors from various providers."""

    def test_openai_context_length_exceeded(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("HTTP 400: context_length_exceeded - This model's maximum context length is 128000 tokens")
        assert _is_prompt_too_long(exc) is True

    def test_anthropic_too_long(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception(
            "Anthropic stream error (invalid_request_error): prompt is too long: 210000 tokens > 200000 maximum"
        )
        assert _is_prompt_too_long(exc) is True

    def test_gemini_request_too_large(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("HTTP 400: request too large for model")
        assert _is_prompt_too_long(exc) is True

    def test_input_too_long(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("input too long for this model")
        assert _is_prompt_too_long(exc) is True

    def test_unrelated_error_not_detected(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("HTTP 500: Internal server error")
        assert _is_prompt_too_long(exc) is False

    def test_rate_limit_not_detected(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("Rate limited after 3 attempts: 429 Too Many Requests")
        assert _is_prompt_too_long(exc) is False

    def test_connection_error_not_detected(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("Connection failed after 3 attempts: timeout")
        assert _is_prompt_too_long(exc) is False
