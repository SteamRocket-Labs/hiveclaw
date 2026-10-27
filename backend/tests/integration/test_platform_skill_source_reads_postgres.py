"""Platform source reads stay current without repeating work for each file."""

import hashlib
import uuid

from sqlalchemy import event

from app.config import get_settings
from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.config_revision import ConfigRevision
from app.models.tenant import Tenant
from app.models.user import User
from app.services import skill_source_receipts as sources
from app.services.config_versioning import canonical_content_hash
from app.services.skill_seeder import builtin_skill_definitions


async def test_platform_sources_batch_folder_reads_and_observe_later_installations(
    owner_sessionmaker, app_user_sessionmaker, app_user_engine, monkeypatch, tmp_path
):
    tenant_id, other_tenant, user_id, agent_id = (uuid.uuid4() for _ in range(4))
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    workspace = tmp_path / str(agent_id)
    files = {}
    for skill in builtin_skill_definitions():
        if not skill.get("is_default"):
            continue
        for entry in skill["files"]:
            relative = f"skills/{skill['folder_name']}/{entry['path']}"
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(entry["content"], encoding="utf-8")
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    folders = sorted({path.split("/")[1] for path in files})
    assert len(folders) > 1 and len(files) > len(folders)
    content = {"schema": "hive.agent_platform_skills.v1", "files": files}
    async with owner_sessionmaker() as db:
        db.add_all(
            Tenant(id=identity, name="Source read tenant", slug=f"source-read-{identity.hex}")
            for identity in (tenant_id, other_tenant)
        )
        await db.flush()
        db.add(
            User(
                id=user_id,
                username=f"source-read-{user_id.hex}",
                email=f"{user_id.hex}@source.test",
                password_hash="synthetic",
                display_name="Source reader",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Source reader", creator_id=user_id))
        db.add(
            ConfigRevision(
                entity_type="agent_platform_skill_bootstrap",
                entity_id=agent_id,
                tenant_id=tenant_id,
                version=1,
                content=content,
                content_hash=canonical_content_hash(content),
                change_source="platform_init",
            )
        )
        await db.commit()

    bundle_reads, installation_reads = [], []
    bundled_hashes = sources._bundled_platform_skill_hashes

    def observe_bundle():
        bundle_reads.append(1)
        return bundled_hashes()

    def observe_query(_connection, _cursor, statement, _parameters, context, _executemany):
        if statement.lstrip().upper().startswith("SELECT") and any(
            value == "agent_skill_install"
            for parameters in context.compiled_parameters or []
            for value in parameters.values()
            if isinstance(value, str)
        ):
            installation_reads.append(1)

    monkeypatch.setattr(sources, "_bundled_platform_skill_hashes", observe_bundle)
    event.listen(app_user_engine.sync_engine, "before_cursor_execute", observe_query)
    try:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            before = await sources._platform_skill_sources(db, tenant_id, agent_id)
            assert set(before) == set(files)
        async with owner_sessionmaker() as db:
            # Even an inactive installation keeps its lineage; a foreign tenant cannot supersede ours.
            for folder, tenant in ((folders[0], tenant_id), (folders[1], other_tenant)):
                db.add(
                    ConfigRevision(
                        entity_type="agent_skill_install",
                        entity_id=sources._installation_id(agent_id, folder),
                        tenant_id=tenant,
                        version=1,
                        content={},
                        content_hash=canonical_content_hash({}),
                        is_active=False,
                        change_source="install",
                    )
                )
            await db.commit()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            after = await sources._platform_skill_sources(db, tenant_id, agent_id)
            assert set(after) == {path for path in files if path.split("/")[1] != folders[0]}
            assert {path for path in after if path.split("/")[1] == folders[1]}
        assert (len(bundle_reads), len(installation_reads)) == (2, 2)
    finally:
        event.remove(app_user_engine.sync_engine, "before_cursor_execute", observe_query)
