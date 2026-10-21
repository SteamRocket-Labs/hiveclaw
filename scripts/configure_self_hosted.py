#!/usr/bin/env python3
"""Create a private, non-overwriting profile for the existing application images."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import tempfile
import sys
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.scripts.self_hosted_recovery import SECRET_KEYS, read_profile  # noqa: E402


def configure(directory: Path, *, public_origin: str, port: int, recovery_profile: Path | None = None,
    object_storage_profile: Path | None = None) -> Path:
    origin = urlsplit(public_origin)
    if (origin.scheme not in {"http", "https"} or not origin.hostname or origin.username or origin.password
        or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise ValueError("public-origin must be an HTTP(S) origin without credentials, path, query or fragment")
    if any(char.isspace() for char in public_origin):
        raise ValueError("public-origin must not contain whitespace")
    hostname = origin.hostname.encode("idna").decode("ascii").lower()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if origin.port is not None and origin.port != {"http": 80, "https": 443}[origin.scheme]:
        authority += f":{origin.port}"
    public_origin = f"{origin.scheme}://{authority}"
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    recovered = read_profile(recovery_profile) if recovery_profile is not None else {}
    values = {"HIVE_INSTALL_ID": "hive_" + uuid.uuid4().hex[:20], "HIVE_INSTALL_SOURCE_ROOT": str(ROOT),
        "HIVE_INSTALL_FRONTEND_PORT": str(port),
        "BLOB_STORAGE_PROVIDER": "filesystem",
        "HIVE_INSTALL_CORS_ORIGINS": json.dumps([public_origin.rstrip("/")]),
        **{key: recovered.get(key) or secrets.token_hex(32) for key in SECRET_KEYS}}
    if object_storage_profile is not None:
        import stat
        from app.services.s3_blob_store import s3_store_from_values

        descriptor = os.open(object_storage_profile, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 64 * 1024:
                raise ValueError("Object storage profile must be private and bounded")
            selected = json.load(source)
        allowed = {"endpoint", "region", "bucket", "access_key_id", "secret_access_key", "session_token"}
        if not isinstance(selected, dict) or set(selected) - allowed:
            raise ValueError("Invalid object storage profile")
        storage = {"BLOB_S3_" + key.upper(): selected.get(key, "us-east-1" if key == "region" else "") for key in allowed}
        s3_store_from_values(storage)
        values.update(storage, BLOB_STORAGE_PROVIDER="s3")
    if any(any(char in value for char in "\r\n\0") for value in values.values()):
        raise ValueError("configuration values must not contain line breaks or NUL")
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / "installation.env"
    # A hard link publishes a complete file atomically and never replaces an old profile.
    descriptor, temporary = tempfile.mkstemp(prefix=".installation-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key, value in values.items():
                quoted = value.replace("'", "\\'")
                handle.write(f"{key}='{quoted}'\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--port", type=int, default=3008)
    parser.add_argument("--recovery-profile", type=Path, help="Reuse verified backup keys in a new isolated project; never overwrite a profile.")
    parser.add_argument("--object-storage-profile", type=Path, help="Private JSON with endpoint, region, bucket, access_key_id, secret_access_key and optional session_token.")
    args = parser.parse_args()
    try:
        profile = configure(args.directory, public_origin=args.public_origin, port=args.port, recovery_profile=args.recovery_profile,
            object_storage_profile=args.object_storage_profile)
    except FileExistsError:
        print("Profile already exists; it was not replaced. Reuse installation.env to retain keys and data.")
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Profile was not initialized ({type(exc).__name__}). Check the origin, port and directory permissions.")
        return 2
    command = ["docker", "compose", "--env-file", str(profile), "-f", str(ROOT / "deploy/self-hosted/compose.yml")]
    print("Private installation profile created. Back up installation.env; it contains credential encryption keys.")
    print(shlex.join([*command, "config", "--quiet"]))
    print(shlex.join([*command, "up", "--build", "-d"]))
    print("Configuration and container startup do not prove readiness; use the company opening guide after signing in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
