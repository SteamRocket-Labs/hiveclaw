"""Member retirement preserves history and keeps unrelated memories usable."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.organization_identity import ContentInputReceipt
from app.models.user import User
from tests.integration.test_d11_member_settings_postgres import _client
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import (
    memory_call, new_owner_work, shared_memory,  # noqa: F401
)
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _entry(root, bot, content, entry_id):
    from app.memory.explicit_overlay import prepare_explicit_memory_entry, stage_explicit_memory_entry
    from app.services.agent_asset_transaction import AgentAssetTransaction

    prepared = prepare_explicit_memory_entry(agent_id=bot, data_root=root, entry_id=entry_id,
        decision=SimpleNamespace(category="user", content=content, sensitivity="normal", metadata={}),
        source_refs=[], target_hint="user", origin="explicit_user_request")
    with AgentAssetTransaction(root / str(bot), operation="test_member_retirement") as tx:
        stage_explicit_memory_entry(tx, prepared)
        tx.commit()
    return prepared


def _receipt(tenant, bot, user, session, prepared):
    return ContentInputReceipt(id=uuid4(), tenant_id=tenant, carrier_kind="explicit_memory",
        carrier_ref=f"{bot}\n{prepared.entry.entry_id}", revision=1,
        content_sha256=hashlib.sha256(prepared.rendered.encode()).hexdigest(), dependencies_json=[],
        authority_json={"schema": "hive.explicit_memory_authority.v1", "state": "active",
            "tenant_id": str(tenant), "agent_id": str(bot), "owner_user_id": str(user),
            "audience_kind": "private", "source_session_id": str(session), "metadata": prepared.entry.metadata})


@pytest.mark.asyncio
async def test_retiring_recalled_a_preserves_dependent_b_list_recall_and_delete(
    shared_memory, owner_sessionmaker, app_user_sessionmaker,
):
    from app.services.explicit_memory_authority import source_dependency

    n = shared_memory.n
    a_id = shared_memory.entry_id
    async with owner_sessionmaker() as db:
        user = await db.get(User, n.user)
        user.role = "member"
        a_receipt = await db.scalar(select(ContentInputReceipt).where(
            ContentInputReceipt.carrier_ref == f"{n.agent}\n{a_id}"))
        a_dependency = source_dependency(a_receipt)
        await db.commit()
    later_session, later_run = await new_owner_work(n, owner_sessionmaker)
    loaded, _ = await memory_call(n, app_user_sessionmaker, "load_memory", {"ids": [a_id]},
        session=later_session, run=later_run)
    assert "ORCHID_PRIVATE_MARKER" in loaded, loaded
    saved, _ = await memory_call(n, app_user_sessionmaker, "save_memory",
        {"content": "Remember the dependent project fact B_DEPENDENT_MARKER", "category": "project"},
        session=later_session, run=later_run, include_history=True)
    assert "B_DEPENDENT_MARKER" in saved, saved
    async with owner_sessionmaker() as db:
        b_receipt = await db.scalar(select(ContentInputReceipt).where(
            ContentInputReceipt.tenant_id == n.tenant,
            ContentInputReceipt.carrier_kind == "explicit_memory",
            ContentInputReceipt.carrier_ref != f"{n.agent}\n{a_id}",
            ContentInputReceipt.authority_json["state"].astext == "active"))
        assert a_dependency in b_receipt.dependencies_json
        b_id = b_receipt.carrier_ref.split("\n", 1)[1]
    path = f"/api/enterprise/memory/agents/{n.agent}/my-entries"
    async with await _client(owner_sessionmaker, n.user) as http:
        assert (await http.delete(f"{path}/{a_id}")).status_code == 200
        listed = await http.get(path)
        assert listed.status_code == 200, listed.text
        assert [entry["id"] for entry in listed.json()] == [b_id]
        recalled = str(await n.service.execute("search_memory", {"query": "project", "scope": "facts"},
            agent_id=n.agent, user_id=n.user, session_id=str(later_session),
            runtime_task_id=str(later_run), emit_runtime_hooks=False))
        assert "B_DEPENDENT_MARKER" in recalled, recalled
        assert "ORCHID_PRIVATE_MARKER" not in recalled, recalled
        assert (await http.delete(f"{path}/{b_id}")).status_code == 200
    async with owner_sessionmaker() as db:
        revisions = (await db.execute(select(ContentInputReceipt.revision,
            ContentInputReceipt.authority_json["state"].astext).where(
            ContentInputReceipt.carrier_ref == f"{n.agent}\n{a_id}").order_by(ContentInputReceipt.revision))).all()
        assert revisions == [(1, "active"), (2, "retired")]
    after = str(await n.service.execute("load_memory", {"ids": [a_id, b_id]}, agent_id=n.agent,
        user_id=n.user, session_id=str(later_session), runtime_task_id=str(later_run), emit_runtime_hooks=False))
    assert "ORCHID_PRIVATE_MARKER" not in after and "B_DEPENDENT_MARKER" not in after, after


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["list", "delete"])
async def test_bad_source_is_local_and_own_entry_can_still_be_retired(owner_sessionmaker, action):
    tenant, user, bot, session, _ = await _seed_session(owner_sessionmaker)
    root = Path(get_settings().AGENT_DATA_DIR)
    good = _entry(root, bot, "An available fact", "explicit-good")
    bad = _entry(root, bot, "A fact with a missing source session", "explicit-bad")
    async with owner_sessionmaker() as db:
        member = await db.get(User, user)
        member.role = "member"
        db.add_all([_receipt(tenant, bot, user, session, good), _receipt(tenant, bot, user, uuid4(), bad)])
        await db.commit()
    path = f"/api/enterprise/memory/agents/{bot}/my-entries"
    async with await _client(owner_sessionmaker, user) as http:
        if action == "list":
            listed = await http.get(path)
            assert listed.status_code == 200, listed.text
            assert listed.json() == [{"id": good.entry.entry_id, "content": "An available fact"}]
        else:
            deleted = await http.delete(f"{path}/{bad.entry.entry_id}")
            assert deleted.status_code == 200, deleted.text
        assert (await http.delete(f"{path}/{good.entry.entry_id}")).status_code == 200


@pytest.mark.asyncio
async def test_member_memory_order_is_stable_by_identity(owner_sessionmaker):
    tenant, user, bot, session, _ = await _seed_session(owner_sessionmaker)
    root = Path(get_settings().AGENT_DATA_DIR)
    entries = [_entry(root, bot, name, f"explicit-{name}") for name in ["z", "b", "a"]]
    async with owner_sessionmaker() as db:
        db.add_all([_receipt(tenant, bot, user, session, entry) for entry in entries])
        await db.commit()
    async with await _client(owner_sessionmaker, user) as http:
        listed = await http.get(f"/api/enterprise/memory/agents/{bot}/my-entries")
        assert [entry["id"] for entry in listed.json()] == ["explicit-a", "explicit-b", "explicit-z"]
