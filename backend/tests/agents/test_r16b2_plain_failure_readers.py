"""R16b round 2: every task reader hands the parent the same plain failure page.

Round 1 shipped the plain no-body page for ``read_runtime_result`` only; the three
task readers still returned ``status`` / ``reason_code`` / ``retryable`` /
``timed_out``, so the parent model could still quote internal codes (E2E run 12
wrote "原因标记为不可重试" after ``check_async_task``'s ``timed_out``). These tests
pin what the parent actually receives from each of the four readers, the loop
guard's shared counter over the plain page, and the closed recognition predicate.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

INTERNAL_MARKERS = ("reason_code", "retryable", "timed_out", "delegation_timeout", '"status"')


def _ref() -> str:
    return f"runtime-result://{uuid4()}/{'a' * 64}"


class TestOnePlainPageForAllFourReaders:
    """B1: one builder, plain sentences only, no per-reader copy."""

    def test_task_reader_page_is_the_same_builder_without_a_result_ref(self):
        from app.services.runtime_display_status import (
            DELEGATION_RESULT_READ_NOTE,
            delegation_failure_result_page,
            is_delegation_failure_result_page,
        )

        task_id = str(uuid4())
        page = delegation_failure_result_page(task_id=task_id, status="failed", metadata={"timed_out": True})
        assert page == {
            "schema": "hive.runtime_result_page.v1",
            "task_id": task_id,
            "result": "没在时限内完成这项任务。",
            "note": DELEGATION_RESULT_READ_NOTE,
        }
        assert is_delegation_failure_result_page(page)
        serialized = json.dumps(page, ensure_ascii=False)
        for marker in INTERNAL_MARKERS:
            assert marker not in serialized

    def test_result_reader_page_still_carries_the_callers_ref(self):
        from app.services.runtime_display_status import delegation_failure_result_page, is_delegation_failure_result_page

        ref, task_id = _ref(), str(uuid4())
        page = delegation_failure_result_page(
            result_ref=ref, task_id=task_id, status="failed", metadata={"error_code": "tool_budget"}
        )
        assert set(page) == {"schema", "result_ref", "task_id", "result", "note"}
        assert page["result_ref"] == ref and page["result"] == "这次用完了用量预算，没有完成。"
        assert is_delegation_failure_result_page(page)

    def test_predicate_accepts_only_the_builders_key_shapes(self):
        from app.services.runtime_display_status import (
            DELEGATION_RESULT_READ_NOTE,
            is_delegation_failure_result_page,
        )

        note = DELEGATION_RESULT_READ_NOTE
        base = {"schema": "hive.runtime_result_page.v1", "result": "这次执行失败，没有完成。", "note": note}
        for extra in ({}, {"task_id": str(uuid4())}, {"result_ref": _ref()},
                      {"result_ref": _ref(), "task_id": str(uuid4())}):
            assert is_delegation_failure_result_page({**base, **extra})
        # Widened shapes never certify as this projection.
        assert not is_delegation_failure_result_page({**base, "content": "x"})
        assert not is_delegation_failure_result_page({**base, "note": note + " PRIVATE"})
        assert not is_delegation_failure_result_page({**base, "result": "PRIVATE_PARTIAL_BODY"})
        assert not is_delegation_failure_result_page({**base, "result": []})  # unhashable, must not raise
        assert not is_delegation_failure_result_page({**base, "task_id": "PRIVATE"})
        assert not is_delegation_failure_result_page({**base, "task_id": str(uuid4()).upper()})
        assert not is_delegation_failure_result_page({**base, "task_id": 5})

    def test_the_fixed_note_tells_the_parent_not_to_retry_without_the_member(self):
        # S2: the no-retry instruction rides the same constant note, so the
        # denial predicate stays closed over fixed copy.
        from app.services.runtime_display_status import DELEGATION_RESULT_READ_NOTE, FAILED_DELEGATION_MODEL_INSTRUCTION

        assert FAILED_DELEGATION_MODEL_INSTRUCTION in DELEGATION_RESULT_READ_NOTE

    def test_a_plain_page_is_not_counted_as_a_failed_tool_call(self):
        # tools/service.py exempts the coded observation from failure counting;
        # the plain page must be exempt the same way.
        from app.services.runtime_display_status import delegation_failure_result_page
        from app.tools.service import _tool_result_failed

        task_id = str(uuid4())
        page = delegation_failure_result_page(task_id=task_id, status="failed", metadata={"error_code": "tool_budget"})
        assert _tool_result_failed(json.dumps(page, ensure_ascii=False)) is False
        envelope = {"ok": True, "runtime_task_id": task_id, "task": page}
        assert _tool_result_failed(json.dumps(envelope, ensure_ascii=False)) is False


class TestLoopGuardCountsPlainPagesAcrossAllFourReaders:
    """B1/S1: identical terminal reads over the plain page share one counter."""

    @staticmethod
    def _bodies(task_id: str, ref: str) -> list[tuple[str, dict, object]]:
        from app.services.runtime_display_status import delegation_failure_result_page

        reader_page = delegation_failure_result_page(
            task_id=task_id, status="failed", metadata={"error_code": "provider_error"}
        )
        result_page = delegation_failure_result_page(
            result_ref=ref, task_id=task_id, status="failed", metadata={"error_code": "provider_error"}
        )
        return [
            ("read_runtime_result", {"result_ref": ref}, result_page),
            ("check_async_task", {"task_id": task_id}, reader_page),
            ("task_output", {"runtime_task_id": task_id}, {"ok": True, "runtime_task_id": task_id, "task": reader_page}),
            ("list_async_tasks", {}, [reader_page]),
        ]

    def test_all_four_readers_returning_the_plain_page_abort_at_the_shared_threshold(self):
        from app.kernel.loop_guard import LoopGuard

        guard = LoopGuard()
        task_id, ref = str(uuid4()), _ref()
        bodies = self._bodies(task_id, ref)
        decision = None
        for index in range(guard.identical_tool_threshold):
            name, args, body = bodies[index % len(bodies)]
            decision = guard.observe_tool_result(name, args, json.dumps(body, ensure_ascii=False),
                machine_outcome="succeeded", side_effect_free=True)
            if index < guard.identical_tool_threshold - 1:
                assert decision is None
        assert decision is not None and decision.severity == "abort"
        assert decision.reason == "identical_terminal_task_read"
        assert decision.message == "任务结果未变化，已停止本次处理：这次调用模型失败，没有完成。"
        assert decision.trace_event["identical_terminal_read_count"] == guard.identical_tool_threshold
        assert guard.failed_tool_calls == 0, "Reading a failed child is still a successful tool read"

    def test_pagination_over_plain_pages_shares_the_counter_with_the_task_readers(self):
        from app.kernel.loop_guard import LoopGuard
        from app.services.runtime_display_status import delegation_failure_result_page

        guard = LoopGuard()
        task_id, ref = str(uuid4()), _ref()
        reader_page = delegation_failure_result_page(
            task_id=task_id, status="failed", metadata={"error_code": "provider_error"}
        )
        result_page = delegation_failure_result_page(
            result_ref=ref, task_id=task_id, status="failed", metadata={"error_code": "provider_error"}
        )
        decision = None
        for index in range(guard.identical_tool_threshold):
            name = "check_async_task" if index % 2 else "read_runtime_result"
            args = {"task_id": task_id} if name == "check_async_task" else {
                "result_ref": ref, "offset": index, "limit": index + 1}
            body = reader_page if name == "check_async_task" else result_page
            decision = guard.observe_tool_result(name, args, json.dumps(body, ensure_ascii=False),
                machine_outcome="succeeded", side_effect_free=True)
            if index < guard.identical_tool_threshold - 1:
                assert decision is None
        assert decision is not None and decision.severity == "abort"
        assert decision.trace_event["identical_terminal_read_count"] == guard.identical_tool_threshold

    def test_plain_page_with_the_bare_stop_sentence_does_not_double_it(self):
        # N2: a stored identical-terminal summary without the prefix must not
        # become "任务结果未变化，已停止本次处理：任务结果未变化，已停止本次处理。".
        from app.kernel.loop_guard import LoopGuard
        from app.services.runtime_display_status import delegation_failure_result_page

        guard = LoopGuard(identical_tool_threshold=1)
        task_id, ref = str(uuid4()), _ref()
        page = delegation_failure_result_page(result_ref=ref, task_id=task_id, status="failed",
            metadata={"error_code": "identical_terminal_task_read"}, result_summary="not a fixed sentence")
        assert page["result"] == "任务结果未变化，已停止本次处理。"
        decision = guard.observe_tool_result("read_runtime_result", {"result_ref": ref},
            json.dumps(page, ensure_ascii=False), machine_outcome="succeeded", side_effect_free=True)
        assert decision is not None and decision.severity == "abort"
        assert decision.message == "任务结果未变化，已停止本次处理。"

    def test_plain_page_with_a_prefixed_sentence_keeps_its_stored_detail(self):
        from app.kernel.loop_guard import LoopGuard
        from app.services.runtime_display_status import TERMINAL_TASK_READ_PREFIX, delegation_failure_result_page

        # A stored prefixed summary rides through unchanged …
        prefixed = TERMINAL_TASK_READ_PREFIX + "这次执行失败，没有完成。"
        guard = LoopGuard(identical_tool_threshold=1)
        task_id, ref = str(uuid4()), _ref()
        page = delegation_failure_result_page(result_ref=ref, task_id=task_id, status="failed",
            metadata={"error_code": "identical_terminal_task_read"}, result_summary=prefixed)
        assert page["result"] == prefixed
        decision = guard.observe_tool_result("read_runtime_result", {"result_ref": ref},
            json.dumps(page, ensure_ascii=False), machine_outcome="succeeded", side_effect_free=True)
        assert decision is not None and decision.message == prefixed
        # … and a would-be doubled summary never reaches the page at all: it is
        # not fixed terminal-read copy, so the page falls back to the bare stop
        # sentence instead of doubling it.
        doubled = TERMINAL_TASK_READ_PREFIX + "任务结果未变化，已停止本次处理。"
        guard = LoopGuard(identical_tool_threshold=1)
        page = delegation_failure_result_page(result_ref=ref, task_id=task_id, status="failed",
            metadata={"error_code": "identical_terminal_task_read"}, result_summary=doubled)
        assert page["result"] == "任务结果未变化，已停止本次处理。"
        decision = guard.observe_tool_result("read_runtime_result", {"result_ref": ref},
            json.dumps(page, ensure_ascii=False), machine_outcome="succeeded", side_effect_free=True)
        assert decision is not None and decision.message == "任务结果未变化，已停止本次处理。"


class TestDenialBoundStaysClosed:
    """N3/N4: the legacy branch keeps its pre-R16b acceptance set; the page
    predicate (not a duplicated dict equality) carries the binding checks."""

    @staticmethod
    def _bound(body: dict, ref: str):
        from app.services.runtime_result_sources import runtime_result_denial_bound
        from app.services.session_model_inputs import _hash

        content = json.dumps(body, ensure_ascii=False)
        receipt = f"tool-frame:{_hash(content)}"
        invocation = SimpleNamespace(tool_name="read_runtime_result", effect_state="effect_committed",
            receipt_ref=receipt, effective_arguments_json={"result_ref": ref}, provider_arguments_json={})
        payload = {"outcome": "success", "content_hash": _hash(content), "content_or_error_ref": receipt}
        return runtime_result_denial_bound(invocation=invocation, payload=payload, content=content)

    def test_pre_r16b_records_still_bind(self):
        ref, task_id = _ref(), str(uuid4())
        legacy = {"schema": "hive.runtime_result_page.v1", "result_ref": ref, "task_id": task_id,
            "ok": True, "status": "failed", "reason_code": "task_failed",
            "reason": "这次执行失败，没有完成。", "retryable": False}
        assert self._bound(legacy, ref)
        assert self._bound({**legacy, "reason_code": "tool_round_limit",
            "reason": "这次已达到运行轮次上限，没有完成。可以重试继续。", "retryable": True}, ref)

    def test_the_legacy_branch_does_not_newly_accept_delegation_timeout(self):
        # N3: no pre-R16b record carries the new code; the legacy acceptance
        # set must not grow with it.
        ref, task_id = _ref(), str(uuid4())
        legacy_timeout = {"schema": "hive.runtime_result_page.v1", "result_ref": ref, "task_id": task_id,
            "ok": True, "status": "failed", "reason_code": "delegation_timeout",
            "reason": "没在时限内完成这项任务。", "retryable": False}
        assert not self._bound(legacy_timeout, ref)

    def test_non_canonical_task_ids_never_bind(self):
        # N4: the canonical-uuid requirement lives in the shared predicate.
        ref, task_id = _ref(), str(uuid4())
        from app.services.runtime_display_status import DELEGATION_RESULT_READ_NOTE

        for task_id_value in (task_id.upper(), "PRIVATE"):
            page = {"schema": "hive.runtime_result_page.v1", "result_ref": ref, "task_id": task_id_value,
                "result": "这次执行失败，没有完成。", "note": DELEGATION_RESULT_READ_NOTE}
            assert not self._bound(page, ref)


class TestNonTimeoutDelegationFailureMemberLine:
    """Grok nit 8: the member line carries a fixed sentence, not an English
    exception type, trace id, or depth; the raw detail stays in server logs."""

    @pytest.mark.asyncio
    async def test_exception_failure_content_is_the_fixed_plain_sentence(self, monkeypatch, caplog):
        import logging

        from app.agents.orchestrator import (
            AgentDelegationRequest,
            OrchestrationPolicy,
            _delegate,
            _delegation_runtime_action_message,
        )
        from tests.agents.test_r16b_delegation_material_and_failure_text import _a2a_authority_kwargs

        target = SimpleNamespace(id=uuid4(), name="[E2E] 周报助手", role_description="Helpful")
        owner_id = uuid4()

        async def explode(_request):
            raise RuntimeError("disk exploded")

        monkeypatch.setattr("app.agents.orchestrator.invoke_agent", explode)
        with caplog.at_level(logging.ERROR, logger="app.agents.orchestrator"):
            result = await _delegate(AgentDelegationRequest(
                target=target,
                target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
                conversation_messages=[{"role": "user", "content": "写三条周报要点"}],
                owner_id=owner_id,
                session_id="child-session",
                trace_id="trace-r16b2",
                depth=1,
                policy=OrchestrationPolicy(timeout_seconds=600),
                **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id="child-session"),
            ))

        assert result.failed is True and result.timed_out is False
        assert result.content == "这次执行失败，没有完成。"
        for marker in ("RuntimeError", "disk exploded", "Trace", "depth", "⚠️"):
            assert marker not in result.content
        member_line = _delegation_runtime_action_message(
            target_name=target.name, status="failed", summary=result.content
        )
        assert member_line == "[E2E] 周报助手 的委派任务失败：这次执行失败，没有完成。"
        # The raw exception detail stays server-side.
        assert "disk exploded" in caplog.text

    @pytest.mark.asyncio
    async def test_timeout_sentence_is_the_single_frozen_constant(self, monkeypatch):
        from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate
        from app.services.runtime_display_status import DELEGATION_TIMEOUT_MESSAGE, _FAILURE_MESSAGES
        from tests.agents.test_r16b_delegation_material_and_failure_text import _a2a_authority_kwargs

        assert _FAILURE_MESSAGES["delegation_timeout"] == DELEGATION_TIMEOUT_MESSAGE == "没在时限内完成这项任务。"
        target = SimpleNamespace(id=uuid4(), name="周报助手", role_description="Helpful")
        owner_id = uuid4()

        async def hang(_request):
            await asyncio.sleep(0.05)
            return SimpleNamespace(content="late reply")

        monkeypatch.setattr("app.agents.orchestrator.invoke_agent", hang)
        result = await _delegate(AgentDelegationRequest(
            target=target,
            target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
            conversation_messages=[{"role": "user", "content": "写三条周报要点"}],
            owner_id=owner_id,
            session_id="child-session",
            trace_id="trace-r16b2",
            depth=1,
            policy=OrchestrationPolicy(timeout_seconds=0.01),
            **_a2a_authority_kwargs(target=target, owner_id=owner_id, session_id="child-session"),
        ))
        assert result.content == DELEGATION_TIMEOUT_MESSAGE


class TestEnvironmentReminderIsTheRuleItself:
    """N6: § Environment restates MEMBER_LANGUAGE_RULE verbatim, not a paraphrase."""

    def test_environment_line_carries_the_member_language_rule(self):
        from app.runtime.prompt_sections.environment import build_environment_section
        from app.runtime.prompt_sections.tone_style import MEMBER_LANGUAGE_RULE

        section = build_environment_section(user_name="test123", channel="web", agent_name="我的助理")
        line = next(part for part in section.splitlines() if part.startswith("- Language:"))
        assert MEMBER_LANGUAGE_RULE in line
        assert "English tool output does not change it" in line


class TestBriefNamesPathsOnlyForDeliverables:
    """N7: reading material rides the message; paths name files the colleague
    must deliver, so the two instructions no longer sit side by side."""

    def test_brief_contract_separates_reading_material_from_delivery_paths(self):
        from app.runtime.prompts.delegation import DELEGATION_BRIEF_CONTRACT

        assert "into the message itself" in DELEGATION_BRIEF_CONTRACT
        assert "do not point at your own paths as its sources" in DELEGATION_BRIEF_CONTRACT
        assert "must update or deliver" in DELEGATION_BRIEF_CONTRACT
        assert "name every target path the worker must write or deliver" in DELEGATION_BRIEF_CONTRACT
        assert "ask the worker to update the same path" not in DELEGATION_BRIEF_CONTRACT

    def test_target_artifacts_description_marks_paths_as_delivery_targets(self):
        from app.tools.collector import collect_tools

        schema = next(tool["function"] for tool in collect_tools().openai_tools
            if tool["function"]["name"] == "delegate_to_agent")
        description = schema["parameters"]["properties"]["target_artifacts"]["description"]
        assert "must update or deliver" in description
        assert "not sources to read" in description
