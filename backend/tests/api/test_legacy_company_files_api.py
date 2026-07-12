from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _FakeDb:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_legacy_company_files_status_and_export_are_admin_only_and_audited(monkeypatch, tmp_path) -> None:
    from app.api import enterprise

    tenant_id = uuid4()
    company_dir = tmp_path / f"enterprise_info_{tenant_id}"
    company_dir.mkdir()
    (company_dir / "company_profile.md").write_text("# Company\n", encoding="utf-8")
    (company_dir / "legacy.md").write_text("legacy\n", encoding="utf-8")
    monkeypatch.setattr(enterprise.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_resolve(_db, _user, requested_tenant_id):
        assert requested_tenant_id == str(tenant_id)
        return tenant_id

    monkeypatch.setattr(enterprise, "resolve_and_pin_tenant_scope", fake_resolve)
    db = _FakeDb()
    member = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="member")
    admin = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")

    with pytest.raises(HTTPException) as exc:
        await enterprise.get_legacy_company_files_status(
            tenant_id=str(tenant_id),
            current_user=member,
            db=db,
        )
    assert exc.value.status_code == 403

    status = await enterprise.get_legacy_company_files_status(
        tenant_id=str(tenant_id),
        current_user=admin,
        db=db,
    )
    assert status == {
        "available": True,
        "file_count": 1,
        "total_bytes": len(b"legacy\n"),
        "excluded_symlink_count": 0,
        "read_only": True,
        "retired": True,
        "surface_kind": "legacy_company_files_quarantine",
        "company_kb_available": False,
        "agent_consumable": False,
    }

    response = await enterprise.export_legacy_company_files(
        tenant_id=str(tenant_id),
        current_user=admin,
        db=db,
    )
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert response.media_type == "application/zip"
    assert body.startswith(b"PK")
    assert "legacy-company-files" in response.headers["content-disposition"]
    assert db.commits == 1
    assert len(db.added) == 1
    assert db.added[0].action == "legacy_company_files_exported"
    assert db.added[0].tenant_id == tenant_id


@pytest.mark.asyncio
async def test_legacy_company_files_io_is_offloaded_and_source_failures_are_recoverable(monkeypatch, tmp_path) -> None:
    from app.api import enterprise
    from app.services import legacy_company_files

    tenant_id = uuid4()
    company_dir = tmp_path / f"enterprise_info_{tenant_id}"
    company_dir.mkdir()
    (company_dir / "legacy.md").write_text("legacy\n", encoding="utf-8")
    monkeypatch.setattr(enterprise.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_resolve(_db, _user, _requested_tenant_id):
        return tenant_id

    calls: list[str] = []

    async def fake_run_sync(function, *args, **_kwargs):
        target = getattr(function, "func", function)
        calls.append(target.__name__)
        return function(*args)

    monkeypatch.setattr(enterprise, "resolve_and_pin_tenant_scope", fake_resolve)
    monkeypatch.setattr(enterprise.anyio.to_thread, "run_sync", fake_run_sync)
    admin = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")
    db = _FakeDb()

    await enterprise.get_legacy_company_files_status(
        tenant_id=str(tenant_id),
        current_user=admin,
        db=db,
    )
    response = await enterprise.export_legacy_company_files(
        tenant_id=str(tenant_id),
        current_user=admin,
        db=db,
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body.startswith(b"PK")
    assert calls[:2] == ["scan_legacy_company_files", "build_legacy_company_files_export"]
    assert "read" in calls

    def unavailable(_company_dir):
        raise legacy_company_files.LegacyCompanyFilesUnavailableError("internal path detail")

    monkeypatch.setattr(legacy_company_files, "scan_legacy_company_files", unavailable)
    with pytest.raises(HTTPException) as exc:
        await enterprise.get_legacy_company_files_status(
            tenant_id=str(tenant_id),
            current_user=admin,
            db=db,
        )
    assert exc.value.status_code == 503
    assert "internal path detail" not in str(exc.value.detail)

    def changed(_company_dir):
        raise legacy_company_files.LegacyCompanyFilesChangedError("internal path detail")

    monkeypatch.setattr(legacy_company_files, "scan_legacy_company_files", changed)
    with pytest.raises(HTTPException) as exc:
        await enterprise.get_legacy_company_files_status(
            tenant_id=str(tenant_id),
            current_user=admin,
            db=db,
        )
    assert exc.value.status_code == 409
    assert "internal path detail" not in str(exc.value.detail)

    def unavailable_export(_company_dir, *, tenant_id):
        assert tenant_id == str(tenant_id_value)
        raise legacy_company_files.LegacyCompanyFilesUnavailableError("internal export path detail")

    tenant_id_value = tenant_id
    monkeypatch.setattr(legacy_company_files, "build_legacy_company_files_export", unavailable_export)
    with pytest.raises(HTTPException) as exc:
        await enterprise.export_legacy_company_files(
            tenant_id=str(tenant_id),
            current_user=admin,
            db=db,
        )
    assert exc.value.status_code == 503
    assert "internal export path detail" not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_legacy_company_files_cancellation_propagates_and_early_stream_close_releases_archive(
    monkeypatch, tmp_path
) -> None:
    from app.api import enterprise
    from app.services import legacy_company_files

    tenant_id = uuid4()
    monkeypatch.setattr(enterprise.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_resolve(_db, _user, _requested_tenant_id):
        return tenant_id

    monkeypatch.setattr(enterprise, "resolve_and_pin_tenant_scope", fake_resolve)
    admin = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, role="org_admin")
    db = _FakeDb()

    async def cancelled_run_sync(_function, *_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(enterprise.anyio.to_thread, "run_sync", cancelled_run_sync)
    with pytest.raises(asyncio.CancelledError):
        await enterprise.get_legacy_company_files_status(
            tenant_id=str(tenant_id),
            current_user=admin,
            db=db,
        )

    stream = io.BytesIO(b"x" * (1024 * 1024 + 1))
    archive = legacy_company_files.LegacyCompanyFilesExport(
        stream=stream,
        size_bytes=1024 * 1024 + 1,
        filename="legacy.zip",
        snapshot=legacy_company_files.LegacyCompanyFilesSnapshot(
            files=(
                legacy_company_files.LegacyCompanyFile(
                    relative_path="legacy.md",
                    absolute_path=tmp_path / "legacy.md",
                    size_bytes=1,
                    sha256="hash",
                ),
            )
        ),
    )

    async def successful_run_sync(function, *args, **_kwargs):
        target = getattr(function, "func", function)
        if target.__name__ == "build_legacy_company_files_export":
            return archive
        return function(*args)

    monkeypatch.setattr(enterprise.anyio.to_thread, "run_sync", successful_run_sync)
    response = await enterprise.export_legacy_company_files(
        tenant_id=str(tenant_id),
        current_user=admin,
        db=db,
    )
    first_chunk = await anext(response.body_iterator)
    assert first_chunk
    assert stream.closed is False

    await response.body_iterator.aclose()

    assert stream.closed is True
