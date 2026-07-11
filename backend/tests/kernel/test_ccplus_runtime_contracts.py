from __future__ import annotations


def test_invocation_result_has_explicit_terminal_reason_default():
    from app.kernel.contracts import InvocationResult, TerminalReason

    result = InvocationResult(content="done")

    assert result.terminal_reason == TerminalReason.TURN_STOP


def test_kernel_terminal_result_helpers_stamp_reasons():
    from app.kernel.contracts import TerminalReason
    from app.kernel.engine import _build_cancelled_result, _build_error_result

    error = _build_error_result("failed", terminal_reason=TerminalReason.TENANT_RESOLUTION_ERROR)
    cancelled = _build_cancelled_result([], [], final_tools=[])

    assert error.terminal_reason == TerminalReason.TENANT_RESOLUTION_ERROR
    assert cancelled.terminal_reason == TerminalReason.USER_CANCEL


def test_agent_invocation_result_can_propagate_terminal_reason():
    from app.kernel.contracts import TerminalReason
    from app.runtime.invoker import AgentInvocationResult

    result = AgentInvocationResult(content="blocked", terminal_reason=TerminalReason.HOOK_STOPPED)

    assert result.terminal_reason == TerminalReason.HOOK_STOPPED


def test_seal_orphan_tool_uses_appends_synthetic_tool_results():
    from app.kernel.contracts import TerminalReason
    from app.kernel.engine import _seal_orphan_tool_uses
    from app.services.llm_utils import LLMMessage

    messages = [
        LLMMessage(
            role="assistant",
            content="calling",
            tool_calls=[{"id": "call_missing", "function": {"name": "read_file", "arguments": "{}"}}],
        )
    ]

    added = _seal_orphan_tool_uses(messages, terminal_reason=TerminalReason.USER_CANCEL)

    assert added == 1
    assert messages[-1].role == "tool"
    assert messages[-1].tool_call_id == "call_missing"
    assert "user_cancel" in str(messages[-1].content)


def test_seal_orphan_tool_uses_does_not_duplicate_existing_results():
    from app.kernel.contracts import TerminalReason
    from app.kernel.engine import _seal_orphan_tool_uses
    from app.services.llm_utils import LLMMessage

    messages = [
        LLMMessage(
            role="assistant",
            content="calling",
            tool_calls=[{"id": "call_done", "function": {"name": "read_file", "arguments": "{}"}}],
        ),
        LLMMessage(role="tool", tool_call_id="call_done", content="done"),
    ]

    added = _seal_orphan_tool_uses(messages, terminal_reason=TerminalReason.USER_CANCEL)

    assert added == 0
    assert len(messages) == 2


def test_tool_result_eviction_is_exclusive_and_hashes_conflicts(tmp_path):
    from app.kernel.engine import _maybe_evict_tool_result

    first = "x" * 60_000
    second = "y" * 60_000

    first_inline = _maybe_evict_tool_result("send_email", "call_same", first, tmp_path)
    first_file = tmp_path / "call_same.txt"
    assert first_file.read_text(encoding="utf-8") == first
    assert "workspace/tool_results/call_same.txt" in first_inline

    second_inline = _maybe_evict_tool_result("send_email", "call_same", second, tmp_path)

    assert first_file.read_text(encoding="utf-8") == first
    assert "workspace/tool_results/call_same-" in second_inline
    hashed_name = second_inline.split("workspace/tool_results/", 1)[1].split(" ", 1)[0]
    assert (tmp_path / hashed_name).read_text(encoding="utf-8") == second


def test_tool_content_envelope_carries_ccplus_side_effect_channels():
    from app.tools.result_envelope import ToolContentEnvelope

    envelope = ToolContentEnvelope(
        text="updated",
        new_messages=({"role": "assistant", "content": "side-effect"},),
        context_modifier={"replace": {"key": "value"}},
        t0_refs=("t0://segment/event",),
        permission_request={"kind": "approval", "risk": "high"},
        terminal_signal="waiting_for_user",
    )

    assert envelope.new_messages[0]["content"] == "side-effect"
    assert envelope.context_modifier["replace"]["key"] == "value"
    assert envelope.t0_refs == ("t0://segment/event",)
    assert envelope.permission_request["risk"] == "high"
    assert envelope.terminal_signal == "waiting_for_user"


