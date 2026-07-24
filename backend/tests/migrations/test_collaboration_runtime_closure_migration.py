from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "collaboration_runtime_closure_0717.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("collaboration_runtime_closure_0717", MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_backfills_team_visibility_and_terminal_root_truth() -> None:
    module = _load_migration()

    assert module.revision == "collaboration_runtime_closure_0717"
    assert module.down_revision == "peer_a2a_session_authority_0717"

    team_sql = "".join(module.build_team_member_surface_backfill_sql().lower().split())
    assert "updatechat_sessions" in team_sql
    assert "listed_surface='parent'" in team_sql
    assert "session_kind='team_member'" in team_sql
    assert "runtime_source='team_member'" in team_sql
    assert "source_channel='agent_team'" in team_sql

    a2a_session_sql = "".join(module.build_peer_a2a_session_surface_backfill_sql().lower().split())
    assert "updatechat_sessionsastarget_session" in a2a_session_sql
    assert "fromruntime_tasksastask" in a2a_session_sql
    assert "task.task_typein('delegation','a2a_delegation')" in a2a_session_sql
    assert "setsession_kind='delegation_run'" in a2a_session_sql
    assert "runtime_source='delegation'" in a2a_session_sql
    assert (
        "replace(lower(task.child_session_id),'-','')=replace(lower(target_session.id::text),'-','')" in a2a_session_sql
    )

    root_sql = "".join(module.build_terminal_root_backfill_sql().lower().split())
    assert "updateruntime_root_itemsasitem" in root_sql
    assert "fromruntime_tasksastask" in root_sql
    assert "task.id=item.runtime_task_id" in root_sql
    assert "when'completed'then'completed'" in root_sql
    assert "when'failed'then'failed'" in root_sql
    assert "when'killed'then'killed'" in root_sql
    assert "when'skipped'then'not_admitted'" in root_sql
    assert "session_v2_terminal_backfill_0717" in root_sql
    assert "item.statenotin('completed','failed','killed','skipped','cancelled','not_admitted')" in root_sql

    item_sql = "".join(module.build_collaboration_thread_item_backfill_sql().lower().split())
    assert "updatechat_transcript_events" in item_sql
    assert "then'agent_team_activity'" in item_sql
    assert "then'peer_a2a_activity'" in item_sql
    assert "event_type='child_session'" in item_sql
    assert "metadata_json->>'source'" in item_sql
    assert "='subagent'" in item_sql
    assert "metadata_json->>'action_kind'" in item_sql
    assert "='a2a_delegation'" in item_sql
    assert "whereitem_type='subagent_activity'" not in item_sql


async def test_upgrade_repairs_exact_legacy_collaboration_rows(migrated_pg_url: str) -> None:
    from app.models import import_all_models
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.migrations.conftest import (
        _alembic_downgrade,
        _alembic_upgrade,
        insert_runtime_task_at_schema_revision,
    )

    import_all_models()
    _alembic_downgrade(migrated_pg_url, "peer_a2a_session_authority_0717")
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id, user_id, parent_agent_id, child_agent_id = (uuid4() for _ in range(4))
    parent_session_id, team_session_id, child_session_id, task_id = (uuid4() for _ in range(4))
    try:
        async with session_factory() as db:
            db.add(Tenant(id=tenant_id, name="Collaboration Repair", slug=f"collab-repair-{tenant_id.hex[:8]}"))
            db.add(
                User(
                    id=user_id,
                    username=f"collab-repair-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@collab-repair.test",
                    password_hash="x",
                    display_name="Collaboration Repair",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add_all(
                [
                    Agent(id=parent_agent_id, tenant_id=tenant_id, name="Lead", creator_id=user_id),
                    Agent(id=child_agent_id, tenant_id=tenant_id, name="Peer", creator_id=user_id),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    ChatSession(
                        id=parent_session_id,
                        agent_id=parent_agent_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    ),
                    ChatSession(
                        id=team_session_id,
                        agent_id=parent_agent_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        session_kind="team_member",
                        runtime_source="team_member",
                        listed_surface="chat",
                        parent_session_id=parent_session_id,
                    ),
                    ChatSession(
                        id=child_session_id,
                        agent_id=child_agent_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        session_kind="subagent_run",
                        runtime_source="subagent",
                        listed_surface="parent",
                        parent_session_id=parent_session_id,
                    ),
                ]
            )
            await db.flush()
            await insert_runtime_task_at_schema_revision(
                db,
                id=task_id,
                tenant_id=tenant_id,
                task_type="delegation",
                status="failed",
                parent_agent_id=parent_agent_id,
                child_agent_id=child_agent_id,
                parent_session_id=parent_session_id.hex,
                child_session_id=child_session_id.hex,
                root_user_id=user_id,
                root_session_id=parent_session_id.hex,
                root_runtime_task_id=task_id,
                metadata_json={"interaction_type": "delegation"},
            )
            await db.flush()
            child_session = await db.get(ChatSession, child_session_id)
            assert child_session is not None
            child_session.runtime_task_id = task_id
            db.add(
                RuntimeRootItem(
                    tenant_id=tenant_id,
                    root_runtime_task_id=task_id,
                    runtime_task_id=task_id,
                    source_agent_id=parent_agent_id,
                    root_user_id=user_id,
                    root_session_id=parent_session_id.hex,
                    intent_key=f"delegation:{task_id.hex}",
                    work_type="delegation",
                    target_ref=f"agent:{child_agent_id}",
                    path_json=[f"agent:{parent_agent_id}", f"agent:{child_agent_id}"],
                    state="running",
                    admission_disposition="admitted",
                )
            )
            db.add(
                ChatTranscriptEvent(
                    sequence=1,
                    tenant_id=tenant_id,
                    agent_id=child_agent_id,
                    session_id=child_session_id,
                    run_id=task_id,
                    schema_version=1,
                    item_type="subagent_activity",
                    item_status="failed",
                    actor_type="system",
                    event_type="delegation_run",
                    visibility_scope="agent_owner",
                    listed_surface="chat",
                    metadata_json={"interaction_type": "delegation"},
                    projection_status="not_requested",
                )
            )
            await db.commit()

        _alembic_upgrade(migrated_pg_url, "head")

        async with session_factory() as db:
            team_session = await db.get(ChatSession, team_session_id)
            child_session = await db.get(ChatSession, child_session_id)
            root_item = await db.scalar(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == task_id))
            collaboration_event = await db.scalar(
                select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == task_id)
            )

            assert team_session is not None and team_session.listed_surface == "parent"
            assert child_session is not None
            assert (child_session.session_kind, child_session.runtime_source, child_session.listed_surface) == (
                "delegation_run",
                "delegation",
                "chat",
            )
            assert root_item is not None
            assert (root_item.state, root_item.admission_disposition, root_item.reason_code) == (
                "failed",
                "admitted",
                "session_v2_terminal_backfill_0717",
            )
            assert collaboration_event is not None
            assert collaboration_event.item_type == "peer_a2a_activity"
    finally:
        _alembic_upgrade(migrated_pg_url, "head")
        await engine.dispose()
