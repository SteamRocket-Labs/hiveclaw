"""Periodic retention preserves delivered versions and current HTTP authority."""

import os
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import text

from app.api.files import _load_download_user_from_jwt as real_download_identity
from app.database import pin_rls_tenant_context
from tests.integration.test_m1g22_artifact_versions_postgres import delivered  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_heartbeat_retention_keeps_exact_saved_versions_for_real_jwt_downloads(delivered, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import files
    from app.core.security import create_access_token
    from app.database import get_db
    from app.models.user import User
    from app.services.chat_artifact_delivery import CHAT_ARTIFACT_SNAPSHOT_DIR, CHAT_ARTIFACT_SNAPSHOT_GC_REPORT
    from app.services.heartbeat import _run_chat_artifact_snapshot_retention

    d = delivered
    d.target.write_bytes(b"BBBB")
    second = await d.publish()
    assert second.id != d.first.id
    snapshots = d.root / CHAT_ARTIFACT_SNAPSHOT_DIR
    orphan = snapshots / "expired/orphan.txt"
    orphan.parent.mkdir()
    orphan.write_bytes(b"DISPOSABLE")
    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    for path in (d.root / d.first.snapshot_json["snapshot_storage_path"],
        d.root / second.snapshot_json["snapshot_storage_path"], orphan):
        os.utime(path, (old_time, old_time))
    caches = d.root / ".office_meta/artifacts"
    retained_cache = caches / str(d.first.id)
    disposable_cache = caches / str(uuid.uuid4())
    for directory in (retained_cache, disposable_cache):
        directory.mkdir(parents=True)
        (directory / "preview.html").write_bytes(b"PREVIEW")

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert (await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)
        report = await _run_chat_artifact_snapshot_retention(d.agent, db=db)
        other = User(id=uuid.uuid4(), tenant_id=d.tenant, username=uuid.uuid4().hex,
            email=uuid.uuid4().hex + "@retention.test", display_name="Other colleague", role="member", password_hash="test-only")
        db.add(other)
        await db.commit()
    assert not orphan.exists() and not disposable_cache.exists()
    assert retained_cache.is_dir()
    assert report["removed_count"] == 1 and report["removed_bytes"] == len(b"DISPOSABLE")
    assert report["kept_referenced_count"] == 2 and report["office_preview_cache_removed_count"] == 1
    assert (d.root / CHAT_ARTIFACT_SNAPSHOT_GC_REPORT).is_file()

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            yield db
            await db.commit()

    monkeypatch.setattr(files, "_load_download_user_from_jwt", real_download_identity)
    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_db] = scoped_db
    owner_token = create_access_token(str(d.user.id), "member", str(d.tenant))
    other_token = create_access_token(str(other.id), "member", str(d.tenant))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://retention.test") as client:
        for artifact, expected in ((d.first, b"AAAA"), (second, b"BBBB")):
            url = f"/agents/{d.agent}/files/artifacts/{artifact.id}"
            for suffix in ("/content", "/download"):
                response = await client.get(url + suffix, headers={"Authorization": "Bearer " + owner_token})
                assert response.status_code == 200, response.text
                assert (response.json()["content"].encode() if suffix == "/content" else response.content) == expected
                denied = await client.get(url + suffix, headers={"Authorization": "Bearer " + other_token})
                assert denied.status_code == 403, denied.text
        assert d.target.read_bytes() == b"BBBB"
