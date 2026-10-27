"""Single-file source validation reads its package and retains current authority."""

import hashlib
from pathlib import Path
import uuid

import pytest

from app.config import get_settings
from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.config_revision import ConfigRevision
from app.models.tenant import Tenant
from app.models.user import User
from app.packs.catalog_reader import PackCatalogReader
from app.services import skill_seeder, skill_source_receipts as sources
from app.services.config_versioning import canonical_content_hash
from app.services.content_input_receipts import ContentInputReceiptError


@pytest.mark.parametrize("package_kind", ["pack", "builtin"])
async def test_single_platform_dependency_preserves_bytes_lineage_and_full_inventory(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, package_kind
):
    tenant_id, user_id, agent_id, revision_id = (uuid.uuid4() for _ in range(4))
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    workspace = tmp_path / str(agent_id)
    backend = Path(sources.__file__).resolve().parents[2]
    files = {}
    for skill in skill_seeder.builtin_skill_definitions(pack_dirs=(backend / "packs",)):
        if not skill.get("is_default"):
            continue
        for entry in skill["files"]:
            relative = f"skills/{skill['folder_name']}/{entry['path']}"
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(entry["content"], encoding="utf-8")
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    source_path = next(
        path
        for path in sorted(files)
        if path.endswith("/SKILL.md")
        and (path.split("/")[1] in skill_seeder.DEFAULT_PACK_SKILL_FOLDERS) == (package_kind == "pack")
    )
    folder = source_path.split("/")[1]
    content = {"schema": "hive.agent_platform_skills.v1", "files": files}
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Dependency reader", slug=f"dependency-reader-{tenant_id.hex}"))
        await db.flush()
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"dependency-{user_id.hex}",
                email=f"{user_id.hex}@dependency.test",
                password_hash="synthetic",
                display_name="Dependency reader",
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Dependency reader", creator_id=user_id))
        db.add(
            ConfigRevision(
                id=revision_id,
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
    dependency = {
        "source_kind": "agent_platform_skill_file",
        "source_id": f"{agent_id}\n{source_path}\n{revision_id}",
        "source_version": files[source_path],
    }
    catalog_reads = []
    discover = PackCatalogReader.discover

    def observe_catalog(reader):
        catalog_reads.append(1)
        return discover(reader)

    monkeypatch.setattr(PackCatalogReader, "discover", observe_catalog)

    async def validate(selected=dependency):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            actor = await db.get(User, user_id)
            await sources.validate_skill_file_dependency(db, tenant_id, selected, user=actor, runtime_request=None)

    await validate()
    original = (workspace / source_path).read_bytes()
    (workspace / source_path).write_bytes(b"CHANGED_CURRENT_SOURCE")
    with pytest.raises(ContentInputReceiptError) as changed:
        await validate()
    assert changed.value.code == "content_source_changed"
    (workspace / source_path).write_bytes(original)
    await validate()

    forged = b"A bootstrap claim cannot replace the original application package."
    forged_digest = hashlib.sha256(forged).hexdigest()
    (workspace / source_path).write_bytes(forged)
    with pytest.raises(ContentInputReceiptError) as unbound:
        await validate({**dependency, "source_version": forged_digest})
    assert unbound.value.code == "content_source_unavailable"
    (workspace / source_path).write_bytes(original)
    await validate()

    async with owner_sessionmaker() as db:
        db.add(
            ConfigRevision(
                entity_type="agent_skill_install",
                entity_id=sources._installation_id(agent_id, folder),
                tenant_id=tenant_id,
                version=1,
                content={},
                content_hash=canonical_content_hash({}),
                is_active=False,
                change_source="install",
            )
        )
        await db.commit()
    with pytest.raises(ContentInputReceiptError) as superseded:
        await validate()
    assert superseded.value.code == "content_source_unavailable"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        inventory = await sources._platform_skill_sources(db, tenant_id, agent_id)
        assert set(inventory) == {path for path in files if path.split("/")[1] != folder}
    # Six dependency reads and one full inventory; only the latter needs unrelated packs.
    assert len(catalog_reads) == (7 if package_kind == "pack" else 1)
