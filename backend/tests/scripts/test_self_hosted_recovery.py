"""Private backup inputs and descriptor-safe archive validation."""

import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def recovery(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("app.scripts.self_hosted_recovery")


@pytest.fixture
def bundle(tmp_path, recovery):
    from configure_self_hosted import configure

    root = tmp_path / "agents"
    (root / "employee/workspace").mkdir(parents=True)
    (root / "employee/workspace/report.txt").write_bytes(b"EXACT_SAVED_VERSION")
    (root / "employee/workspace/external").symlink_to("/external/retained-data")
    path = tmp_path / "backup"
    path.mkdir(mode=0o700)
    profile = configure(tmp_path / "profile", public_origin="http://localhost:3008", port=3008)
    (path / "installation.env").write_bytes(profile.read_bytes())
    (path / "installation.env").chmod(0o600)
    (path / "database.dump").write_bytes(b"CONTROLLED_ARCHIVE_BYTES")
    with tarfile.open(path / "agents.tar.gz", "w:gz") as archive:
        entries = recovery.file_tree(root, archive)
    manifest = {"schema": recovery.SCHEMA, "postgres_major": 17, "entries": entries,
        "files": {name: {"bytes": (path / name).stat().st_size, "sha256": recovery.digest(path / name)}
            for name in ("installation.env", "database.dump", "agents.tar.gz")}}
    recovery.write_json(path / "manifest.json", manifest)
    return SimpleNamespace(path=path, manifest=manifest, root=root, profile=profile,
        fingerprint=recovery.digest(path / "manifest.json"))


def test_recovery_profile_retains_keys_in_a_new_project_without_replacing_existing_profile(tmp_path, recovery):
    from configure_self_hosted import configure, read_profile, SECRET_KEYS

    source = configure(tmp_path / "source", public_origin="https://old.example.com", port=3008)
    target = configure(tmp_path / "target", public_origin="https://new.example.com", port=3010, recovery_profile=source)
    old, new = read_profile(source), read_profile(target)
    assert all(old[key] == new[key] for key in SECRET_KEYS)
    assert old["HIVE_INSTALL_ID"] != new["HIVE_INSTALL_ID"]
    assert new["HIVE_INSTALL_FRONTEND_PORT"] == "3010"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        configure(target.parent, public_origin="https://new.example.com", port=3010, recovery_profile=source)
    assert target.read_bytes() == before


def test_verify_observes_exact_archive_and_preserves_links_as_data(bundle, recovery):
    manifest = recovery.verify(bundle.path, bundle.fingerprint)
    assert manifest == bundle.manifest
    assert next(row for row in manifest["entries"] if row["type"] == "symlink")["target"] == "/external/retained-data"
    assert next(row for row in manifest["entries"] if row["type"] == "file")["sha256"] == hashlib.sha256(b"EXACT_SAVED_VERSION").hexdigest()


@pytest.mark.parametrize("invalid", [None, {}, [None], [{"provider": "s3"}]])
def test_invalid_remote_recovery_manifest_cannot_hide_missing_object_bytes(bundle, recovery, invalid):
    bundle.manifest["remote_objects"] = invalid
    (bundle.path / "manifest.json").write_text(json.dumps(bundle.manifest))
    with pytest.raises(recovery.RecoveryError, match="backup_remote_objects_invalid"):
        recovery.verify(bundle.path, recovery.digest(bundle.path / "manifest.json"))


@pytest.mark.parametrize("name", ["database.dump", "agents.tar.gz", "installation.env", "manifest.json"])
def test_changed_backup_is_refused_before_any_restore_effect(bundle, recovery, name):
    (bundle.path / name).write_bytes(b"CHANGED_PRIVATE_BYTES")
    with pytest.raises(recovery.RecoveryError):
        recovery.verify(bundle.path, bundle.fingerprint)
    assert (bundle.root / "employee/workspace/report.txt").read_bytes() == b"EXACT_SAVED_VERSION"


@pytest.mark.parametrize("invalid", ["../escape", "/absolute", "double//separator", "missing/parent/file"])
def test_unsafe_manifest_paths_cannot_be_extracted(bundle, recovery, invalid):
    bundle.manifest["entries"][0]["path"] = invalid
    (bundle.path / "manifest.json").write_text(json.dumps(bundle.manifest))
    with pytest.raises(recovery.RecoveryError):
        recovery.verify(bundle.path, recovery.digest(bundle.path / "manifest.json"))


def test_profile_reader_refuses_aliases_and_public_permissions(tmp_path, recovery):
    from configure_self_hosted import configure, read_profile

    profile = configure(tmp_path / "profile", public_origin="http://localhost:3008", port=3008)
    link = tmp_path / "linked.env"
    link.symlink_to(profile)
    with pytest.raises(OSError):
        read_profile(link)
    profile.chmod(0o644)
    with pytest.raises(ValueError):
        read_profile(profile)


def test_capture_refuses_shared_file_ownership_without_reinterpreting_it(tmp_path, recovery):
    root = tmp_path / "agents"
    root.mkdir()
    source = root / "original.txt"
    source.write_bytes(b"SHARED")
    os.link(source, root / "other.txt")
    with pytest.raises(recovery.RecoveryError, match="special_or_shared_file_unsupported"):
        recovery.file_tree(root)


def test_maintenance_image_python_layout_has_no_application_dependency(bundle, tmp_path):
    image = tmp_path / "maintenance-image/opt/hive"
    for relative, target in (("scripts/self_hosted_recovery.py", "scripts/self_hosted_recovery.py"),
        ("backend/app/scripts/self_hosted_recovery.py", "backend/app/scripts/self_hosted_recovery.py"),
        ("backend/app/scripts/object_storage_recovery.py", "backend/app/scripts/object_storage_recovery.py"),
        ("backend/app/services/blob_store.py", "backend/app/services/blob_store.py"),
        ("backend/app/services/original_blob_identity.py", "backend/app/services/original_blob_identity.py"),
        ("backend/app/services/s3_blob_store.py", "backend/app/services/s3_blob_store.py")):
        destination = image / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    result = subprocess.run([sys.executable, "-I", "-B", str(image / "scripts/self_hosted_recovery.py"), "verify", str(bundle.path),
        "--manifest-sha256", bundle.fingerprint], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "verified"
