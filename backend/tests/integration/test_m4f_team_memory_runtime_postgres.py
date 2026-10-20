"""Team memory reaches actual employee work with current source authority."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.organization_identity import BusinessTeamMembership, ContentInputReceipt
from tests.integration.test_m4e_team_memory_authority_postgres import runtime_team  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_memory  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def readable_team_memory(runtime_team, owner_sessionmaker, app_user_sessionmaker):  # noqa: F811
    s, workspace, (writer, reader) = runtime_team
    n = s.n
    async with owner_sessionmaker() as db:
        db.add(BusinessTeamMembership(tenant_id=n.tenant, business_team_id=UUID(workspace.split(":", 1)[1]),
            user_id=n.foreign, role="member", status="active"))
        await db.commit()
    saved = await writer.put("/enterprise/memory/shared", json={"workspace_key": workspace, "key": "report",
        "title": "Team release", "content": "Our release cadence is TEAM_PROJECT_RELEASE_MARKER", "mode": "replace"})
    assert saved.status_code == 200, saved.text
    available = await reader.get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert available.status_code == 200 and "TEAM_PROJECT_RELEASE_MARKER" in available.text
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        receipt = await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == n.tenant,
            ContentInputReceipt.carrier_kind == "team_memory", ContentInputReceipt.carrier_ref == f"{workspace}\nreport"))
        assert receipt is not None
    return s, workspace, receipt


@pytest.mark.parametrize("surface", ["search", "load", "automatic"])
@pytest.mark.parametrize("damaged_peer", [False, True])
async def test_current_member_can_consume_team_memory_in_actual_work(
    readable_team_memory, runtime_team, monkeypatch, surface, damaged_peer,  # noqa: F811
):
    from pathlib import Path
    from app.memory.activation import ActivationContext
    from app.memory.retriever import MemoryRetriever
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack
    from app.services.team_memory import TeamMemoryStore

    s, workspace, receipt = readable_team_memory
    n = s.n
    if damaged_peer:
        saved = await runtime_team[2][0].put("/enterprise/memory/shared", json={"workspace_key": workspace, "key": "blocked",
            "title": "Corrupt release peer", "content": "Originally verified peer", "mode": "replace"})
        assert saved.status_code == 200, saved.text
        entry = TeamMemoryStore().get_entry(str(n.tenant), workspace, "blocked")
        path = Path(entry.absolute_path)
        path.write_text(path.read_text().replace("Originally verified peer", "UNRECEIPTED_TEAM_PEER_MARKER"))
    if surface in {"search", "load"}:
        name = "search_memory" if surface == "search" else "load_memory"
        args = {"query": "release", "scope": "facts"} if surface == "search" else {"ids": [f"team_memory:{workspace}:report"]}
        if surface == "load" and damaged_peer:
            args["ids"].insert(0, f"team_memory:{workspace}:blocked")
        result = str(await n.service.execute(name, args, agent_id=n.agent, user_id=n.foreign,
            session_id=str(s.second_session), runtime_task_id=str(s.second_run), emit_runtime_hooks=False))
    else:
        async def choose(_self, *, items, **_kwargs):
            return [item.metadata["selection_candidate_id"] for item in items], "Use the current team's release fact"

        monkeypatch.setattr(MemoryRetriever, "_select_with_model", choose)
        activation = ActivationContext(query="release", principal_stack=PrincipalStack(
            current_user=Principal(PrincipalRole.CURRENT_USER, str(n.foreign)),
            direct_owner=Principal(PrincipalRole.OWNER, str(n.user))))
        items = await MemoryRetriever(data_root=n.root.parent).retrieve(n.agent, "release", str(s.second_session),
            str(n.tenant), runtime_task_id=str(s.second_run), activation_context=activation,
            rerank_model_config={"provider": "reference", "model": "local-reference"})
        result = "\n".join(item.content for item in items)
        if "TEAM_PROJECT_RELEASE_MARKER" in result:
            assert any({"source_kind": "team_memory", "source_id": str(receipt.id),
                "source_version": receipt.content_sha256} in item.source_refs for item in items)
    assert "TEAM_PROJECT_RELEASE_MARKER" in result, result
    assert "ORCHID_PRIVATE_MARKER" not in result, result
    assert "UNRECEIPTED_TEAM_PEER_MARKER" not in result, result


async def leave_team(owner_sessionmaker, workspace, user_id):
    async with owner_sessionmaker() as db:
        member = await db.scalar(select(BusinessTeamMembership).where(
            BusinessTeamMembership.business_team_id == UUID(workspace.split(":", 1)[1]),
            BusinessTeamMembership.user_id == user_id, BusinessTeamMembership.status == "active"))
        member.status = "left"
        await db.commit()


@pytest.mark.parametrize("surface", ["search", "load", "automatic"])
async def test_member_withdrawal_and_browser_admin_inspection_do_not_grant_runtime_reads(
    readable_team_memory, runtime_team, owner_sessionmaker, monkeypatch, surface,  # noqa: F811
):
    from app.models.user import User

    s, workspace, _receipt = readable_team_memory
    n = s.n
    await leave_team(owner_sessionmaker, workspace, n.foreign)
    async with owner_sessionmaker() as db:
        (await db.get(User, n.foreign)).role = "org_admin"
        await db.commit()
    browser = runtime_team[2][1]
    inspected = await browser.get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert inspected.status_code == 200 and "TEAM_PROJECT_RELEASE_MARKER" in inspected.text
    if surface == "automatic":
        from app.memory.activation import ActivationContext
        from app.memory.retriever import MemoryRetriever
        from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

        observed = []

        async def choose(_self, *, items, **_kwargs):
            observed.extend(item.content for item in items)
            return [item.metadata["selection_candidate_id"] for item in items], "Use authorized facts only"

        monkeypatch.setattr(MemoryRetriever, "_select_with_model", choose)
        context = ActivationContext(query="release", principal_stack=PrincipalStack(
            current_user=Principal(PrincipalRole.CURRENT_USER, str(n.foreign)),
            direct_owner=Principal(PrincipalRole.OWNER, str(n.user))))
        items = await MemoryRetriever(data_root=n.root.parent).retrieve(n.agent, "release", str(s.second_session), str(n.tenant),
            runtime_task_id=str(s.second_run), activation_context=context, rerank_model_config={"provider": "reference", "model": "local"})
        result = "\n".join(item.content for item in items)
        assert all("TEAM_PROJECT_RELEASE_MARKER" not in value for value in observed)
    else:
        tool = "search_memory" if surface == "search" else "load_memory"
        args = {"query": "release", "scope": "facts"} if surface == "search" else {"ids": [f"team_memory:{workspace}:report"]}
        result = str(await n.service.execute(tool, args, agent_id=n.agent, user_id=n.foreign,
            session_id=str(s.second_session), runtime_task_id=str(s.second_run), emit_runtime_hooks=False))
    assert "TEAM_PROJECT_RELEASE_MARKER" not in result and "ORCHID_PRIVATE_MARKER" not in result, result
    still_inspected = await browser.get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert still_inspected.status_code == 200


async def test_membership_withdrawn_while_selector_runs_withholds_selected_body(
    readable_team_memory, owner_sessionmaker, monkeypatch,
):
    from app.memory.activation import ActivationContext
    from app.memory.retriever import MemoryRetriever
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    s, workspace, _receipt = readable_team_memory
    n = s.n
    observed = []

    async def choose(_self, *, items, **_kwargs):
        observed.extend(item.content for item in items)
        await leave_team(owner_sessionmaker, workspace, n.foreign)
        return [item.metadata["selection_candidate_id"] for item in items], "The source was available before selection"

    monkeypatch.setattr(MemoryRetriever, "_select_with_model", choose)
    context = ActivationContext(query="release", principal_stack=PrincipalStack(
        current_user=Principal(PrincipalRole.CURRENT_USER, str(n.foreign)),
        direct_owner=Principal(PrincipalRole.OWNER, str(n.user))))
    items = await MemoryRetriever(data_root=n.root.parent).retrieve(n.agent, "release", str(s.second_session), str(n.tenant),
        runtime_task_id=str(s.second_run), activation_context=context, rerank_model_config={"provider": "reference", "model": "local"})
    assert any("TEAM_PROJECT_RELEASE_MARKER" in value for value in observed)
    assert all("TEAM_PROJECT_RELEASE_MARKER" not in item.content for item in items)


async def test_actual_tool_result_sources_reach_private_derived_memory_and_membership_withdrawal(
    readable_team_memory, app_user_sessionmaker, owner_sessionmaker,
):
    from app.models.invocation_span import InvocationSpan
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, workspace, source = readable_team_memory
    n = s.n
    loaded, invocation = await memory_call(n, app_user_sessionmaker, "load_memory", {"ids": [f"team_memory:{workspace}:report"]},
        user=n.foreign, session=s.second_session, run=s.second_run, command="Read our team release reference")
    assert "TEAM_PROJECT_RELEASE_MARKER" in loaded, loaded
    dependency = {"source_kind": "team_memory", "source_id": str(source.id), "source_version": source.content_sha256}
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        span = await db.get(InvocationSpan, uuid5(invocation.id, "explicit-memory-tool-result"))
        assert dependency in span.metadata_json["memory_result"]["sources"]
    saved, derived_invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "Remember the team's cadence TEAM_DERIVED_MEMORY_MARKER", "category": "project"}, user=n.foreign,
        session=s.second_session, run=s.second_run, include_history=True)
    assert "Saved to explicit memory overlay" in saved, saved
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        derived = await db.get(ContentInputReceipt, uuid5(derived_invocation.id, "explicit-memory-source"))
        assert dependency in derived.dependencies_json
        derived_id = derived.authority_json["entry_id"]
    before = str(await n.service.execute("load_memory", {"ids": [derived_id]}, agent_id=n.agent, user_id=n.foreign,
        session_id=str(s.second_session), runtime_task_id=str(s.second_run), emit_runtime_hooks=False))
    assert "TEAM_DERIVED_MEMORY_MARKER" in before, before
    await leave_team(owner_sessionmaker, workspace, n.foreign)
    after = str(await n.service.execute("load_memory", {"ids": [derived_id]}, agent_id=n.agent, user_id=n.foreign,
        session_id=str(s.second_session), runtime_task_id=str(s.second_run), emit_runtime_hooks=False))
    assert "TEAM_DERIVED_MEMORY_MARKER" not in after, after


async def test_group_read_requires_the_entire_live_recipient_team_scope(
    readable_team_memory, group_memory, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from tests.integration.test_m4d_group_memory_postgres import group_call

    _s, workspace, _receipt = readable_team_memory
    g = group_memory
    args = {"ids": [f"team_memory:{workspace}:report"]}
    allowed, _invocation = await group_call(g, app_user_sessionmaker, "load_memory", args)
    assert "TEAM_PROJECT_RELEASE_MARKER" in allowed and "ORCHID_PRIVATE_MARKER" not in allowed, allowed
    await leave_team(owner_sessionmaker, workspace, g.n.foreign)
    denied, _invocation = await group_call(g, app_user_sessionmaker, "load_memory", args, include_history=False)
    assert "TEAM_PROJECT_RELEASE_MARKER" not in denied, denied


async def test_group_recipient_inspection_is_not_a_private_source_grant_and_real_grants_still_work(
    readable_team_memory, group_memory, runtime_team, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    import hashlib
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
    from app.models.user import User
    from tests.integration.test_m4d_group_memory_postgres import group_call

    _s, workspace, receipt = readable_team_memory
    g = group_memory
    n = g.n
    path = n.root.parent / "team-upstream.md"
    path.write_text("PERSONAL_TEAM_UPSTREAM_MARKER")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    document_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(KnowledgeDocument(id=document_id, tenant_id=n.tenant, scope_type="person", scope_id=n.user,
            owner_user_id=n.user, source_kind="text", source_sha256=digest, title="Personal team source", status="ready",
            canonical_md_path=path.name, canonical_md_sha256=digest))
        await db.flush()
        db.add(KnowledgeGrant(tenant_id=n.tenant, scope_id=n.user, resource_type="document", resource_id=document_id,
            document_id=document_id, grantee_type="agent", grantee_id=n.agent, permission="read",
            requester_user_id=n.user, purpose="interactive_session", session_id=str(g.session),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)))
        (await db.get(User, n.foreign)).role = "org_admin"
        (await db.get(ContentInputReceipt, receipt.id)).dependencies_json = [
            {"source_kind": "knowledge_document", "source_id": str(document_id), "source_version": digest}]
        await db.commit()
    inspected = await runtime_team[2][1].get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert inspected.status_code == 200, inspected.text
    from app.core.source_read_purpose import source_recipient_read
    from app.services.content_input_receipts import ContentInputReceiptError, group_source_read, validate_browser_input_dependencies

    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        recipient = await db.get(User, n.foreign)
        dependencies = [{"source_kind": "knowledge_document", "source_id": str(document_id), "source_version": digest}]
        with group_source_read():
            await validate_browser_input_dependencies(db, n.tenant, recipient, dependencies)
            with source_recipient_read(session_id=g.session, user_id=n.foreign), pytest.raises(ContentInputReceiptError):
                await validate_browser_input_dependencies(db, n.tenant, recipient, dependencies)
    denied, _invocation = await group_call(g, app_user_sessionmaker, "load_memory", {"ids": [f"team_memory:{workspace}:report"]})
    assert "TEAM_PROJECT_RELEASE_MARKER" not in denied, denied
    async with owner_sessionmaker() as db:
        db.add(KnowledgeGrant(tenant_id=n.tenant, scope_id=n.user, resource_type="document", resource_id=document_id,
            document_id=document_id, grantee_type="user", grantee_id=n.foreign, permission="read"))
        await db.commit()
    allowed, _invocation = await group_call(g, app_user_sessionmaker, "load_memory", {"ids": [f"team_memory:{workspace}:report"]})
    assert "TEAM_PROJECT_RELEASE_MARKER" in allowed, allowed
    still_inspected = await runtime_team[2][1].get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert still_inspected.status_code == 200


async def test_next_provider_request_stops_after_team_membership_is_withdrawn(
    readable_team_memory, app_user_sessionmaker, owner_sessionmaker,
):
    from app.kernel.contracts import ExecutionAuthorityUnavailable
    from app.models.session_v2 import SessionModelResult
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, workspace, _source = readable_team_memory
    n = s.n
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_memory",
        {"ids": [f"team_memory:{workspace}:report"]}, user=n.foreign, session=s.second_session, run=s.second_run)
    assert "TEAM_PROJECT_RELEASE_MARKER" in loaded, loaded
    await leave_team(owner_sessionmaker, workspace, n.foreign)
    with pytest.raises(ExecutionAuthorityUnavailable) as blocked:
        await memory_call(n, app_user_sessionmaker, "save_memory",
            {"content": "Remember a note derived from the team's release cadence", "category": "project"},
            user=n.foreign, session=s.second_session, run=s.second_run, include_history=True)
    assert blocked.value.code == "team_memory_workspace_denied"
    assert blocked.value.status == "denied"
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == s.second_run)))
        assert len(rows) == 1, rows


async def test_ordinary_team_correction_preserves_historical_provenance_and_new_reads_use_current_body(
    readable_team_memory, runtime_team, app_user_sessionmaker,  # noqa: F811
):
    from app.models.session_v2 import SessionModelResult
    from app.services.session_model_inputs import _model_dependencies
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    s, workspace, source = readable_team_memory
    n = s.n
    loaded, _invocation = await memory_call(n, app_user_sessionmaker, "load_memory",
        {"ids": [f"team_memory:{workspace}:report"]}, user=n.foreign, session=s.second_session, run=s.second_run)
    assert "TEAM_PROJECT_RELEASE_MARKER" in loaded, loaded
    revised = await runtime_team[2][0].put("/enterprise/memory/shared", json={"workspace_key": workspace,
        "key": "report", "title": "Revised team release", "content": "Our cadence is TEAM_REVISED_RELEASE_MARKER",
        "mode": "replace", "base_revision": 1})
    assert revised.status_code == 200, revised.text
    current = str(await n.service.execute("load_memory", {"ids": [f"team_memory:{workspace}:report"]},
        agent_id=n.agent, user_id=n.foreign, session_id=str(s.second_session), runtime_task_id=str(s.second_run),
        emit_runtime_hooks=False))
    assert "TEAM_REVISED_RELEASE_MARKER" in current and "TEAM_PROJECT_RELEASE_MARKER" not in current, current
    saved, invocation = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "Retain the historical team release note", "category": "project"}, user=n.foreign,
        session=s.second_session, run=s.second_run, include_history=True)
    assert "Saved to explicit memory overlay" in saved, saved
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == invocation.provider_request_id))
        dependencies, complete = _model_dependencies(model)
        assert complete and {"source_kind": "team_memory", "source_id": str(source.id),
            "source_version": source.content_sha256} in dependencies, dependencies


@pytest.mark.parametrize("surface", ["load", "search", "automatic"])
async def test_private_work_team_memory_uses_its_actual_parent_audience(
    runtime_team, owner_sessionmaker, app_user_sessionmaker, monkeypatch, surface,  # noqa: F811
):
    from app.models.agent_team import AgentTeam
    from app.models.user import User
    from app.memory.activation import ActivationContext
    from app.memory.retriever import MemoryRetriever
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    s, _business_workspace, (writer, inspector) = runtime_team
    n = s.n
    team_id = uuid4()
    workspace = f"agent_team:{team_id}"
    async with owner_sessionmaker() as db:
        db.add(AgentTeam(id=team_id, tenant_id=n.tenant, lead_agent_id=n.agent, parent_session_id=n.session,
            name="Private task team", created_by_user_id=n.user))
        (await db.get(User, n.foreign)).role = "org_admin"
        await db.commit()
    saved = await writer.put("/enterprise/memory/shared", json={"workspace_key": workspace, "key": "report",
        "title": "Task release", "content": "WORK_TEAM_PARENT_MARKER", "mode": "replace"})
    assert saved.status_code == 200, saved.text
    inspected = await inspector.get("/enterprise/memory/shared/report", params={"workspace_key": workspace})
    assert inspected.status_code == 200 and "WORK_TEAM_PARENT_MARKER" in inspected.text

    async def choose(_self, *, items, **_kwargs):
        return [item.metadata["selection_candidate_id"] for item in items], "Use authorized work-team facts"

    monkeypatch.setattr(MemoryRetriever, "_select_with_model", choose)
    outputs = []
    for user, session, run in ((n.user, n.session, n.run), (n.foreign, s.second_session, s.second_run)):
        if surface == "automatic":
            context = ActivationContext(query="release", principal_stack=PrincipalStack(
                current_user=Principal(PrincipalRole.CURRENT_USER, str(user)),
                direct_owner=Principal(PrincipalRole.OWNER, str(n.user))))
            items = await MemoryRetriever(data_root=n.root.parent).retrieve(n.agent, "release", str(session), str(n.tenant),
                runtime_task_id=str(run), activation_context=context,
                rerank_model_config={"provider": "reference", "model": "local"})
            outputs.append("\n".join(item.content for item in items))
        else:
            tool = "search_memory" if surface == "search" else "load_memory"
            args = {"query": "release", "scope": "facts"} if surface == "search" else {"ids": [f"team_memory:{workspace}:report"]}
            outputs.append(str(await n.service.execute(tool, args, agent_id=n.agent, user_id=user,
                session_id=str(session), runtime_task_id=str(run), emit_runtime_hooks=False)))
    assert "WORK_TEAM_PARENT_MARKER" in outputs[0], outputs[0]
    assert "WORK_TEAM_PARENT_MARKER" not in outputs[1], outputs[1]


async def test_group_actor_keeps_authenticated_administrator_employee_use(
    readable_team_memory, group_memory, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from sqlalchemy import delete
    from app.models.agent import AgentPermission
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from tests.integration.test_m4d_group_memory_postgres import group_call

    _s, workspace, _receipt = readable_team_memory
    g = group_memory
    n = g.n
    async with owner_sessionmaker() as db:
        (await db.get(User, n.foreign)).role = "org_admin"
        await db.execute(delete(AgentPermission).where(AgentPermission.agent_id == n.agent))
        await db.execute(delete(ResourcePermission).where(ResourcePermission.resource_type == "agent",
            ResourcePermission.resource_id == n.agent))
        await db.commit()
    loaded, _invocation = await group_call(g, app_user_sessionmaker, "load_memory",
        {"ids": [f"team_memory:{workspace}:report"]}, user_id=n.foreign)
    assert "TEAM_PROJECT_RELEASE_MARKER" in loaded and "ORCHID_PRIVATE_MARKER" not in loaded, loaded


@pytest.mark.parametrize("withdrawal", [None, "team_member", "unknown_recipient"])
async def test_team_source_reaches_actual_terminal_outbox_and_current_recipient_gate(
    readable_team_memory, group_memory, app_user_sessionmaker, owner_sessionmaker, withdrawal,  # noqa: F811
):
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.channel_delivery_service import DeliveryResult
    from tests.integration.test_m4d_group_memory_postgres import finish_group_work, group_call

    _s, workspace, _source = readable_team_memory
    g = group_memory
    loaded, _invocation = await group_call(g, app_user_sessionmaker, "load_memory",
        {"ids": [f"team_memory:{workspace}:report"]})
    assert "TEAM_PROJECT_RELEASE_MARKER" in loaded, loaded
    await finish_group_work(g, app_user_sessionmaker, user_id=g.n.user)
    async with tenant_scoped_session(g.n.tenant, session_factory=app_user_sessionmaker) as db:
        outbox = list(await db.scalars(select(ChannelDeliveryOutbox).where(
            ChannelDeliveryOutbox.runtime_task_id == g.runs[g.n.user])))
        assert len(outbox) == 1
        outbox[0].available_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        delivery_id = outbox[0].id
        await db.commit()
    if withdrawal == "team_member":
        await leave_team(owner_sessionmaker, workspace, g.n.foreign)
    elif withdrawal == "unknown_recipient":
        g.members.add("unlinked-new-recipient")
    sends = []

    async def send(**kwargs):
        sends.append(kwargs)
        return DeliveryResult(ok=True, status="delivered", channel="feishu", message="Controlled transport receipt")

    outcome = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker,
        text_sender=send, file_sender=send).drain_once(worker_id="team-memory-delivery", limit=1)
    assert outcome["claimed"] == 1, outcome
    async with tenant_scoped_session(g.n.tenant, session_factory=app_user_sessionmaker) as db:
        delivery = await db.get(ChannelDeliveryOutbox, delivery_id)
        if withdrawal is None:
            assert outcome["delivered"] == 1 and len(sends) == 1, outcome
            assert sends[0]["reply_target"]["receive_id"] == "reference-memory-group"
            assert delivery.status == "delivered"
        elif withdrawal == "team_member":
            assert not sends and outcome["dead_lettered"] == 1 and delivery.status == "dead_letter", outcome
            assert "team_memory_workspace_denied" in delivery.last_error
        else:
            assert not sends and outcome["retried"] == 1 and delivery.status == "pending", outcome
            assert "group_recipient_identity_unavailable" in delivery.last_error