def test_ccplus_v1_profiles_default_to_governed_safe_values():
    from app.runtime.ccplus_contracts import (
        AgentSessionV1,
        CompactionLifecycleV1,
        ContextPolicyV1,
        ExtensionRegistryV1,
        GovernanceCapabilityDescriptorV1,
        HookLifecycleV1,
        PendingToolFrameV1,
        PermissionMode,
        PermissionCheckpointV1,
        PermissionProfileV1,
        SandboxProfile,
        ToolCallLifecycleV1,
        ToolExecutionFrameV1,
        ToolResultV1,
        ToolSpecV1,
        ResolvedAssetRefV1,
        TurnStateV1,
        TurnStatus,
        TruthEvidencePackV1,
    )

    permission = PermissionProfileV1()
    assert permission.mode == PermissionMode.DEFAULT
    assert permission.sandbox == SandboxProfile.WORKSPACE_WRITE
    assert permission.writable_roots == ("workspace/",)
    assert permission.default_decision == "escalate"

    turn = TurnStateV1(session_id="session-1", runtime_task_id="task-1")
    assert turn.status == TurnStatus.ACCEPTED
    assert turn.terminal_reason is None

    tool_spec = ToolSpecV1(name="read_file", capability="filesystem.read", read_only=True)
    assert tool_spec.defer_loading is False

    tool_result = ToolResultV1(text="ok", t0_refs=("t0://event",))
    assert tool_result.t0_refs == ("t0://event",)

    context = ContextPolicyV1(model_window=128000)
    assert context.autocompact_failure_breaker_limit == 3
    assert context.breaker_half_open_seconds == 600

    registry = ExtensionRegistryV1()
    assert registry.extensions == ()

    session = AgentSessionV1(session_id="session-1", permission_profile=permission, context_policy=context)
    assert session.session_id == "session-1"

    tool_call = ToolCallLifecycleV1(
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="send_email",
        lifecycle_state="validated",
        governance_decisions=("l0_allow", "l1_missing_policy_escalate_l3"),
    )
    assert tool_call.lifecycle_state == "validated"
    assert tool_call.governance_decisions[-1] == "l1_missing_policy_escalate_l3"

    asset_ref = ResolvedAssetRefV1(
        asset_id="asset-1",
        asset_type="skill",
        native_key="skill:agent:a:research",
        revision_id="revision-3",
        revision_version=3,
        content_hash="sha256:skill-v3",
        source_ref="agent:a/skills/research",
    )
    execution = ToolExecutionFrameV1(
        tool_call_id="call-1",
        tool_name="send_email",
        executor="tool_runtime_service",
        sandbox_profile=SandboxProfile.EXTERNAL_SANDBOX,
        input_hash="sha256:input",
        resolved_asset_refs=(asset_ref,),
    )
    assert execution.executor == "tool_runtime_service"
    assert execution.resolved_asset_refs == (asset_ref,)

    pending = PendingToolFrameV1(
        permission_request_id="perm-1",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="send_email",
        arguments={"to": "user@example.com"},
        permission_profile=permission,
        knowledge_refs=("truth://policy/1",),
        hook_refs=("hook://pre/1",),
    )
    assert pending.arguments["to"] == "user@example.com"
    assert pending.permission_profile.mode == PermissionMode.DEFAULT

    checkpoint = PermissionCheckpointV1(
        permission_request_id="perm-1",
        decision="allow_once",
        pending_frame=pending,
        resolver_user_id="user-1",
    )
    assert checkpoint.pending_frame.tool_call_id == "call-1"

    hook = HookLifecycleV1(
        hook_run_id="hook-1",
        event="pre_tool_use",
        source="plugin:test",
        trust_level="tenant_approved",
        lifecycle_state="blocking_capable",
        original_hash="sha256:old",
        modified_hash="sha256:new",
        decision="allow_modified",
        span_ref="span-1",
    )
    assert hook.lifecycle_state == "blocking_capable"
    assert hook.modified_hash == "sha256:new"

    compaction = CompactionLifecycleV1(
        compaction_id="compact-1",
        session_id="session-1",
        trigger="request_preflight",
        before_message_count=20,
        after_message_count=8,
        pre_hook_refs=("hook://pre_compaction/1",),
        post_hook_refs=("hook://post_compaction/1",),
        recovery_manifest_ref="workspace/runtime_artifacts/recovery_manifest.json",
    )
    assert compaction.trigger == "request_preflight"
    assert compaction.recovery_manifest_ref is not None

    evidence = TruthEvidencePackV1(
        evidence_id="truth-1",
        query="company outbound email policy",
        source_refs=("knowledge://policy/email",),
        citations=("policy/email#L1-L8",),
        acl_scope="tenant",
        digest="sha256:evidence",
        provider="knowledge_core",
    )
    assert evidence.source_refs == ("knowledge://policy/email",)

    descriptor = GovernanceCapabilityDescriptorV1(
        name="web_search",
        layer="agent_base",
        tools=("web_search",),
        default_enabled=True,
        l2_visible=False,
        enterprise_toggleable=False,
        source="core",
    )
    assert descriptor.layer == "agent_base"
    assert descriptor.enterprise_toggleable is False
