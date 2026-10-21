"""Carry private remote objects into the existing portable recovery bundle."""

import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid

from app.services.original_blob_identity import original_object_key


def remote_objects(args, sql):
    import json

    if sql(args, "SELECT to_regclass('public.storage_blobs') IS NOT NULL") != "t":
        return []
    return json.loads(
        sql(
            args,
            """SELECT coalesce(json_agg(jsonb_build_object(
        'id',id::text,'tenant_id',tenant_id::text,'provider',provider,'bucket',bucket,'object_key',object_key,
        'content_sha256',content_sha256,'size_bytes',size_bytes,'metadata_json',metadata_json)
        || CASE WHEN state='quarantined' THEN jsonb_build_object('state',state) ELSE '{}'::jsonb END
        ORDER BY id),'[]'::json)
        FROM storage_blobs WHERE provider<>'filesystem' AND state IN ('available','quarantined')""",
        )
    )


def has_generational_objects(args, sql):
    return (sql(args, "SELECT to_regclass('public.storage_blobs') IS NOT NULL") == "t"
        and sql(args, "SELECT EXISTS(SELECT 1 FROM storage_blobs WHERE kind='chat_upload' "
            "AND object_key <> tenant_id::text || '/' || content_sha256)") == "t")


def has_quarantined_objects(args, sql):
    return (sql(args, "SELECT to_regclass('public.storage_blobs') IS NOT NULL") == "t"
        and sql(args, "SELECT EXISTS(SELECT 1 FROM storage_blobs WHERE state='quarantined')") == "t")


def object_path(row):
    if (
        not isinstance(row, dict)
        or row.get("provider") != "s3"
        or not isinstance(row.get("metadata_json"), dict)
        or set(row["metadata_json"]) != {"endpoint", "region"}
        or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("content_sha256", "")))
        or type(row.get("size_bytes")) is not int
        or row["size_bytes"] < 0
        or row.get("state", "available") not in {"available", "quarantined"}
    ):
        raise ValueError("Invalid remote object recovery identity")
    if str(uuid.UUID(row["tenant_id"])) != row["tenant_id"] or str(uuid.UUID(row["id"])) != row["id"]:
        raise ValueError("Invalid remote object recovery identity")
    keys = {original_object_key(row["tenant_id"], row["content_sha256"]),
        original_object_key(row["tenant_id"], row["content_sha256"], generation_id=row["id"])}
    if row.get("object_key") not in keys:
        raise ValueError("Invalid remote object recovery namespace")
    return ".storage_objects/durable/" + row["object_key"]


def export_objects(rows, profile, archive, existing, file_tree):
    from app.services.blob_store import BlobLocation
    from app.services.s3_blob_store import s3_store_from_values

    store = s3_store_from_values(profile)
    combined = {row["path"]: row for row in existing}
    seen = set()
    with tempfile.TemporaryDirectory(prefix="hive-backup-objects-") as directory:
        root = Path(directory)
        for row in rows:
            relative = object_path(row)
            if row["id"] in seen or row["bucket"] != store.bucket or row["metadata_json"] != store.identity:
                raise ValueError("Recorded remote object configuration is unavailable")
            seen.add(row["id"])
            target = root / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with (
                store.open_verified_source(BlobLocation("s3", row["bucket"], row["object_key"])) as source,
                target.open("xb") as output,
            ):
                os.chmod(target, 0o600)
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > row["size_bytes"]:
                        raise ValueError("Remote backup object size mismatch")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != row["size_bytes"] or digest.hexdigest() != row["content_sha256"]:
                raise ValueError("Remote backup object content mismatch")
        # The private export tree has known ownership. Merge only missing members;
        # an existing volume payload must be exactly the same canonical bytes.
        import tarfile

        for entry in file_tree(root):
            previous = combined.get(entry["path"])
            if previous is not None:
                if previous["type"] == entry["type"] == "directory":
                    continue
                if previous["type"] == entry["type"] == "file" and (previous["sha256"], previous["size"]) == (
                    entry["sha256"],
                    entry["size"],
                ):
                    continue
                raise ValueError("Remote backup object conflicts with volume data")
            member = tarfile.TarInfo(entry["path"])
            member.mode, member.mtime = entry["mode"], entry["mtime_ns"] / 1e9
            if entry["type"] == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            else:
                member.size = entry["size"]
                with (root / entry["path"]).open("rb") as source:
                    archive.addfile(member, source)
            combined[entry["path"]] = entry
    return sorted(combined.values(), key=lambda row: row["path"])


def verify_objects(rows, entries, *, source_paths=None):
    if not isinstance(rows, list):
        raise ValueError("Invalid remote object recovery list")
    seen, locations = set(), set()
    for row in rows:
        relative = object_path(row)
        entry = entries.get(relative, {})
        if (
            row["id"] in seen
            or relative in locations
            or entry.get("type") != "file"
            or (entry.get("size"), entry.get("sha256")) != (row["size_bytes"], row["content_sha256"])
        ):
            raise ValueError("Remote object recovery bytes are unavailable")
        seen.add(row["id"])
        locations.add(relative)
    if rows:
        from pathlib import PurePosixPath

        if (not isinstance(source_paths, list) or any(not isinstance(path, str) or path not in entries for path in source_paths)
            or len(set(source_paths)) != len(source_paths)):
            raise ValueError("Invalid original volume recovery paths")
        exported = set(locations)
        for path in locations:
            exported.update(str(parent) for parent in PurePosixPath(path).parents if str(parent) != ".")
        if set(entries) != set(source_paths) | exported:
            raise ValueError("Invalid original and exported volume union")


def upgrade_entries(manifest, actual_objects, *, restored):
    expected = []
    for row in manifest["remote_objects"]:
        item = dict(row)
        if restored:
            item.update(provider="filesystem", bucket="durable", metadata_json={
                "restored_from": {key: row[key] for key in ("provider", "bucket", "object_key", "metadata_json")}})
        expected.append(item)
    actual_objects = [dict(row) for row in actual_objects]
    for row in actual_objects:
        if row.get("state") == "available":
            row.pop("state")
    if sorted(actual_objects, key=lambda row: row["id"]) != sorted(expected, key=lambda row: row["id"]):
        raise ValueError("Current object locations differ from the frozen recovery boundary")
    if restored:
        return manifest["entries"]
    source_paths = set(manifest["source_volume_paths"])
    return [entry for entry in manifest["entries"] if entry["path"] in source_paths]


def relocation_sql(rows):
    import json

    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    updates = []
    for row in rows:
        object_path(row)
        metadata = json.dumps(row["metadata_json"], sort_keys=True)
        evidence = json.dumps(
            {"restored_from": {key: row[key] for key in ("provider", "bucket", "object_key", "metadata_json")}},
            sort_keys=True,
        )
        updates.append(f"""UPDATE storage_blobs SET provider='filesystem',bucket='durable',
            metadata_json={literal(evidence)}::jsonb
            WHERE id={literal(row["id"])}::uuid AND tenant_id={literal(row["tenant_id"])}::uuid
            AND provider='s3' AND bucket={literal(row["bucket"])} AND object_key={literal(row["object_key"])}
            AND content_sha256={literal(row["content_sha256"])} AND size_bytes={row["size_bytes"]}
            AND metadata_json={literal(metadata)}::jsonb AND state={literal(row.get("state", "available"))};
            IF NOT FOUND THEN RAISE EXCEPTION 'remote object restore identity mismatch'; END IF;""")
    return "BEGIN; DO $object_restore$ BEGIN " + "\n".join(updates) + " END $object_restore$; COMMIT;"
