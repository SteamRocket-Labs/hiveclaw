"""Tests for the runtime hook/event bus."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.runtime.hooks import HookContext, HookEvent, HookRegistry, HookResult


@pytest.fixture
def registry():
    reg = HookRegistry()
    yield reg
    reg.clear()


class TestHookRegistry:
    """Core hook registration and emission."""

    @pytest.mark.asyncio
    async def test_sync_handler_called(self, registry) -> None:
        calls = []

        def handler(ctx: HookContext):
            calls.append(ctx.tool_name)

        registry.register(HookEvent.POST_TOOL_USE, handler)
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="read_file"))
        assert calls == ["read_file"]

    @pytest.mark.asyncio
    async def test_async_handler_called(self, registry) -> None:
        calls = []

        async def handler(ctx: HookContext):
            calls.append(ctx.tool_name)

        registry.register(HookEvent.POST_TOOL_USE, handler)
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="write_file"))
        assert calls == ["write_file"]

    @pytest.mark.asyncio
    async def test_pre_tool_use_can_block(self, registry) -> None:
        def blocker(ctx: HookContext):
            return HookResult(block=True, reason="dangerous tool")

        registry.register(HookEvent.PRE_TOOL_USE, blocker)
        result = await registry.emit(HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="execute_code"))
        assert result is not None
        assert result.block is True
        assert "dangerous" in result.reason

    @pytest.mark.asyncio
    async def test_pre_tool_use_allows_when_no_block(self, registry) -> None:
        def allower(ctx: HookContext):
            return HookResult(block=False)

        registry.register(HookEvent.PRE_TOOL_USE, allower)
        result = await registry.emit(HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="read_file"))
        assert result is None  # Non-blocking results are not returned

    @pytest.mark.asyncio
    async def test_multiple_handlers_run_in_order(self, registry) -> None:
        order = []

        def first(ctx):
            order.append(1)

        def second(ctx):
            order.append(2)

        registry.register(HookEvent.SESSION_START, first)
        registry.register(HookEvent.SESSION_START, second)
        await registry.emit(HookContext(event=HookEvent.SESSION_START))
        assert order == [1, 2]

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash(self, registry) -> None:
        from app.memory.metrics import reset_all, snapshot

        reset_all()
        calls = []

        def crasher(ctx):
            raise ValueError("boom")

        def survivor(ctx):
            calls.append("survived")

        registry.register(HookEvent.POST_TOOL_USE, crasher)
        registry.register(HookEvent.POST_TOOL_USE, survivor)
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE))
        assert calls == ["survived"]
        assert snapshot()["hook_failure_total"]["post_tool_use:registry:ValueError"] == 1

    @pytest.mark.asyncio
    async def test_matcher_filters_hook_execution(self, registry) -> None:
        calls = []

        def handler(ctx: HookContext):
            calls.append(ctx.tool_name)

        registry.register(
            HookEvent.POST_TOOL_USE,
            handler,
            matcher=lambda ctx: ctx.tool_name == "write_file",
        )
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="read_file"))
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="write_file"))
        assert calls == ["write_file"]

    @pytest.mark.asyncio
    async def test_matcher_can_guard_pre_tool_modifier(self, registry) -> None:
        def modifier(ctx: HookContext):
            return HookResult(block=False, modified_args={"path": "/guarded"})

        registry.register(
            HookEvent.PRE_TOOL_USE,
            modifier,
            matcher=lambda ctx: ctx.tool_name == "read_file",
        )
        ignored = await registry.emit(HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="write_file"))
        assert ignored is None
        applied = await registry.emit(HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="read_file"))
        assert applied is not None
        assert applied.modified_args == {"path": "/guarded"}

    @pytest.mark.asyncio
    async def test_register_spec_filters_by_tool_and_source(self, registry) -> None:
        calls = []

        def handler(ctx: HookContext):
            calls.append((ctx.tool_name, ctx.source))

        registry.register_spec(
            HookEvent.POST_TOOL_USE,
            handler,
            {
                "tool_names": ["write_file"],
                "sources": ["websocket"],
            },
        )
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="write_file", source="agent"))
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="read_file", source="websocket"))
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="write_file", source="websocket"))
        assert calls == [("write_file", "websocket")]

    @pytest.mark.asyncio
    async def test_register_spec_filters_by_metadata(self, registry) -> None:
        calls = []

        def handler(ctx: HookContext):
            calls.append(ctx.metadata.get("status"))

        registry.register_spec(
            HookEvent.DELEGATION_END,
            handler,
            {
                "metadata_equals": {"status": "failure"},
                "metadata_truthy": ["failed"],
            },
        )
        await registry.emit(
            HookContext(
                event=HookEvent.DELEGATION_END,
                metadata={"status": "success", "failed": False},
            )
        )
        await registry.emit(
            HookContext(
                event=HookEvent.DELEGATION_END,
                metadata={"status": "failure", "failed": True},
            )
        )
        assert calls == ["failure"]

    @pytest.mark.asyncio
    async def test_register_many_accepts_mixed_specs(self, registry) -> None:
        calls = []

        def plain(ctx: HookContext):
            calls.append(("plain", ctx.tool_name))

        def filtered(ctx: HookContext):
            calls.append(("filtered", ctx.tool_name))

        from app.runtime.hooks import HookRegistrationSpec

        registry.register_many(
            [
                HookRegistrationSpec(event=HookEvent.POST_TOOL_USE, handler=plain),
                HookRegistrationSpec(
                    event=HookEvent.POST_TOOL_USE,
                    handler=filtered,
                    matcher_spec={"tool_names": ["write_file"]},
                ),
            ]
        )
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="read_file"))
        await registry.emit(HookContext(event=HookEvent.POST_TOOL_USE, tool_name="write_file"))
        assert calls == [
            ("plain", "read_file"),
            ("plain", "write_file"),
            ("filtered", "write_file"),
        ]

    def test_describe_registrations_exports_key_and_matcher_metadata(self, registry) -> None:
        from app.runtime.hooks import HookRegistrationSpec

        def plain(ctx: HookContext):
            return None

        def filtered(ctx: HookContext):
            return None

        registry.register_many(
            [
                HookRegistrationSpec(
                    event=HookEvent.POST_TOOL_USE,
                    handler=plain,
                    key="plain.post_tool",
                ),
                HookRegistrationSpec(
                    event=HookEvent.POST_TOOL_USE,
                    handler=filtered,
                    key="filtered.post_tool",
                    matcher_spec={"tool_names": ["write_file"], "sources": ["websocket"]},
                ),
            ]
        )

        exported = registry.describe_registrations()

        assert exported == [
            {
                "event": "post_tool_use",
                "handler_name": "plain",
                "key": "plain.post_tool",
                "profile_name": None,
                "has_matcher": False,
                "matcher_spec": None,
            },
            {
                "event": "post_tool_use",
                "handler_name": "filtered",
                "key": "filtered.post_tool",
                "profile_name": None,
                "has_matcher": True,
                "matcher_spec": {
                    "tool_names": ["write_file"],
                    "agent_ids": [],
                    "tenant_ids": [],
                    "sources": ["websocket"],
                    "session_ids": [],
                    "metadata_equals": {},
                    "metadata_truthy": [],
                },
            },
        ]

    def test_load_registration_specs_parses_external_config(self) -> None:
        from app.runtime.hooks import HookMatcherSpec, load_registration_specs

        def plain(ctx: HookContext):
            return None

        def filtered(ctx: HookContext):
            return None

        registrations = load_registration_specs(
            [
                {
                    "event": "post_tool_use",
                    "handler": "plain",
                    "key": "plain.post_tool",
                },
                {
                    "event": "post_tool_failure",
                    "handler": "filtered",
                    "key": "filtered.post_tool_failure",
                    "matcher_spec": {
                        "tool_names": ["write_file"],
                        "metadata_truthy": ["failed"],
                    },
                },
            ],
            {
                "plain": plain,
                "filtered": filtered,
            },
        )

        assert len(registrations) == 2
        assert registrations[0].handler is plain
        assert registrations[0].handler_name == "plain"
        assert registrations[1].handler is filtered
        assert registrations[1].handler_name == "filtered"
        assert registrations[1].matcher_spec == HookMatcherSpec(
            tool_names=("write_file",),
            metadata_truthy=("failed",),
        )

    def test_load_registration_specs_accepts_if_condition_shorthand(self) -> None:
        from app.runtime.hooks import HookMatcherSpec, load_registration_specs

        def filtered(ctx: HookContext):
            return None

        registrations = load_registration_specs(
            [
                {
                    "event": "post_tool_failure",
                    "handler": "filtered",
                    "key": "filtered.post_tool_failure",
                    "if": "tool=write_file,source=websocket,meta.status=failure,meta.failed=true",
                },
            ],
            {
                "filtered": filtered,
            },
        )

        assert registrations[0].matcher_spec == HookMatcherSpec(
            tool_names=("write_file",),
            sources=("websocket",),
            metadata_equals=(("status", "failure"),),
            metadata_truthy=("failed",),
        )

    def test_load_registration_specs_rejects_unknown_if_condition_key(self) -> None:
        from app.runtime.hooks import load_registration_specs

        def filtered(ctx: HookContext):
            return None

        with pytest.raises(ValueError, match="Unknown hook if-clause"):
            load_registration_specs(
                [
                    {
                        "event": "post_tool_use",
                        "handler": "filtered",
                        "key": "filtered.post_tool_use",
                        "if": "unknown=value",
                    },
                ],
                {"filtered": filtered},
            )

    def test_load_registration_specs_rejects_unknown_handler(self) -> None:
        from app.runtime.hooks import load_registration_specs

        def plain(ctx: HookContext):
            return None

        with pytest.raises(ValueError, match="Unknown hook handler"):
            load_registration_specs(
                [{"event": "post_tool_use", "handler": "missing"}],
                {"plain": plain},
            )

    def test_load_registration_specs_requires_stable_unique_keys(self) -> None:
        from app.runtime.hooks import load_registration_specs

        def plain(ctx: HookContext):
            return None

        with pytest.raises(ValueError, match="missing a stable key"):
            load_registration_specs(
                [{"event": "post_tool_use", "handler": "plain"}],
                {"plain": plain},
            )

        with pytest.raises(ValueError, match="Duplicate hook key"):
            load_registration_specs(
                [
                    {"event": "post_tool_use", "handler": "plain", "key": "duplicate.key"},
                    {"event": "post_tool_failure", "handler": "plain", "key": "duplicate.key"},
                ],
                {"plain": plain},
            )

    def test_load_registration_specs_accepts_named_matcher_profiles(self) -> None:
        from app.runtime.hooks import HookMatcherSpec, load_registration_specs

        def filtered(ctx: HookContext):
            return None

        registrations = load_registration_specs(
            [
                {
                    "event": "post_tool_use",
                    "handler": "filtered",
                    "key": "filtered.profiled",
                    "profile": "workspace_writes",
                },
            ],
            {"filtered": filtered},
            matcher_profiles={
                "workspace_writes": {
                    "tool_names": ["write_file", "edit_file"],
                    "sources": ["websocket"],
                    "metadata_truthy": ["workspace_bound"],
                }
            },
        )

        assert registrations[0].profile_name == "workspace_writes"
        assert registrations[0].matcher_spec == HookMatcherSpec(
            tool_names=("write_file", "edit_file"),
            sources=("websocket",),
            metadata_truthy=("workspace_bound",),
        )

    def test_load_registration_specs_rejects_unknown_matcher_profile(self) -> None:
        from app.runtime.hooks import load_registration_specs

        def filtered(ctx: HookContext):
            return None

        with pytest.raises(ValueError, match="Unknown hook matcher profile"):
            load_registration_specs(
                [
                    {
                        "event": "post_tool_use",
                        "handler": "filtered",
                        "key": "filtered.profiled",
                        "profile": "missing_profile",
                    },
                ],
                {"filtered": filtered},
                matcher_profiles={"workspace_writes": {"tool_names": ["write_file"]}},
            )

    @pytest.mark.asyncio
    async def test_matcher_spec_scopes_by_agent_and_tenant(self, registry) -> None:
        """Plugin hooks must be tenant/agent scoped before they can enter tool governance."""
        calls: list[str] = []

        def handler(ctx: HookContext):
            calls.append(str(ctx.agent_id))

        registry.register_spec(
            HookEvent.PRE_TOOL_USE,
            handler,
            {
                "tool_names": ["write_file"],
                "agent_ids": ["agent-1"],
                "tenant_ids": ["tenant-1"],
            },
            key="plugin.tenant-1.agent-1.write_file",
        )

        await registry.emit(
            HookContext(
                event=HookEvent.PRE_TOOL_USE,
                agent_id="agent-2",
                tool_name="write_file",
                metadata={"tenant_id": "tenant-1"},
            )
        )
        await registry.emit(
            HookContext(
                event=HookEvent.PRE_TOOL_USE,
                agent_id="agent-1",
                tool_name="write_file",
                metadata={"tenant_id": "tenant-2"},
            )
        )
        await registry.emit(
            HookContext(
                event=HookEvent.PRE_TOOL_USE,
                agent_id="agent-1",
                tool_name="write_file",
                metadata={"tenant_id": "tenant-1"},
            )
        )

        assert calls == ["agent-1"]

    @pytest.mark.asyncio
    async def test_no_handlers_returns_none(self, registry) -> None:
        result = await registry.emit(HookContext(event=HookEvent.SESSION_CLOSE))
        assert result is None

    def test_unregister_removes_handler(self, registry) -> None:
        def handler(ctx):
            pass

        registry.register(HookEvent.POST_TOOL_USE, handler)
        assert registry.handler_count(HookEvent.POST_TOOL_USE) == 1
        registry.unregister(HookEvent.POST_TOOL_USE, handler)
        assert registry.handler_count(HookEvent.POST_TOOL_USE) == 0

    def test_clear_removes_all(self, registry) -> None:
        registry.register(HookEvent.PRE_TOOL_USE, lambda ctx: None)
        registry.register(HookEvent.POST_TOOL_USE, lambda ctx: None)
        registry.clear()
        assert registry.handler_count(HookEvent.PRE_TOOL_USE) == 0
        assert registry.handler_count(HookEvent.POST_TOOL_USE) == 0

    @pytest.mark.asyncio
    async def test_modified_args_returned_for_pre_tool(self, registry) -> None:
        def modifier(ctx: HookContext):
            return HookResult(block=False, modified_args={"path": "/safe/path"})

        registry.register(HookEvent.PRE_TOOL_USE, modifier)
        result = await registry.emit(HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="read_file"))
        assert result is not None
        assert result.block is False
        assert result.modified_args == {"path": "/safe/path"}

    @pytest.mark.asyncio
    async def test_pre_tool_use_modifiers_chain_in_order(self, registry) -> None:
        def first(ctx: HookContext):
            return HookResult(block=False, modified_args={"path": "/safe/path"})

        def second(ctx: HookContext):
            assert ctx.tool_args == {"path": "/safe/path"}
            return HookResult(block=False, modified_args={"path": "/safer/path"})

        registry.register(HookEvent.PRE_TOOL_USE, first)
        registry.register(HookEvent.PRE_TOOL_USE, second)
        result = await registry.emit(
            HookContext(
                event=HookEvent.PRE_TOOL_USE,
                tool_name="read_file",
                tool_args={"path": "/unsafe/path"},
            )
        )
        assert result is not None
        assert result.modified_args == {"path": "/safer/path"}


class TestHookContext:
    """HookContext data structure."""

    def test_all_fields_optional_except_event(self) -> None:
        ctx = HookContext(event=HookEvent.SESSION_START)
        assert ctx.event == HookEvent.SESSION_START
        assert ctx.agent_id is None
        assert ctx.tool_name is None
        assert ctx.metadata == {}

    def test_metadata_default_factory(self) -> None:
        ctx1 = HookContext(event=HookEvent.SESSION_START)
        ctx2 = HookContext(event=HookEvent.SESSION_CLOSE)
        ctx1.metadata["key"] = "val"
        assert "key" not in ctx2.metadata

    def test_messages_and_source_fields(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        ctx = HookContext(
            event=HookEvent.RESPONSE_COMPLETE,
            agent_id="test-agent",
            messages=msgs,
            source="websocket",
        )
        assert ctx.messages == msgs
        assert ctx.source == "websocket"

    def test_messages_defaults_to_none(self) -> None:
        ctx = HookContext(event=HookEvent.SESSION_START)
        assert ctx.messages is None
        assert ctx.source is None


class TestHookEvents:
    """Verify hook events exist and registry initializes them."""

    def test_all_events_defined(self) -> None:
        events = list(HookEvent)
        assert len(events) == 33

    def test_new_events_exist(self) -> None:
        assert HookEvent.USER_PROMPT_SUBMIT == "user_prompt_submit"
        assert HookEvent.RESPONSE_COMPLETE == "response_complete"
        assert HookEvent.SESSION_IDLE == "session_idle"
        assert HookEvent.SESSION_CLOSE == "session_close"
        assert HookEvent.SESSION_END == "session_end"
        assert HookEvent.STOP == "stop"
        assert HookEvent.STOP_FAILURE == "stop_failure"
        assert HookEvent.SUBAGENT_START == "subagent_start"
        assert HookEvent.SUBAGENT_STOP == "subagent_stop"
        assert HookEvent.TRIGGER_END == "trigger_end"
        assert HookEvent.HEARTBEAT_TICK_END == "heartbeat_tick_end"
        assert HookEvent.DREAM_END == "dream_end"
        assert HookEvent.NOTIFICATION == "notification"
        assert HookEvent.PERMISSION_REQUEST == "permission_request"
        assert HookEvent.TASK_CREATED == "task_created"
        assert HookEvent.TASK_COMPLETED == "task_completed"
        assert HookEvent.ELICITATION == "elicitation"
        assert HookEvent.CONFIG_CHANGE == "config_change"
        assert HookEvent.INSTRUCTIONS_LOADED == "instructions_loaded"
        assert HookEvent.WORKSPACE_CONTEXT_CHANGED == "workspace_context_changed"
        assert HookEvent.ARTIFACT_CHANGED == "artifact_changed"
        assert HookEvent.TEAM_CREATED == "team_created"
        assert HookEvent.TEAM_CLOSED == "team_closed"
        assert HookEvent.TEAMMATE_IDLE == "teammate_idle"

    def test_session_end_restored_for_cc_parity(self) -> None:
        event_values = [e.value for e in HookEvent]
        assert "session_end" in event_values

    @pytest.mark.asyncio
    async def test_runtime_config_can_disable_registration_key(self) -> None:
        from app.runtime.hooks import configure_hook_runtime, reset_hook_runtime_config

        reset_hook_runtime_config()
        reg = HookRegistry()
        calls: list[str] = []

        async def handler(_ctx):
            calls.append("called")

        reg.register(HookEvent.SESSION_END, handler, key="test.disable")
        configure_hook_runtime(key="test.disable", enabled=False)

        await reg.emit(HookContext(event=HookEvent.SESSION_END))

        assert calls == []
        reset_hook_runtime_config()

    @pytest.mark.asyncio
    async def test_runtime_config_timeout_can_block_blocking_hook(self) -> None:
        from app.runtime.hooks import configure_hook_runtime, reset_hook_runtime_config

        reset_hook_runtime_config()
        reg = HookRegistry()

        async def slow_handler(_ctx):
            await asyncio.sleep(0.05)

        reg.register(HookEvent.USER_PROMPT_SUBMIT, slow_handler, key="test.timeout")
        configure_hook_runtime(key="test.timeout", timeout_seconds=0.001, failure_policy="block")

        result = await reg.emit(HookContext(event=HookEvent.USER_PROMPT_SUBMIT, prompt="hello"))

        assert result is not None
        assert result.block is True
        assert "test.timeout" in result.reason
        reset_hook_runtime_config()

    @pytest.mark.asyncio
    async def test_runtime_config_can_disable_one_agent_without_disabling_others(self) -> None:
        from app.runtime.hooks import configure_hook_runtime, reset_hook_runtime_config

        reset_hook_runtime_config()
        agent_one = uuid4()
        agent_two = uuid4()
        reg = HookRegistry()
        calls: list[str] = []

        async def handler(ctx):
            calls.append(str(ctx.agent_id))

        reg.register(HookEvent.USER_PROMPT_SUBMIT, handler, key="test.scoped")
        configure_hook_runtime(key="test.scoped", agent_id=agent_one, enabled=False)

        await reg.emit(HookContext(event=HookEvent.USER_PROMPT_SUBMIT, agent_id=agent_one, prompt="one"))
        await reg.emit(HookContext(event=HookEvent.USER_PROMPT_SUBMIT, agent_id=agent_two, prompt="two"))

        assert calls == [str(agent_two)]
        reset_hook_runtime_config()

    def test_registry_initializes_all_events(self) -> None:
        reg = HookRegistry()
        for event in HookEvent:
            assert reg.handler_count(event) == 0
        reg.clear()

    @pytest.mark.asyncio
    async def test_response_complete_fires(self) -> None:
        reg = HookRegistry()
        calls = []
        reg.register(HookEvent.RESPONSE_COMPLETE, lambda ctx: calls.append(ctx.source))
        await reg.emit(
            HookContext(
                event=HookEvent.RESPONSE_COMPLETE,
                messages=[{"role": "assistant", "content": "hi"}],
                source="websocket",
            )
        )
        assert calls == ["websocket"]
        reg.clear()

    @pytest.mark.asyncio
    async def test_session_idle_fires(self) -> None:
        reg = HookRegistry()
        calls = []
        reg.register(HookEvent.SESSION_IDLE, lambda ctx: calls.append("idle"))
        await reg.emit(HookContext(event=HookEvent.SESSION_IDLE, messages=[]))
        assert calls == ["idle"]
        reg.clear()

    @pytest.mark.asyncio
    async def test_dream_end_fires(self) -> None:
        reg = HookRegistry()
        calls = []
        reg.register(HookEvent.DREAM_END, lambda ctx: calls.append(ctx.agent_id))
        await reg.emit(HookContext(event=HookEvent.DREAM_END, agent_id="agent-1"))
        assert calls == ["agent-1"]
        reg.clear()
