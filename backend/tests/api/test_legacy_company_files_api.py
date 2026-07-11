from __future__ import annotations

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
