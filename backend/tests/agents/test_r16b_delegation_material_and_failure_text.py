"""R16b: the colleague gets the material in the message; failure text stays plain.

E2E run 12: the parent delegated with a pointer to ``uploads/sales-run*.csv`` in
its own workspace, the colleague burned 12 rounds searching for a file it can
never read, and the 600 s timeout line (seconds + name twice) plus the
``task_failed``/retryable codes reached the member through the parent's reply.
These tests pin the model-facing guidance and failure text, not just constants.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _a2a_authority_kwargs(
    *,
    target,
    owner_id,
    session_id: str,
    parent_agent_id=None,
    parent_session_id: str | None = None,
) -> dict:
    from app.core.execution_context import ExecutionPrincipal

    tenant_id = getattr(target, "tenant_id", None) or uuid4()
    target.tenant_id = tenant_id
    source_agent_id = parent_agent_id or uuid4()
    root_session_id = parent_session_id or session_id
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent_id,
        requester_user_id=owner_id,
        root_session_id=root_session_id,
        delegation_chain=(f"agent:{source_agent_id}",),
    )
    return {
        "parent_agent_id": source_agent_id,
        "parent_session_id": root_session_id,
        "execution_principal": principal.to_evidence(),
    }


@pytest.fixture(autouse=True)
def _stub_activity_logger(monkeypatch):
    async def fake_log_activity(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    async def fake_delegation_plan_gate_allows(_request):
        return True, None

    monkeypatch.setattr("app.agents.orchestrator._delegation_plan_gate_allows", fake_delegation_plan_gate_allows)


class TestDelegationToolGuidanceInlinesTheMaterial:
    """1a: the delegating model is told to carry the facts in the message."""

    def _delegate_schema(self) -> dict:
        from app.tools.collector import collect_tools

        collected = collect_tools()
        for tool in collected.openai_tools:
            if tool["function"]["name"] == "delegate_to_agent":
                return tool["function"]
        raise AssertionError("delegate_to_agent schema missing from the tool surface")

    def test_description_tells_parent_to_put_facts_into_the_message(self):
        description = self._delegate_schema()["description"]
        assert "into the message itself" in description
        assert "do not point at your own paths as its sources" in description

    def test_brief_contract_reaches_the_worker_facing_tool_text(self):
        from app.runtime.prompts.delegation import DELEGATION_BRIEF_CONTRACT

        assert "facts, figures, and short excerpts" in DELEGATION_BRIEF_CONTRACT
        # The contract is embedded verbatim in the schema the parent model sees.
        assert DELEGATION_BRIEF_CONTRACT in self._delegate_schema()["description"]


class TestWorkerTreatsMessageFactsAsSource:
    """1b: the colleague answers from the brief instead of hunting for files."""

    def test_isolation_contract_names_message_facts_as_the_source(self):
        from app.agents.orchestrator import _DELEGATION_TOOL_PROFILES, _build_delegated_worker_prompt

        prompt = _build_delegated_worker_prompt(_DELEGATION_TOOL_PROFILES["worker_safe"])
        assert "are your source when you cannot read the original" in prompt
        assert "say which points rest on the message" in prompt
        assert "Do not spend rounds searching for files outside your reach" in prompt

    def test_peer_profile_carries_the_same_guidance(self):
        from app.agents.orchestrator import _DELEGATION_TOOL_PROFILES, _build_delegated_worker_prompt

        prompt = _build_delegated_worker_prompt(_DELEGATION_TOOL_PROFILES["agent_message"], parent_name="Atlas")
        assert "are your source when you cannot read the original" in prompt


class TestTimeoutFailureText:
    """3a: the timeout line carries no seconds and no repeated name."""

    @pytest.mark.asyncio
    async def test_timeout_content_is_plain_and_the_member_line_names_the_colleague_once(self, monkeypatch):
        from app.agents.orchestrator import (
            AgentDelegationRequest,
            OrchestrationPolicy,
            _delegate,
            _delegation_runtime_action_message,
        )

        target = SimpleNamespace(id=uuid4(), name="[E2E] 周报助手", role_description="Helpful")
        owner_id = uuid4()
        parent_agent_id = uuid4()

        async def fake_invoke_agent(_request):
            await asyncio.sleep(0.05)
            return SimpleNamespace(content="late reply")

        monkeypatch.setattr("app.agents.orchestrator.invoke_agent", fake_invoke_agent)

        result = await _delegate(
            AgentDelegationRequest(
                target=target,
                target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
                conversation_messages=[{"role": "user", "content": "写三条周报要点"}],
                owner_id=owner_id,
                session_id="child-session",
                trace_id="trace-r16b",
                depth=1,
                policy=OrchestrationPolicy(timeout_seconds=0.01),
                **_a2a_authority_kwargs(
                    target=target,
                    owner_id=owner_id,
                    session_id="child-session",
                    parent_agent_id=parent_agent_id,
                    parent_session_id="parent-session",
                ),
            )
        )

        assert result.timed_out is True and result.failed is True
        content = result.content
        assert "秒" not in content
        assert "结果未知" not in content
        assert not any(character.isdigit() for character in content)
        assert "[E2E] 周报助手" not in content

        member_line = _delegation_runtime_action_message(
            target_name=target.name, status="failed", summary=content
        )
        assert member_line.count(target.name) == 1, member_line
        assert "秒" not in member_line and "结果未知" not in member_line


class TestDelegationFailureResultPage:
    """3b: the result page the parent model reads carries plain sentences only."""

    def test_timed_out_delegation_maps_to_the_timeout_sentence(self):
        from app.services.runtime_display_status import runtime_failure_observation

        observation = runtime_failure_observation("failed", {"timed_out": True})
        assert observation is not None
        assert observation["reason"] == "没在时限内完成这项任务。"
        # Round-trip recognition (loop guard / denial bound) must stay consistent.
        from app.services.runtime_display_status import is_runtime_failure_observation

        assert is_runtime_failure_observation(observation)

    def test_page_derived_from_the_same_fields_has_no_internal_codes(self):
        from app.services.runtime_display_status import delegation_failure_result_page

        page = delegation_failure_result_page(
            result_ref=f"runtime-result://{uuid4()}/{'a' * 64}",
            task_id=str(uuid4()),
            status="failed",
            metadata={"timed_out": True},
        )
        assert page is not None
        assert page["result"] == "没在时限内完成这项任务。"
        serialized = json.dumps(page, ensure_ascii=False)
        for code in ("task_failed", "reason_code", "retryable", '"status"'):
            assert code not in serialized

    def test_denial_bound_accepts_only_the_fixed_plain_page(self):
        from app.services.runtime_display_status import delegation_failure_result_page
        from app.services.runtime_result_sources import runtime_result_denial_bound
        from app.services.session_model_inputs import _hash

        ref = f"runtime-result://{uuid4()}/{'b' * 64}"
        page = delegation_failure_result_page(
            result_ref=ref, task_id=str(uuid4()), status="failed", metadata={"error_code": "tool_budget"}
        )
        content = json.dumps(page)
        receipt = f"tool-frame:{_hash(content)}"
        invocation = SimpleNamespace(
            tool_name="read_runtime_result",
            effect_state="effect_committed",
            receipt_ref=receipt,
            effective_arguments_json={"result_ref": ref},
            provider_arguments_json={},
        )
        payload = {"outcome": "success", "content_hash": _hash(content), "content_or_error_ref": receipt}
        assert runtime_result_denial_bound(invocation=invocation, payload=payload, content=content)

        forged = {**page, "result": "DENIED_TARGET_BYTES"}
        forged_content = json.dumps(forged)
        forged_payload = {
            "outcome": "success",
            "content_hash": _hash(forged_content),
            "content_or_error_ref": f"tool-frame:{_hash(forged_content)}",
        }
        assert not runtime_result_denial_bound(
            invocation=SimpleNamespace(**{**invocation.__dict__, "receipt_ref": f"tool-frame:{_hash(forged_content)}"}),
            payload=forged_payload,
            content=forged_content,
        )
