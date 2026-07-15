from __future__ import annotations


def test_storage_records_are_strict_tenant_scoped() -> None:
    from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun

    for model in (StorageBlob, StorageBlobRef, StorageGCRun):
        assert model.__table__.c.tenant_id.nullable is False
    assert StorageBlob.__table__.c.content_sha256.type.length == 64
    assert StorageBlob.__table__.c.size_bytes.nullable is False
    assert StorageBlobRef.__table__.c.legal_hold.nullable is False
    assert StorageGCRun.__table__.c.manifest_sha256.type.length == 64


def test_storage_tables_are_in_fresh_bootstrap_rls_manifest() -> None:
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, STRICT_TENANT_RLS_TABLES

    expected = {"storage_blobs", "storage_blob_refs", "storage_gc_runs"}
    assert expected <= set(RLS_FORCED_TENANT_TABLES)
    assert expected <= set(STRICT_TENANT_RLS_TABLES)
