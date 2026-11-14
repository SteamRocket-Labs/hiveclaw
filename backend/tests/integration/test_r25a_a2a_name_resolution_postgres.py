"""Name routing through real messaging/delegation tools and PostgreSQL policy.

Other owners' assistants are inserted before the source owner's assistant, as
in run20. Only the consulted model response and background worker wake are
stubbed; name lookup, policy, pair sessions, and durable delegation are real.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.core.execution_context import (
    ExecutionPrincipal,
    clear_execution_identity,
    get_execution_identity,
    set_delegated_user_identity,
    set_execution_identity,
)
from app.database import tenant_scoped_session
from app.models.agent import Agent, AgentPermission
from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_tool_domains import messaging
from app.services.secrets_provider import get_secrets_provider
from app.services.tool_visibility import HR_AGENT_CLASS, HR_AGENT_NAME
from app.tools.handlers import communication
from app.tools import validation

TOOLS = ("send_message_to_agent", "delegate_to_agent")
DENIAL = (
    "Cross-owner A2A requires an active A2A Collaboration Group with both agents approved. "
    "Create or approve a group membership before retrying."
)


@pytest.fixture
async def directory(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    tenant_id, owner_id, other_id, third_id = (uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="R25a", slug=f"r25a-{tenant_id.hex}"))
        await db.flush()
        for user_id in (owner_id, other_id, third_id):
            db.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=f"r25a-{user_id.hex}",
                    email=f"{user_id.hex}@fixture.test",
                    password_hash="fixture",
                    display_name="R25a member",
                    role="member",
                )
            )
        model = LLMModel(
            tenant_id=tenant_id,
            provider="openai",
            model="fixture-only",
            label="R25a",
            api_key_encrypted=get_secrets_provider().encrypt("synthetic-unused-no-provider-call"),
        )
        db.add(model)
        await db.flush()
        source = Agent(
            tenant_id=tenant_id,
            creator_id=owner_id,
            owner_user_id=owner_id,
            name="Hired colleague",
            status="idle",
            primary_model_id=model.id,
        )
        db.add(source)
        await db.flush()
        root = ChatSession(tenant_id=tenant_id, agent_id=source.id, user_id=owner_id)
        db.add(root)
        await db.commit()

    state = SimpleNamespace(
        tenant_id=tenant_id,
        owner_id=owner_id,
        other_id=other_id,
        third_id=third_id,
        source=source,
        root=root,
        model=model,
        runtime_calls=[],
        policy_calls=[],
        owner=owner_sessionmaker,
    )

    def scoped(tid, **kwargs):
        return tenant_scoped_session(tid, session_factory=app_user_sessionmaker, **kwargs)

    native_policy = messaging.resolve_a2a_collaboration_policy

    async def record_policy(db, source, target, *, action):
        state.policy_calls.append((target.id, action))
        return await native_policy(db, source, target, action=action)

    async def respond(**kwargs):
        state.runtime_calls.append(kwargs["target"].id)
        return "R25a real target selected; synthetic model reply."

    async def no_worker(**_kwargs):
        return None

    monkeypatch.setattr(messaging, "tenant_scoped_session", scoped)
    monkeypatch.setattr(messaging, "resolve_a2a_collaboration_policy", record_policy)
    monkeypatch.setattr(messaging, "_invoke_agent_message_runtime", respond)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_worker)
    async with scoped(tenant_id) as db:
        assert (
            await db.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        ).scalar() is False
    return state


async def _targets(state, *specs):
    agents = []
    async with state.owner() as db:
        for index, (name, owner_id) in enumerate(specs):
            agent = Agent(
                tenant_id=state.tenant_id,
                creator_id=owner_id,
                owner_user_id=owner_id,
                name=name,
                status="idle",
                primary_model_id=state.model.id,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index),
            )
            db.add(agent)
            # Flush each row so heap order reproduces the pre-fix arbitrary pick.
            await db.flush()
            agents.append(agent)
        await db.commit()
    return agents


async def _call(state, tool, name=None, **extra):
    principal = ExecutionPrincipal(
        tenant_id=state.tenant_id,
        source_agent_id=state.source.id,
        requester_user_id=state.owner_id,
        root_session_id=str(state.root.id),
        delegation_chain=(f"agent:{state.source.id}",),
    )
    arguments = {
        "message": "Return the requested analysis.",
        "_execution_principal": principal.to_evidence(),
        **extra,
    }
    if name is not None:
        arguments["agent_name"] = name
    validation._tool_parameter_schemas.cache_clear()
    errors = validation.validate_tool_arguments(tool, arguments)
    assert errors == [], errors
    previous_identity = get_execution_identity()
    set_delegated_user_identity(state.owner_id, "R25a member", "web")
    try:
        raw = await getattr(communication, tool)(state.source.id, arguments)
    finally:
        if previous_identity is None:
            clear_execution_identity()
        else:
            set_execution_identity(previous_identity)
    if "<tool_error>" in raw:
        return json.loads(raw.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    return json.loads(raw)


async def _assert_delivered(state, tool, result, target):
    assert result["ok"] is True, result
    assert result["target_agent_id"] == str(target.id), result
    # R25a S2: assert the enforced/delivered target, not candidate evaluation order.
    action = "message" if tool == "send_message_to_agent" else "delegate"
    assert state.policy_calls[-1] == (target.id, action)
    async with state.owner() as db:
        session = await db.get(ChatSession, UUID(result["child_session_id"]))
        sessions = list(await db.scalars(select(ChatSession.id).where(ChatSession.user_id == state.owner_id)))
        assert set(sessions) == {state.root.id, session.id}
        tasks = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == state.tenant_id)))
        assert {task.child_agent_id for task in tasks} <= {target.id}
        messages = list(await db.scalars(select(ChatMessage).where(ChatMessage.tenant_id == state.tenant_id)))
        assert {message.agent_id for message in messages} <= {state.source.id, target.id}
        assert session is not None
        assert session.user_id == state.owner_id
        if tool == "send_message_to_agent":
            assert {session.agent_id, session.peer_agent_id} == {state.source.id, target.id}
            messages = list(await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == str(session.id))))
            assert len(messages) == 2
            assert state.runtime_calls == [target.id]
            assert tasks == []
        else:
            assert len(tasks) == 1
            assert state.runtime_calls == []
            task = await db.get(RuntimeTask, UUID(result["task_id"]))
            assert task.status == "pending"
            assert task.parent_agent_id == state.source.id and task.child_agent_id == target.id
            assert session.agent_id == target.id and session.peer_agent_id == state.source.id
            assert session.runtime_task_id == task.id


async def _assert_no_delivery(state):
    assert state.runtime_calls == []
    async with state.owner() as db:
        assert list(await db.scalars(select(ChatMessage.id).where(ChatMessage.tenant_id == state.tenant_id))) == []
        assert list(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == state.tenant_id))) == []
        assert list(await db.scalars(select(ChatSession.id).where(ChatSession.user_id == state.owner_id))) == [
            state.root.id
        ]


@pytest.mark.parametrize("tool", TOOLS)
async def test_name_reaches_same_owner_not_older_private_or_public_assistants(directory, tool):
    state = directory
    _, public, own = await _targets(
        state, ("My Assistant", state.other_id), ("My Assistant", state.third_id), ("My Assistant", state.owner_id)
    )
    async with state.owner() as db:
        db.add(AgentPermission(tenant_id=state.tenant_id, agent_id=public.id, scope_type="company"))
        await db.commit()
    result = await _call(state, tool, "My Assistant")
    await _assert_delivered(state, tool, result, own)


@pytest.mark.parametrize("tool", TOOLS)
async def test_exact_denied_target_does_not_fall_through_to_same_owner_substring_guard(directory, tool):
    state = directory
    await _targets(state, ("My Assistant", state.other_id), ("My Assistant 2", state.owner_id))
    result = await _call(state, tool, "My Assistant")
    assert result["ok"] is False and result["diagnostic_message"] == DENIAL, result
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
async def test_two_same_owner_helpers_are_ambiguous_and_send_nothing(directory, tool):
    state = directory
    first, second = await _targets(state, ("Helper", state.owner_id), ("Helper", state.owner_id))
    result = await _call(state, tool, "Helper")
    assert result["ok"] is False, result
    diagnostic = result["diagnostic_message"]
    # R25a N1/N2: both paths expose one actionable ambiguity sentence/code.
    assert diagnostic.startswith("Multiple agents match 'Helper'; retry with target_agent_id."), result
    assert first.name in diagnostic and str(first.id) in diagnostic and str(second.id) in diagnostic
    outcome_entry = (
        messaging._send_message_to_agent_outcome
        if tool == "send_message_to_agent"
        else messaging._delegate_to_agent_async_outcome
    )
    outcome = await outcome_entry(state.source.id, {"agent_name": "Helper", "message": "No ambiguous send."})
    assert outcome.error_code == "target_agent_ambiguous" and outcome.retryable is False
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
async def test_cross_owner_without_group_keeps_existing_denial_guard(directory, tool):
    state = directory
    await _targets(state, ("My Assistant", state.other_id))
    result = await _call(state, tool, "My Assistant")
    assert result["ok"] is False and result["diagnostic_message"] == DENIAL, result
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("query", "decoy", "literal"),
    [
        ("Rate%", "RateX analyst", "Team Rate% analyst"),
        ("Rate_", "RateX analyst", "Team Rate_ analyst"),
        ("Rate\\", "Team Rate analyst", r"Team Rate\ analyst"),
    ],
)
async def test_name_wildcards_and_escape_match_literally(directory, tool, query, decoy, literal):
    state = directory
    _, target = await _targets(state, (decoy, state.owner_id), (literal, state.owner_id))
    await _assert_delivered(state, tool, await _call(state, tool, query), target)


@pytest.mark.parametrize("tool", TOOLS)
async def test_case_insensitive_exact_beats_older_same_owner_substring(directory, tool):
    state = directory
    _, target = await _targets(state, ("My Assistant 2", state.owner_id), ("MY ASSISTANT", state.owner_id))
    await _assert_delivered(state, tool, await _call(state, tool, "My Assistant"), target)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("permission", ("public", "active_group"))
async def test_only_allowed_cross_owner_candidate_wins_over_private_match(directory, tool, permission):
    state = directory
    _, target = await _targets(state, ("Helper", state.other_id), ("Helper", state.third_id))
    async with state.owner() as db:
        if permission == "public":
            db.add(AgentPermission(tenant_id=state.tenant_id, agent_id=target.id, scope_type="company"))
        else:
            group = AgentCollaborationGroup(
                tenant_id=state.tenant_id,
                name="R25a approved",
                status="active",
                created_by_user_id=state.owner_id,
                created_by_agent_id=state.source.id,
            )
            db.add(group)
            await db.flush()
            for agent in (state.source, target):
                db.add(
                    AgentCollaborationGroupMember(
                        tenant_id=state.tenant_id,
                        group_id=group.id,
                        agent_id=agent.id,
                        agent_owner_user_id=agent.owner_user_id,
                        status="active",
                        approved_by_user_id=agent.owner_user_id,
                    )
                )
        await db.commit()
    await _assert_delivered(state, tool, await _call(state, tool, "Helper"), target)


@pytest.mark.parametrize("tool", TOOLS)
async def test_two_public_candidates_are_ambiguous_without_disclosing_denied_candidate(directory, tool):
    state = directory
    private, first, second = await _targets(
        state, ("Helper", state.other_id), ("Helper", state.third_id), ("Helper", state.third_id)
    )
    async with state.owner() as db:
        for target in (first, second):
            db.add(AgentPermission(tenant_id=state.tenant_id, agent_id=target.id, scope_type="company"))
        await db.commit()
    result = await _call(state, tool, "Helper")
    assert result["ok"] is False, result
    diagnostic = result["diagnostic_message"]
    # R25a N2: wording is shared with the capped and delegated ambiguity paths.
    assert diagnostic.startswith("Multiple agents match 'Helper'; retry with target_agent_id."), result
    assert str(first.id) in diagnostic and str(second.id) in diagnostic and str(private.id) not in diagnostic
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
async def test_same_owner_is_reached_among_twenty_matches(directory, tool):
    state = directory
    targets = await _targets(state, *(("Helper", state.other_id) for _ in range(19)), ("Helper", state.owner_id))
    await _assert_delivered(state, tool, await _call(state, tool, "Helper"), targets[-1])


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("tier", ("exact", "substring"))
async def test_over_twenty_in_winning_tier_requires_id_without_partial_ranking(directory, tool, tier):
    state = directory
    names = ["Helper" if tier == "exact" else f"Helper {index}" for index in range(21)]
    await _targets(state, *((name, state.owner_id) for name in names))
    result = await _call(state, tool, "Helper")
    assert result["ok"] is False, result
    # R25a S2/N2: retain whole-group refusal/no delivery, not internal scan order or terminology.
    assert result["diagnostic_message"] == "Multiple agents match 'Helper'; retry with target_agent_id.", result
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
async def test_explicit_id_is_not_replaced_by_same_owner_name_guard(directory, tool):
    state = directory
    private, _ = await _targets(state, ("My Assistant", state.other_id), ("My Assistant", state.owner_id))
    result = await _call(state, tool, "My Assistant", target_agent_id=str(private.id))
    assert result["ok"] is False and result["diagnostic_message"] == DENIAL, result
    action = "message" if tool == "send_message_to_agent" else "delegate"
    assert state.policy_calls[-1] == (private.id, action)
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
async def test_many_lower_tier_matches_do_not_hide_single_exact_match_guard(directory, tool):
    state = directory
    target, *_ = await _targets(
        state, ("Helper", state.owner_id), *((f"Helper {index}", state.owner_id) for index in range(21))
    )
    await _assert_delivered(state, tool, await _call(state, tool, "Helper"), target)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("legacy_owner", ("neither", "source", "target", "both"))
async def test_twenty_other_members_cannot_push_own_assistant_past_cap(directory, tool, legacy_owner):
    state = directory
    other_owners = [state.other_id, state.third_id, *(uuid4() for _ in range(18))]
    async with state.owner() as db:
        for user_id in other_owners[2:]:
            db.add(
                User(
                    id=user_id,
                    tenant_id=state.tenant_id,
                    username=f"r25a-{user_id.hex}",
                    email=f"{user_id.hex}@fixture.test",
                    password_hash="fixture",
                    display_name="Other member",
                    role="member",
                )
            )
        await db.commit()
    *_, own = await _targets(
        state, *(("My Assistant", user_id) for user_id in other_owners), ("My Assistant", state.owner_id)
    )
    async with state.owner() as db:
        if legacy_owner in {"source", "both"}:
            (await db.get(Agent, state.source.id)).owner_user_id = None
        if legacy_owner in {"target", "both"}:
            (await db.get(Agent, own.id)).owner_user_id = None
        await db.commit()
    await _assert_delivered(state, tool, await _call(state, tool, "My Assistant"), own)


async def _make_unavailable(state, agent, kind):
    async with state.owner() as db:
        row = await db.get(Agent, agent.id)
        row.status = "stopped"
        if kind == "deactivated":
            # Mirrors retirement's committed deactivated_at + stopped state.
            row.deactivated_at = datetime.now(timezone.utc)
        await db.commit()


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("kind", ("deactivated", "stopped"))
@pytest.mark.parametrize("public", (True, False), ids=("grok_input_a", "grok_input_b"))
async def test_unavailable_own_assistant_never_redirects_or_reports_another_owners_denial(
    directory, tool, kind, public
):
    state = directory
    other, own = await _targets(state, ("My Assistant", state.other_id), ("My Assistant", state.owner_id))
    await _make_unavailable(state, own, kind)
    if public:
        async with state.owner() as db:
            db.add(AgentPermission(tenant_id=state.tenant_id, agent_id=other.id, scope_type="company"))
            await db.commit()
    result = await _call(state, tool, "My Assistant")
    assert result["ok"] is False, result
    diagnostic = result["diagnostic_message"]
    assert ("已停用" if kind == "deactivated" else "stopped") in diagnostic, result
    assert "Cross-owner" not in diagnostic, result
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("kind", ("deactivated", "stopped"))
async def test_active_own_assistant_wins_over_own_retired_namesake(directory, tool, kind):
    state = directory
    retired, active = await _targets(state, ("My Assistant", state.owner_id), ("My Assistant", state.owner_id))
    await _make_unavailable(state, retired, kind)
    await _assert_delivered(state, tool, await _call(state, tool, "My Assistant"), active)


@pytest.mark.parametrize("tool", TOOLS)
async def test_id_only_call_passes_real_schema_and_reaches_target(directory, tool):
    state = directory
    (target,) = await _targets(state, ("My Assistant", state.owner_id))
    await _assert_delivered(state, tool, await _call(state, tool, target_agent_id=str(target.id)), target)


@pytest.mark.parametrize("tool", TOOLS)
async def test_not_found_guides_to_collaborators_without_leaking_tenant_names(directory, tool):
    state = directory
    await _targets(state, ("PRIVATE_OTHER_MEMBER_NAME", state.other_id), ("Own colleague", state.owner_id))
    result = await _call(state, tool, "missing-colleague")
    assert result["ok"] is False, result
    diagnostic = result["diagnostic_message"]
    assert "A2A Collaborators" in diagnostic and "name or id" in diagnostic, result
    assert "Available:" not in diagnostic and "PRIVATE_OTHER_MEMBER_NAME" not in diagnostic, result
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
async def test_handler_still_requires_a_name_or_id_after_schema_validation(directory, tool):
    state = directory
    result = await _call(state, tool)
    assert result["ok"] is False and "Please provide target agent" in result["diagnostic_message"], result
    # R25a N-c: preserve the prefix while explicitly explaining the id alternative.
    assert result["diagnostic_message"] == (
        "Please provide target agent name or target_agent_id and message content"
    ), result
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
async def test_unreachable_private_substring_discloses_neither_identity_nor_lifecycle(directory, tool, caplog):
    state = directory
    (private,) = await _targets(state, ("Finance Secret M&A Analyst", state.other_id))
    await _make_unavailable(state, private, "deactivated")
    caplog.clear()
    result = await _call(state, tool, "Finance")
    assert result["ok"] is False, result
    reply = json.dumps(result, ensure_ascii=False)
    assert private.name not in reply and str(private.id) not in reply, result
    assert result["diagnostic_message"] == (
        "No agent found matching 'Finance'. Pick a name or id from your A2A Collaborators list."
    ), result
    await _assert_no_delivery(state)
    assert private.name not in caplog.text and str(private.id) not in caplog.text


async def test_error_state_name_cannot_masquerade_as_delegate_ambiguity(directory):
    state = directory
    display_name = "Multiple agents match 'x'; retry with target_agent_id."
    (target,) = await _targets(state, (display_name, state.owner_id))
    async with state.owner() as db:
        (await db.get(Agent, target.id)).status = "error"
        await db.commit()
    result = await _call(state, "delegate_to_agent", "x")
    assert result["ok"] is False and "is currently error" in result["diagnostic_message"], result
    outcome = await messaging._delegate_to_agent_async_outcome(
        state.source.id, {"agent_name": "x", "message": "Do not send to an unavailable employee."}
    )
    assert outcome.error_code == "target_runtime_unavailable" and outcome.retryable is False
    await _assert_no_delivery(state)


@pytest.mark.parametrize("tool", TOOLS)
async def test_lazily_owned_system_hr_does_not_shadow_public_hr_partner(directory, tool):
    state = directory
    hr, partner = await _targets(state, (HR_AGENT_NAME, state.owner_id), ("HR Partner", state.other_id))
    async with state.owner() as db:
        (await db.get(Agent, hr.id)).agent_class = HR_AGENT_CLASS
        db.add(AgentPermission(tenant_id=state.tenant_id, agent_id=partner.id, scope_type="company"))
        await db.commit()
    await _assert_delivered(state, tool, await _call(state, tool, "hr"), partner)
